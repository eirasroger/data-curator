"""What a datastore has to be able to do, and how to get one.

The protocol is named methods, not a `query(sql)` escape hatch. BigQuery and
DuckDB do not share a dialect - backticked table names and `@param` placeholders
are both parse errors in DuckDB - so any caller holding raw SQL is a caller
locked to one backend.

Implementations live in `domain/stores/`. Same arrangement as `Triager` and
`domain/providers/`.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any, Protocol

DEFAULT_BACKEND = os.environ.get("STORE_BACKEND", "bigquery")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class InsertError(RuntimeError):
    """A write was rejected. Never swallowed - the caller must nack."""


class Store(Protocol):
    """Every database operation the system performs."""

    # -- reads ---------------------------------------------------------------

    def current_record(self, product_id: int) -> dict | None: ...

    def already_decided(self, request_id: str) -> bool: ...

    def review_queue(self, limit: int = 50) -> list[dict]: ...

    def review_queue_depth(self) -> int: ...

    def get_request(self, request_id: str) -> dict | None: ...

    def change_status(self, request_id: str) -> dict | None: ...

    # -- reads used by the nightly job ---------------------------------------

    def expiry_candidates(self) -> list[dict]: ...

    def expired_product_ids(self) -> set[int]: ...

    def decision_stats(self, window_start: datetime) -> dict: ...

    def overdue_reviews(self, hours: int) -> int: ...

    def epd_count(self) -> int: ...

    # -- reads used by the dashboard -----------------------------------------

    def latest_reconciliation(self) -> dict | None: ...

    def agents_needing_attention(self) -> list[dict]: ...

    # -- writes --------------------------------------------------------------

    def save_request(self, request: Any) -> None: ...

    def save_event(
        self,
        decision: Any,
        product_id: int,
        event_type: str = "decided",
        actor: str = "pipeline",
        triage_outcome: Any = None,
        occurred_at: str = "",
    ) -> str: ...

    def save_version(
        self,
        record: dict,
        product_id: int,
        version: int,
        change_request_id: str | None,
        status: str = "active",
    ) -> None: ...

    def save_reconciliation(self, row: dict) -> None: ...


def to_change_request(row: dict) -> Any:
    """Rebuild a ChangeRequest from its stored row.

    Both backends store JSON columns as text and timestamps as datetimes, so
    this is shared rather than written twice.
    """
    from .changes import ChangeRequest

    def unjson(value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    submitted = row.get("submitted_at")
    return ChangeRequest(
        request_id=row["request_id"],
        product_id=row["product_id"],
        kind=row["kind"],
        source=row["source"],
        submitted_by=row["submitted_by"],
        # A TIMESTAMP column comes back as a datetime, but a replayed or
        # hand-inserted row can carry a string.
        submitted_at=(
            submitted.isoformat() if isinstance(submitted, datetime) else str(submitted)
        ),
        field_path=row.get("field_path"),
        new_value=unjson(row.get("new_value")),
        replacement=unjson(row.get("replacement")),
        reason=row["reason"],
    )


def request_row(request: Any) -> dict[str, Any]:
    """The change_requests row. Identical for both backends."""
    return {
        "request_id": request.request_id,
        "product_id": request.product_id,
        "kind": request.kind.value,
        "source": request.source.value,
        "submitted_by": request.submitted_by,
        "submitted_at": request.submitted_at,
        "field_path": request.field_path,
        # JSON columns take a JSON-encoded string, which lets one column hold a
        # number, a string or null without three columns.
        "new_value": json.dumps(request.new_value),
        "replacement": (
            json.dumps(request.replacement) if request.replacement else None
        ),
        "reason": request.reason,
    }


def event_row(
    decision: Any,
    product_id: int,
    event_id: str,
    event_type: str,
    actor: str,
    triage_outcome: Any,
    occurred_at: str = "",
) -> dict[str, Any]:
    """The change_events row. Identical for both backends.

    `occurred_at` defaults to now. It is only ever passed by a backfill, which
    is replaying decisions that carry their own timestamps.
    """
    row: dict[str, Any] = {
        "event_id": event_id,
        "request_id": decision.request_id,
        "product_id": product_id,
        "occurred_at": occurred_at or now_iso(),
        "event_type": event_type,
        "actor": actor,
        "action": decision.action.value,
        "reason": decision.reason,
        "blocking_issues": decision.blocking_issues or [],
        "old_value": json.dumps(decision.old_value),
        "triage": decision.triage.value if decision.triage else None,
        "confidence": decision.confidence,
        "rationale": decision.rationale,
    }
    # Absent model fields are meaningful: they say the rules settled this
    # without spending anything.
    if triage_outcome is not None:
        row.update({
            "model": triage_outcome.model,
            "prompt_tokens": triage_outcome.prompt_tokens,
            "completion_tokens": triage_outcome.completion_tokens,
            "cost_usd": round(triage_outcome.cost_usd, 8),
            "latency_ms": triage_outcome.latency_ms,
        })
    return row


def version_row(
    record: dict,
    product_id: int,
    version: int,
    change_request_id: str | None,
    status: str,
) -> dict[str, Any]:
    """The epd_records row. Identical for both backends."""
    clean = {k: v for k, v in record.items() if not k.startswith("_")}
    impacts = clean.get("impacts") or {}
    return {
        "product_id": product_id,
        "version": version,
        "valid_from": now_iso(),
        "change_request_id": change_request_id,
        "epd_code": clean.get("epd_code"),
        "prod_name": clean.get("prod_name"),
        "prod_man": clean.get("prod_man"),
        "prod_site": clean.get("prod_site"),
        "expiry_date": clean.get("date"),
        "status": status,
        "reference_unit": clean.get("reference_unit"),
        "density": clean.get("density"),
        "thickness": clean.get("thickness"),
        "lifespan": clean.get("lifespan"),
        "gwp_total": impacts.get("gwp_total"),
        "gwp_fossil": impacts.get("gwp_fossil"),
        "gwp_luluc": impacts.get("gwp_luluc"),
        "gwp_bio": impacts.get("gwp_bio"),
        "record": json.dumps(clean, ensure_ascii=False),
    }


def get_store(backend: str = "", **kwargs: Any) -> Store:
    """Build a store by name. Unknown names fail loudly rather than defaulting.

    Defaulting on a typo would mean a deployment quietly writing to the wrong
    place, or reading an empty local file and reporting no EPDs at all.
    """
    backend = backend or DEFAULT_BACKEND
    if backend == "bigquery":
        from .stores.bigquery_store import BigQueryStore

        return BigQueryStore(**kwargs)
    if backend == "duckdb":
        from .stores.duckdb_store import DuckDBStore

        return DuckDBStore(**kwargs)
    raise ValueError(
        f"unknown store backend {backend!r}; expected 'bigquery' or 'duckdb'"
    )


__all__ = [
    "InsertError",
    "Store",
    "event_row",
    "get_store",
    "now_iso",
    "request_row",
    "to_change_request",
    "version_row",
]
