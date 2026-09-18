"""The operator page: what needs a person, and what the pipeline has been doing.

Read-mostly. The one thing it writes is a review decision, and it writes that
the same way a person with curl would - by publishing to the existing review
path, not by touching the datastore. This service holds no write permission on
BigQuery and does not need any.

There are no timers on this page. Nothing it shows changes on a timescale that
would justify one: reconciliation writes one row a night, the agent registry
changes when an eval is run, and proposals arrive a few times a day. Every panel
states the age of what it shows instead, and the queue updates when you act on
it, because that is the only moment it has a reason to.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from domain import metrics, pipeline
from domain.changes import MessageType, ReviewDecision
from domain.store import get_store

HERE = Path(__file__).resolve().parent
PROJECT = os.environ.get("GCP_PROJECT", "")
TOPIC_ID = os.environ.get("PUBSUB_TOPIC", "epd-changes")
REVIEWER = os.environ.get("DASHBOARD_REVIEWER", "dashboard-user")

app = FastAPI(title="data-curator operator")
templates = Jinja2Templates(directory=str(HERE / "templates"))

_store = get_store()
_publisher: Any = None


def log(severity: str, message: str, **fields: Any) -> None:
    print(json.dumps({"severity": severity, "message": message, **fields}), flush=True)


def publisher() -> Any:
    global _publisher
    if _publisher is None:
        from google.cloud import pubsub_v1

        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def render(request: Request) -> HTMLResponse:
    overview = metrics.overview(_store)
    return templates.TemplateResponse(
        request=request,
        name="base.html",
        context={
            "overview": overview,
            "static": False,
            "sla_days": metrics.REVIEW_SLA_DAYS,
        },
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return render(request)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "backend": type(_store).__name__,
        "as_of": datetime.now(UTC).isoformat(),
    }


def submit_review(request_id: str, approve: bool, note: str) -> None:
    """Hand the verdict on, by whichever route this deployment has.

    With a topic configured, publish: the worker owns applying a review, this
    service holds no write permission on the datastore, and a decision taken
    here produces exactly the same events as one taken with curl.

    Without one - a local run against DuckDB, where there is no Pub/Sub and no
    worker - apply it in process. Same function the worker calls, so the two
    routes cannot disagree about what a review means.
    """
    review = ReviewDecision(
        request_id=request_id, reviewer=REVIEWER, approve=approve, note=note
    )

    if not PROJECT:
        applied = pipeline.apply_review(review, store=_store)
        log("INFO", "review applied locally",
            request_id=request_id, approve=approve, found=applied)
        return

    message = json.dumps({
        "type": MessageType.REVIEW.value,
        "payload": json.loads(review.model_dump_json()),
    }).encode()
    future = publisher().publish(
        publisher().topic_path(PROJECT, TOPIC_ID), message, request_id=request_id
    )
    future.result(timeout=10)
    log("INFO", "review queued", request_id=request_id, approve=approve)


@app.post("/reviews/{request_id}/approve", response_class=HTMLResponse)
def approve(request: Request, request_id: str, note: str = "") -> HTMLResponse:
    submit_review(request_id, True, note)
    return render(request)


@app.post("/reviews/{request_id}/reject", response_class=HTMLResponse)
def reject(request: Request, request_id: str, note: str = "") -> HTMLResponse:
    submit_review(request_id, False, note)
    return render(request)


@app.get("/healthz")
def healthz() -> RedirectResponse:
    # Google's front end intercepts /healthz on Cloud Run and answers with an
    # HTML 404 before the request reaches the container. Kept as a redirect so
    # anyone who tries it locally is pointed at the endpoint that works.
    return RedirectResponse("/health")
