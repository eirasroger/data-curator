"""Worker: decides queued change requests and writes the results.

Replies to Pub/Sub: 204 done, 400 permanently broken (goes to the DLQ), 500 retry.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
from typing import Any

from fastapi import FastAPI, Request, Response
from google.cloud import pubsub_v1

from domain import pipeline, reconcile
from domain.changes import Action, ChangeKind, ChangeRequest, MessageType, ReviewDecision
from domain.pipeline import ReviewOutcome
from domain.store import InsertError, get_store
from domain.triage import get_triager

TRIAGE_PROVIDER = os.environ.get("TRIAGE_PROVIDER", "stub")
TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "gpt-5-mini")
AGENT_ID = os.environ.get("AGENT_ID", "epd-change-triage")
PROJECT = os.environ.get("GCP_PROJECT", "")
TOPIC_ID = os.environ.get("PUBSUB_TOPIC", "epd-changes")

app = FastAPI(title="data-curator worker")

_store = get_store()
_triager: Any = None


def triager() -> Any:
    global _triager
    if _triager is None:
        _triager = get_triager(TRIAGE_PROVIDER, TRIAGE_MODEL)
    return _triager


_publisher: Any = None


def publisher() -> Any:
    global _publisher
    if _publisher is None:
        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def log(severity: str, message: str, **fields: Any) -> None:
    print(json.dumps({"severity": severity, "message": message, **fields}), flush=True)


def publish_change(req: ChangeRequest) -> None:
    """Queue an expiry request, so it follows the same path as any other change."""
    body = json.dumps({
        "type": MessageType.CHANGE_REQUEST.value,
        "payload": json.loads(req.model_dump_json()),
    }).encode()
    publisher().publish(
        publisher().topic_path(PROJECT, TOPIC_ID), body,
        message_type=MessageType.CHANGE_REQUEST.value,
        product_id=str(req.product_id), kind=req.kind.value,
    ).result(timeout=10)


@app.get("/health")
def health() -> dict:
    """Liveness check. Cloud Run intercepts /healthz, hence /health."""
    return {"status": "ok", "provider": TRIAGE_PROVIDER, "model": TRIAGE_MODEL}


@app.post("/")
async def handle(request: Request) -> Response:
    started = time.monotonic()

    try:
        envelope = await request.json()
        message = envelope["message"]
        body = json.loads(base64.b64decode(message["data"]))
        message_type = MessageType(body["type"])
        payload = body["payload"]
    except (KeyError, ValueError, TypeError, binascii.Error) as exc:
        log("ERROR", "unusable message", error=str(exc))
        return Response(status_code=400)

    delivery = message.get("deliveryAttempt", 1)

    try:
        if message_type is MessageType.CHANGE_REQUEST:
            return handle_change(ChangeRequest.model_validate(payload), delivery, started)
        return handle_review(ReviewDecision.model_validate(payload), delivery, started)
    except ValueError as exc:
        # Valid JSON in an unknown shape, so the failure is permanent.
        log("ERROR", "message failed validation", error=str(exc))
        return Response(status_code=400)
    except (InsertError, Exception) as exc:  # noqa: BLE001
        log("ERROR", "processing failed, will retry", error=str(exc),
            error_type=type(exc).__name__, delivery_attempt=delivery)
        return Response(status_code=500)


def handle_change(req: ChangeRequest, delivery: int, started: float) -> Response:
    # Pub/Sub can deliver twice; skip requests already decided.
    if _store.already_decided(req.request_id):
        log("INFO", "already decided, skipping duplicate",
            request_id=req.request_id, delivery_attempt=delivery)
        return Response(status_code=204)

    record = _store.current_record(req.product_id)
    if record is None:
        log("ERROR", "no such EPD", request_id=req.request_id, product_id=req.product_id)
        return Response(status_code=400)

    if delivery == 1:
        _store.save_request(req)

    outcome = pipeline.decide(record, req, triager())
    decision = outcome.decision

    _store.save_event(decision, req.product_id, triage_outcome=outcome.triage)

    if decision.action is Action.APPLIED:
        updated = pipeline.apply_decision(record, req, decision)
        status = "expired" if req.kind is ChangeKind.EXPIRY else "active"
        _store.save_version(
            updated, req.product_id, int(record.get("_version", 1)) + 1,
            req.request_id, status=status,
        )

    log("INFO", "change decided",
        request_id=req.request_id, product_id=req.product_id,
        action=decision.action.value, reason=decision.reason,
        used_model=outcome.used_model,
        triage=decision.triage.value if decision.triage else None,
        confidence=decision.confidence,
        model=outcome.triage.model if outcome.triage else None,
        prompt_tokens=outcome.triage.prompt_tokens if outcome.triage else 0,
        completion_tokens=outcome.triage.completion_tokens if outcome.triage else 0,
        cost_usd=round(outcome.cost_usd, 6),
        model_latency_ms=outcome.latency_ms,
        total_latency_ms=round((time.monotonic() - started) * 1000, 1),
        agent_id=AGENT_ID)

    return Response(status_code=204)


def handle_review(review: ReviewDecision, delivery: int, started: float) -> Response:
    """Apply a person's verdict on a parked change."""
    outcome = pipeline.apply_review(review, store=_store)

    if outcome is ReviewOutcome.UNKNOWN_REQUEST:
        log("ERROR", "review for an unknown request", request_id=review.request_id)
        return Response(status_code=400)

    if outcome is ReviewOutcome.NOT_PENDING:
        # Duplicate or stale verdict: acknowledge, since a retry changes nothing.
        log("INFO", "review ignored, request is not awaiting one",
            request_id=review.request_id, delivery_attempt=delivery)
        return Response(status_code=204)

    log("INFO", "review applied",
        request_id=review.request_id,
        reviewer=review.reviewer, approve=review.approve,
        total_latency_ms=round((time.monotonic() - started) * 1000, 1))

    return Response(status_code=204)


@app.post("/jobs/reconcile")
def run_reconcile() -> dict:
    """The nightly job, called by Cloud Scheduler. Access is set by Cloud Run IAM."""
    started = time.monotonic()
    summary = reconcile.run(_store, publish_change)
    _store.save_reconciliation(summary)

    log("WARNING" if summary["needs_attention"] else "INFO",
        "reconciliation complete",
        run_id=summary["run_id"],
        needs_attention=summary["needs_attention"],
        drift_flags=summary["drift_flags"],
        epds_expired=summary["epds_expired"],
        expiry_requests_raised=summary["expiry_requests_raised"],
        requests_total=summary["requests_total"],
        reviews_overdue=summary["reviews_overdue"],
        total_cost_usd=summary["total_cost_usd"],
        duration_ms=round((time.monotonic() - started) * 1000, 1))

    return summary
