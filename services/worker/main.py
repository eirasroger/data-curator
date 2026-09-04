"""The worker: decides change requests and writes the result.

Woken by Pub/Sub push. Everything slow or failure-prone happens here, off the
caller's request path - the model call, the BigQuery writes, the retries.

HTTP status is how this service talks to Pub/Sub, so the codes are load-bearing:

    204  done, do not send this again
    400  this message is broken and always will be. Retrying wastes calls and
         hides the failure inside normal-looking traffic; after the configured
         number of attempts it lands in the dead-letter topic, which is what
         the DLQ is for.
    500  something transient went wrong - BigQuery hiccup, model outage.
         Pub/Sub will back off and try again, which is what we want.

Returning 204 on a failure would silently drop the change. Returning 500 on a
permanently broken message would retry it forever.
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
from domain.store import InsertError, Store
from domain.triage import get_triager

TRIAGE_PROVIDER = os.environ.get("TRIAGE_PROVIDER", "stub")
TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "gpt-5-mini")
AGENT_ID = os.environ.get("AGENT_ID", "epd-change-triage")
PROJECT = os.environ.get("GCP_PROJECT", "")
TOPIC_ID = os.environ.get("PUBSUB_TOPIC", "epd-changes")

app = FastAPI(title="data-curator worker")

_store = Store()
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
    """Put an expiry request onto the same topic everything else arrives on.

    The nightly job could edit the record directly - it has the permission and
    expiry needs no judgement. It goes through the queue anyway, so that a
    change raised by the calendar is screened, logged and versioned by exactly
    the same code as a change raised by a person. One path, three sources.
    """
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
    """Liveness check.

    NOT /healthz. Google's front end intercepts that exact path on Cloud Run
    and answers it itself with an HTML 404, so the request never reaches the
    container - even though FastAPI has the route registered and lists it in
    openapi.json. The app was fine the whole time.
    """
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
        return Response(status_code=400)  # permanently broken -> DLQ

    delivery = message.get("deliveryAttempt", 1)

    try:
        if message_type is MessageType.CHANGE_REQUEST:
            return handle_change(ChangeRequest.model_validate(payload), delivery, started)
        return handle_review(ReviewDecision.model_validate(payload), delivery, started)
    except ValueError as exc:
        # The message parsed as JSON but is not a shape we understand. No amount
        # of retrying changes that.
        log("ERROR", "message failed validation", error=str(exc))
        return Response(status_code=400)
    except (InsertError, Exception) as exc:  # noqa: BLE001
        log("ERROR", "processing failed, will retry", error=str(exc),
            error_type=type(exc).__name__, delivery_attempt=delivery)
        return Response(status_code=500)


def handle_change(req: ChangeRequest, delivery: int, started: float) -> Response:
    # Pub/Sub delivers at least once, so the same request can arrive twice.
    # Without this check a duplicate would write a second decision and apply the
    # change a second time, producing a version nobody asked for.
    if _store.already_decided(req.request_id):
        log("INFO", "already decided, skipping duplicate",
            request_id=req.request_id, delivery_attempt=delivery)
        return Response(status_code=204)

    record = _store.current_record(req.product_id)
    if record is None:
        log("ERROR", "no such EPD", request_id=req.request_id, product_id=req.product_id)
        return Response(status_code=400)  # will never exist on a retry

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
        # Cost and latency per decision, in the log line, so "what is this
        # costing" is a Logs Explorer query rather than a guess.
        cost_usd=round(outcome.cost_usd, 6),
        model_latency_ms=outcome.latency_ms,
        total_latency_ms=round((time.monotonic() - started) * 1000, 1),
        agent_id=AGENT_ID)

    return Response(status_code=204)


def handle_review(review: ReviewDecision, delivery: int, started: float) -> Response:
    """Apply a person's verdict on a parked change."""
    original = _store.get_request(review.request_id)
    if original is None:
        log("ERROR", "review for an unknown request", request_id=review.request_id)
        return Response(status_code=400)

    req = _store.to_change_request(original)

    from domain.changes import Decision

    decision = Decision(
        request_id=review.request_id,
        action=Action.APPLIED if review.approve else Action.REJECTED,
        reason=f"Reviewed by {review.reviewer}: "
               f"{'approved' if review.approve else 'rejected'}."
               + (f" {review.note}" if review.note else ""),
    )

    if review.approve:
        record = _store.current_record(req.product_id)
        if record is None:
            return Response(status_code=400)
        updated = pipeline.apply_decision(record, req, decision)
        status = "expired" if req.kind is ChangeKind.EXPIRY else "active"
        _store.save_version(
            updated, req.product_id, int(record.get("_version", 1)) + 1,
            req.request_id, status=status,
        )

    # event_type "reviewed", not "decided": the pipeline's original conclusion
    # stays in the log untouched. Both answers are on the record.
    _store.save_event(decision, req.product_id,
                      event_type="reviewed", actor=review.reviewer)

    log("INFO", "review applied",
        request_id=review.request_id, product_id=req.product_id,
        reviewer=review.reviewer, approve=review.approve,
        total_latency_ms=round((time.monotonic() - started) * 1000, 1))

    return Response(status_code=204)


@app.post("/jobs/reconcile")
def run_reconcile() -> dict:
    """The nightly check. Invoked by Cloud Scheduler, never by the public.

    Access is controlled by Cloud Run IAM: only identities holding run.invoker
    on this service can reach it, and the scheduler has its own account. There
    is no shared secret to leak and no token to rotate.
    """
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
