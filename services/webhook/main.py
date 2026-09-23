"""Public webhook for manufacturer updates. Checks the signature before reading the body.

The only public service; it can publish to the queue and read its own signing keys.
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
    """The signing secret for a source, mounted as WEBHOOK_SECRET_<SOURCE>."""
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

    # Hash the raw bytes; re-serialised JSON would change the digest.
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
        # The sender is authenticated, so it gets a useful error.
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
        # Wait for the publish to succeed before replying 202.
        publisher().publish(
            publisher().topic_path(PROJECT, TOPIC_ID), message,
            message_type=MessageType.CHANGE_REQUEST.value,
            product_id=str(change.product_id),
            kind=change.kind.value,
        ).result(timeout=10)
    except Exception as exc:  # noqa: BLE001
        log("ERROR", "publish failed", source=source, error=str(exc))
        # 5xx asks the sender to retry.
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
