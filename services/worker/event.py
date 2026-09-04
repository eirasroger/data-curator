# GENERATED FILE - DO NOT EDIT.
# Source: contracts/event.py   Regenerate: scripts/sync_contracts.sh

"""Event contract shared by the ingest service and the enrichment worker.

This file is the single source of truth. `scripts/sync_contracts.sh` copies it
into each service directory, because Cloud Run source deploys only upload the
service's own directory and cannot reach a sibling package. `make check` (and
CI) re-runs the sync and fails if anything drifted.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Bumped whenever the envelope shape changes in a way consumers must notice.
CONTRACT_VERSION = "1"


class HubSpotEvent(BaseModel):
    """One element of a HubSpot webhook payload.

    HubSpot POSTs a JSON *array* of these. Unknown fields are kept rather than
    rejected: HubSpot adds properties over time, and a webhook that 400s on an
    unrecognised field is a webhook that silently loses data.
    """

    model_config = ConfigDict(extra="allow")

    event_id: int = Field(alias="eventId")
    subscription_id: int | None = Field(default=None, alias="subscriptionId")
    portal_id: int | None = Field(default=None, alias="portalId")
    app_id: int | None = Field(default=None, alias="appId")
    occurred_at: int = Field(alias="occurredAt")  # epoch milliseconds
    subscription_type: str = Field(alias="subscriptionType")
    attempt_number: int = Field(default=0, alias="attemptNumber")
    object_id: int | None = Field(default=None, alias="objectId")
    property_name: str | None = Field(default=None, alias="propertyName")
    property_value: str | None = Field(default=None, alias="propertyValue")
    change_source: str | None = Field(default=None, alias="changeSource")

    @property
    def occurred_at_iso(self) -> str:
        return datetime.fromtimestamp(
            self.occurred_at / 1000, tz=timezone.utc
        ).isoformat()

    @property
    def idempotency_key(self) -> str:
        """Stable across HubSpot's own retries.

        `eventId` is unique per event and does NOT change between delivery
        attempts (only `attemptNumber` does), so it is the right dedupe key.
        Hashed so the key is a fixed-width string regardless of source system.
        """
        return hashlib.sha256(
            f"hubspot:{self.event_id}".encode()
        ).hexdigest()[:32]


class Envelope(BaseModel):
    """What ingest publishes to Pub/Sub and the worker consumes.

    The raw event is carried verbatim alongside the parsed fields so the worker
    (and any future consumer) can re-derive anything ingest chose not to parse.
    """

    contract_version: Literal["1"] = CONTRACT_VERSION
    idempotency_key: str
    source: str = "hubspot"
    received_at: str  # ISO 8601, when ingest accepted it
    event: HubSpotEvent
    raw: dict[str, Any]


def make_envelope(event: HubSpotEvent, raw: dict[str, Any]) -> Envelope:
    return Envelope(
        idempotency_key=event.idempotency_key,
        received_at=datetime.now(timezone.utc).isoformat(),
        event=event,
        raw=raw,
    )
