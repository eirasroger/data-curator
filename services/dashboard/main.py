"""Operator dashboard: review queue, throughput and the latest nightly check.

Reviews are published to the queue; the service has read-only database access.
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
    """Publish the verdict, or apply it directly in a local run with no Pub/Sub."""
    review = ReviewDecision(
        request_id=request_id, reviewer=REVIEWER, approve=approve, note=note
    )

    if not PROJECT:
        outcome = pipeline.apply_review(review, store=_store)
        log("INFO", "review applied locally",
            request_id=request_id, approve=approve, outcome=outcome.value)
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
    # Cloud Run intercepts /healthz, so point callers at /health.
    return RedirectResponse("/health")
