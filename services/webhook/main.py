"""Public webhook receiver.

This is the only component reachable from the open internet. Everything else in
the system requires a Google identity, which an external sender cannot have, so
this service exists to hold that exposure by itself and to hold as little
authority as possible while doing it.

What it can do: verify a signature and publish a message.
What it cannot do: read or write BigQuery, read any secret but its own webhook
keys, or reach any other service.

The order of operations matters. Nothing is parsed, logged in full, or acted on
until the signature has been checked. An unverified request gets a bare 401 and
no explanation, because telling a stranger whether their signature or their
timestamp was wrong helps them fix it.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request, Response

from domain import webhooks
from domain.changes import ChangeKind, ChangeRequest, MessageType, Source
from domain.schema import EPDProduct

PROJECT = os.environ.get("GCP_PROJECT", "")
TOPIC_ID = os.environ.get("PUBSUB_TOPIC", "epd-changes")

app = FastAPI(title="data-curator webhooks")

_publisher: Any = None


def publisher() -> Any:
    global _publisher
    if _publisher is None:
        from google.cloud import pubsub_v1

        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def log(severity: str, message: str, **fields: Any) -> None:
    print(json.dumps({"severity": severity, "message": message, **fields}), flush=True)


def secret_for(source: str) -> str:
    """One secret per source.

    Sources are separated so that a leaked key compromises one sender instead of
    all of them, and so a sender can be revoked by deleting one secret version.
    Cloud Run mounts each as WEBHOOK_SECRET_<SOURCE>.
    """
    if not source.replace("-", "").replace("_", "").isalnum():
        return ""
    key = f"WEBHOOK_SECRET_{source.upper().replace('-', '_')}"
    return os.environ.get(key, "")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "sources": sorted(_configured_sources())}


def _configured_sources() -> list[str]:
    prefix = "WEBHOOK_SECRET_"
    return [k[len(prefix):].lower() for k in os.environ if k.startswith(prefix)]


@app.post("/webhooks/{source}")
async def receive(source: str, request: Request) -> Response:
    started = time.monotonic()

    # The raw bytes, before anything touches them. Parsing and re-serialising
    # before hashing reorders keys and changes whitespace, and the digest stops
    # matching for reasons that are invisible in the code.
    body = await request.body()

    accepted, reason = webhooks.verify(
        secret=secret_for(source),
        body=body,
        signature=request.headers.get(webhooks.SIGNATURE_HEADER),
        timestamp=request.headers.get(webhooks.TIMESTAMP_HEADER),
    )
    if not accepted:
        log("WARNING", "webhook rejected", source=source, reason=reason,
            body_bytes=len(body), remote=request.client.host if request.client else None)
        return Response(status_code=401)

    try:
        payload = json.loads(body)
        record = payload["record"]
        EPDProduct.model_validate(record)
    except Exception as exc:  # noqa: BLE001
        # Authenticated, so we can afford to say what was wrong. The sender is
        # known and needs to be able to fix their integration.
        log("ERROR", "webhook payload invalid", source=source, error=str(exc)[:300])
        return Response(
            content=json.dumps({"error": "payload must be {\"record\": <EPDProduct>}"}),
            media_type="application/json",
            status_code=422,
        )

    change = ChangeRequest(
        request_id=str(uuid.uuid4()),
        product_id=record["product_id"],
        kind=ChangeKind.RECORD_REPLACEMENT,
        source=Source.MANUFACTURER_FEED,
        submitted_by=f"webhook:{source}",
        replacement=record,
        reason=payload.get(
            "reason", f"{source} published a new version of {record.get('epd_code')}"
        ),
    )

    message = json.dumps({
        "type": MessageType.CHANGE_REQUEST.value,
        "payload": json.loads(change.model_dump_json()),
    }).encode()

    try:
        # Confirmed before answering. A sender that receives 202 will not send
        # this event again, so acknowledging before the message is durable
        # loses it permanently.
        publisher().publish(
            publisher().topic_path(PROJECT, TOPIC_ID), message,
            message_type=MessageType.CHANGE_REQUEST.value,
            product_id=str(change.product_id),
            kind=change.kind.value,
        ).result(timeout=10)
    except Exception as exc:  # noqa: BLE001
        log("ERROR", "publish failed", source=source, error=str(exc))
        # 5xx tells the sender to retry. Most webhook senders back off and
        # redeliver, which is exactly what should happen here.
        return Response(status_code=503)

    log("INFO", "webhook accepted",
        source=source, request_id=change.request_id,
        product_id=change.product_id, epd_code=record.get("epd_code"),
        latency_ms=round((time.monotonic() - started) * 1000, 1))

    return Response(
        content=json.dumps({"request_id": change.request_id, "status": "queued"}),
        media_type="application/json",
        status_code=202,
    )
