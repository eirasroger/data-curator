"""The BigQuery implementation. Production.

Writes go through the streaming insert API (`insert_rows_json`). Two things
follow from that and both suit us:

  - Rows are queryable within seconds, which is what makes `already_decided`
    work at all.
  - Rows in the streaming buffer cannot be UPDATEd or DELETEd for a while
    afterwards. For an append-only design that is not a limitation, it is the
    rule being enforced by the platform.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any

from google.cloud import bigquery

from ..store import InsertError, event_row, request_row, version_row

PROJECT = os.environ.get("GCP_PROJECT", "")
DATASET = os.environ.get("BQ_DATASET", "curator")


class BigQueryStore:
    def __init__(self, project: str = "", dataset: str = "") -> None:
        self.project = project or PROJECT
        self.dataset = dataset or DATASET
        self._client: bigquery.Client | None = None

    @property
    def client(self) -> bigquery.Client:
        # Built on first use. Constructing it at import time would make this
        # module unimportable without credentials, including under test.
        if self._client is None:
            self._client = bigquery.Client(project=self.project)
        return self._client

    def table(self, name: str) -> str:
        return f"{self.project}.{self.dataset}.{name}"

    def query(self, sql: str, **params: Any) -> list[dict]:
        """Run a parameterised query.

        Always parameterised, never f-strings: a product_id arriving from an
        HTTP path segment is untrusted input, and string-building it into SQL is
        how injection happens.
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[_param(k, v) for k, v in params.items()]
        )
        rows = self.client.query(sql, job_config=job_config).result()
        return [dict(row) for row in rows]

    # -- reads ---------------------------------------------------------------

    def current_record(self, product_id: int) -> dict | None:
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

    def get_request(self, request_id: str) -> dict | None:
        rows = self.query(
            f"SELECT * FROM `{self.table('change_requests')}` "
            f"WHERE request_id = @request_id",
            request_id=request_id,
        )
        return rows[0] if rows else None

    def change_status(self, request_id: str) -> dict | None:
        rows = self.query(
            f"SELECT * FROM `{self.table('change_status')}` "
            f"WHERE request_id = @request_id",
            request_id=request_id,
        )
        return rows[0] if rows else None

    # -- reads used by the nightly job ---------------------------------------

    def expiry_candidates(self) -> list[dict]:
        return self.query(
            f"SELECT product_id, epd_code, prod_name, expiry_date, expiry_state "
            f"FROM `{self.table('epd_expiry_status')}` "
            f"WHERE expiry_state IN ('expired', 'expiring within 90 days')"
        )

    def expired_product_ids(self) -> set[int]:
        return {
            r["product_id"]
            for r in self.query(
                f"SELECT DISTINCT product_id FROM `{self.table('epd_current')}` "
                f"WHERE status = 'expired'"
            )
        }

    def decision_stats(self, window_start: datetime) -> dict:
        rows = self.query(
            f"""
            SELECT
              COUNT(*) AS total,
              COUNTIF(action = 'applied') AS applied,
              COUNTIF(action = 'rejected') AS rejected,
              COUNTIF(action = 'pending_review') AS pending,
              COUNTIF(model IS NOT NULL) AS model_calls,
              AVG(confidence) AS mean_confidence,
              APPROX_QUANTILES(latency_ms, 100)[OFFSET(95)] AS p95_latency_ms,
              SUM(cost_usd) AS total_cost
            FROM `{self.table('change_events')}`
            WHERE occurred_at >= @window_start AND event_type = 'decided'
            """,
            window_start=window_start.isoformat(),
        )
        return rows[0] if rows else {}

    def overdue_reviews(self, hours: int) -> int:
        rows = self.query(
            f"SELECT COUNT(*) AS n FROM `{self.table('review_queue')}` "
            f"WHERE hours_waiting > @hours",
            hours=hours,
        )
        return int(rows[0]["n"]) if rows else 0

    def epd_count(self) -> int:
        rows = self.query(f"SELECT COUNT(*) AS n FROM `{self.table('epd_current')}`")
        return int(rows[0]["n"]) if rows else 0

    # -- writes --------------------------------------------------------------

    def _insert(self, table: str, rows: list[dict]) -> None:
        errors = self.client.insert_rows_json(self.table(table), rows)
        if errors:
            raise InsertError(f"{table}: {errors}")

    def save_request(self, request: Any) -> None:
        """The proposal, exactly as submitted. Never modified afterwards."""
        self._insert("change_requests", [request_row(request)])

    def save_event(
        self,
        decision: Any,
        product_id: int,
        event_type: str = "decided",
        actor: str = "pipeline",
        triage_outcome: Any = None,
        occurred_at: str = "",
    ) -> str:
        """One line in the audit trail."""
        event_id = str(uuid.uuid4())
        self._insert("change_events", [event_row(
            decision, product_id, event_id, event_type, actor, triage_outcome,
            occurred_at,
        )])
        return event_id

    def save_version(
        self,
        record: dict,
        product_id: int,
        version: int,
        change_request_id: str | None,
        status: str = "active",
    ) -> None:
        """A new version of an EPD. Never an update of the old one."""
        self._insert("epd_records", [
            version_row(record, product_id, version, change_request_id, status)
        ])

    def save_reconciliation(self, row: dict) -> None:
        self._insert("reconciliation_runs", [row])


def _param(name: str, value: Any) -> bigquery.ScalarQueryParameter:
    kind = {int: "INT64", float: "FLOAT64", bool: "BOOL"}.get(type(value), "STRING")
    return bigquery.ScalarQueryParameter(name, kind, value)
