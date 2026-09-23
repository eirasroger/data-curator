"""Public API: validates change requests and reviews, and queues them on Pub/Sub."""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from google.cloud import pubsub_v1
from pydantic import BaseModel, Field

from domain.changes import ChangeKind, ChangeRequest, MessageType, ReviewDecision, Source
from domain.store import get_store

PROJECT = os.environ.get("GCP_PROJECT", "")
TOPIC_ID = os.environ.get("PUBSUB_TOPIC", "epd-changes")

app = FastAPI(
    title="data-curator API",
    description="Submit and review changes to EPD records.",
)

_publisher: Any = None
_store = get_store()


def publisher() -> Any:
    global _publisher
    if _publisher is None:
        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def log(severity: str, message: str, **fields: Any) -> None:
    """One JSON line on stdout, which Cloud Logging parses into structured fields."""
    print(json.dumps({"severity": severity, "message": message, **fields}), flush=True)


def publish(message_type: MessageType, payload: dict, **attributes: str) -> str:
    """Publish one message and wait for confirmation."""
    body = json.dumps({"type": message_type.value, "payload": payload}).encode()
    future = publisher().publish(
        publisher().topic_path(PROJECT, TOPIC_ID),
        body,
        # Attributes let subscriptions filter by message type.
        message_type=message_type.value,
        **attributes,
    )
    return future.result(timeout=10)


# ---------------------------------------------------------------------------

class SubmitChange(BaseModel):
    """What a caller sends to propose a change."""

    product_id: int
    kind: ChangeKind = ChangeKind.FIELD_UPDATE
    source: Source = Source.HUMAN
    submitted_by: str = Field(max_length=200, description="who is proposing this")
    reason: str = Field(
        min_length=3, max_length=2000, description="why they think it is right"
    )
    field_path: str | None = Field(
        default=None,
        description="e.g. 'density', 'impacts.gwp_total', "
                    "'product_integrity.comp[Basalt].percentage'",
    )
    new_value: Any = None
    replacement: dict | None = None


@app.get("/health")
def health() -> dict:
    """Liveness check. Cloud Run intercepts /healthz, hence /health."""
    return {"status": "ok"}


@app.post("/changes", status_code=202)
def submit_change(body: SubmitChange) -> dict:
    """Propose a change. Returns immediately with an id to follow it by."""
    started = time.monotonic()
    request = ChangeRequest(request_id=str(uuid.uuid4()), **body.model_dump())

    # Shape checks only; the worker judges the change itself.
    if request.kind is ChangeKind.FIELD_UPDATE and not request.field_path:
        raise HTTPException(422, "field_update requires field_path")
    if request.kind is ChangeKind.RECORD_REPLACEMENT and not request.replacement:
        raise HTTPException(422, "record_replacement requires replacement")

    try:
        message_id = publish(
            MessageType.CHANGE_REQUEST,
            json.loads(request.model_dump_json()),
            product_id=str(request.product_id),
            kind=request.kind.value,
        )
    except Exception as exc:  # noqa: BLE001
        log("ERROR", "publish failed", error=str(exc), request_id=request.request_id)
        # 503 tells the caller that nothing was queued.
        raise HTTPException(
            503, "could not queue the request; nothing was stored"
        ) from exc

    log("INFO", "change request queued",
        request_id=request.request_id, product_id=request.product_id,
        kind=request.kind.value, field_path=request.field_path,
        message_id=message_id,
        latency_ms=round((time.monotonic() - started) * 1000, 1))

    return {"request_id": request.request_id, "status": "queued"}


@app.post("/changes/{request_id}/review", status_code=202)
def review(request_id: str, body: ReviewDecision) -> dict:
    """Queue a person's decision on a parked change. The worker applies it."""
    if body.request_id != request_id:
        raise HTTPException(422, "request_id in the path and body must match")

    try:
        message_id = publish(
            MessageType.REVIEW,
            json.loads(body.model_dump_json()),
            request_id=request_id,
        )
    except Exception as exc:  # noqa: BLE001
        log("ERROR", "publish failed", error=str(exc), request_id=request_id)
        raise HTTPException(503, "could not queue the review") from exc

    log("INFO", "review queued", request_id=request_id,
        reviewer=body.reviewer, approve=body.approve, message_id=message_id)
    return {"request_id": request_id, "status": "queued"}


@app.get("/epd/{product_id}")
def get_epd(product_id: int) -> dict:
    record = _store.current_record(product_id)
    if record is None:
        raise HTTPException(404, f"no EPD with product_id {product_id}")
    return record


@app.get("/reviews")
def pending_reviews(limit: int = 50) -> dict:
    """What is waiting for a person, oldest first."""
    rows = _store.review_queue(limit=limit)
    return {"count": len(rows), "items": rows}


@app.get("/changes/{request_id}")
def change_status(request_id: str) -> dict:
    status = _store.change_status(request_id)
    if status is None:
        raise HTTPException(404, f"no change request {request_id}")
    return status
