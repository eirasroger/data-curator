"""Everything that touches BigQuery.

Kept in one place so there is exactly one file to read when asking "how does a
row get written", and exactly one place the append-only rule can be broken.

Writes go through the streaming insert API (`insert_rows_json`). Two things
follow from that and both suit us:

  - Rows are queryable within seconds, which is what makes the idempotency
    check below work at all.
  - Rows in the streaming buffer cannot be UPDATEd or DELETEd for a while
    afterwards. For an append-only design that is not a limitation, it is the
    rule being enforced by the platform.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from google.cloud import bigquery

PROJECT = os.environ.get("GCP_PROJECT", "")
DATASET = os.environ.get("BQ_DATASET", "curator")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InsertError(RuntimeError):
    """A streaming insert was rejected. Never swallowed - the caller must nack."""


class Store:
    def __init__(self, project: str = "", dataset: str = "") -> None:
        self.project = project or PROJECT
        self.dataset = dataset or DATASET
        self._client: Optional[bigquery.Client] = None

    @property
    def client(self) -> bigquery.Client:
        # Built on first use. Constructing it at import time would make this
        # module unimportable without credentials, including under test.
        if self._client is None:
            self._client = bigquery.Client(project=self.project)
        return self._client

    def table(self, name: str) -> str:
        return f"{self.project}.{self.dataset}.{name}"

    # -- reads ---------------------------------------------------------------

    def query(self, sql: str, **params: Any) -> list[dict]:
        """Run a parameterised query.

        Always parameterised, never f-strings: a product_id arriving from an
        HTTP path segment is untrusted input, and string-building it into SQL is
        how injection happens.
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[_param(k, v) for k, v in params.items()]
        )
        return [dict(row) for row in self.client.query(sql, job_config=job_config).result()]

    def current_record(self, product_id: int) -> Optional[dict]:
        """The latest version of one EPD, as the extractor's own dict shape."""
        rows = self.query(
            f"SELECT version, record FROM `{self.table('epd_current')}` "
            f"WHERE product_id = @product_id",
            product_id=product_id,
        )
        if not rows:
            return None
        record = rows[0]["record"]
        if isinstance(record, str):
            record = json.loads(record)
        record["_version"] = rows[0]["version"]
        return record

    def already_decided(self, request_id: str) -> bool:
        """Has the pipeline already ruled on this request?

        Pub/Sub guarantees at-least-once delivery, so the same message can and
        will arrive twice - after a redeploy, after a timeout, after a retry
        that succeeded on the far side. Without this check a duplicate would
        write a second decision and, worse, apply the change a second time.
        """
        rows = self.query(
            f"SELECT 1 FROM `{self.table('change_events')}` "
            f"WHERE request_id = @request_id AND event_type = 'decided' LIMIT 1",
            request_id=request_id,
        )
        return bool(rows)

    def review_queue(self, limit: int = 50) -> list[dict]:
        return self.query(
            f"SELECT * FROM `{self.table('review_queue')}` LIMIT @limit",
            limit=limit,
        )

    def get_request(self, request_id: str) -> Optional[dict]:
        """The original proposal, as submitted."""
        rows = self.query(
            f"SELECT * FROM `{self.table('change_requests')}` "
            f"WHERE request_id = @request_id",
            request_id=request_id,
        )
        return rows[0] if rows else None

    @staticmethod
    def to_change_request(row: dict) -> Any:
        """Rebuild a ChangeRequest from its stored row.

        BigQuery hands back JSON columns as strings and timestamps as datetimes,
        so this is where those go back to the shapes the domain expects.
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
            submitted_at=submitted.isoformat() if hasattr(submitted, "isoformat") else str(submitted),
            field_path=row.get("field_path"),
            new_value=unjson(row.get("new_value")),
            replacement=unjson(row.get("replacement")),
            reason=row["reason"],
        )

    def change_status(self, request_id: str) -> Optional[dict]:
        rows = self.query(
            f"SELECT * FROM `{self.table('change_status')}` "
            f"WHERE request_id = @request_id",
            request_id=request_id,
        )
        return rows[0] if rows else None

    # -- writes --------------------------------------------------------------

    def _insert(self, table: str, rows: list[dict]) -> None:
        errors = self.client.insert_rows_json(self.table(table), rows)
        if errors:
            raise InsertError(f"{table}: {errors}")

    def save_request(self, request: Any) -> None:
        """The proposal, exactly as submitted. Never modified afterwards."""
        self._insert("change_requests", [{
            "request_id": request.request_id,
            "product_id": request.product_id,
            "kind": request.kind.value,
            "source": request.source.value,
            "submitted_by": request.submitted_by,
            "submitted_at": request.submitted_at,
            "field_path": request.field_path,
            # JSON columns take a JSON-encoded string, which lets one column
            # hold a number, a string or null without three columns.
            "new_value": json.dumps(request.new_value),
            "replacement": json.dumps(request.replacement) if request.replacement else None,
            "reason": request.reason,
        }])

    def save_event(
        self,
        decision: Any,
        product_id: int,
        event_type: str = "decided",
        actor: str = "pipeline",
        triage_outcome: Any = None,
    ) -> str:
        """One line in the audit trail."""
        event_id = str(uuid.uuid4())
        row: dict[str, Any] = {
            "event_id": event_id,
            "request_id": decision.request_id,
            "product_id": product_id,
            "occurred_at": _now(),
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
        self._insert("change_events", [row])
        return event_id

    def save_version(self, record: dict, product_id: int, version: int,
                     change_request_id: str, status: str = "active") -> None:
        """A new version of an EPD. Never an update of the old one."""
        clean = {k: v for k, v in record.items() if not k.startswith("_")}
        impacts = clean.get("impacts") or {}
        self._insert("epd_records", [{
            "product_id": product_id,
            "version": version,
            "valid_from": _now(),
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
        }])

    def save_reconciliation(self, row: dict) -> None:
        self._insert("reconciliation_runs", [row])


def _param(name: str, value: Any) -> bigquery.ScalarQueryParameter:
    kind = {int: "INT64", float: "FLOAT64", bool: "BOOL"}.get(type(value), "STRING")
    return bigquery.ScalarQueryParameter(name, kind, value)
