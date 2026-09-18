"""The DuckDB implementation. Local runs, tests, and the analysis.

Same tables and views as BigQuery, same method contract, no credentials and no
network. What differs is only dialect, and it is confined to this file.

Append-only is a promise made by this code rather than by the platform: nothing
in DuckDB prevents an UPDATE, so the protection is that no method issues one.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from ..store import InsertError, event_row, request_row, version_row

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "sql" / "local" / "schema.sql"

# Where seed_local.py puts the database, so seeding and then running needs no
# configuration. Tests pass ":memory:" explicitly.
DEFAULT_PATH = os.environ.get("DUCKDB_PATH", str(ROOT / "local.duckdb"))


class DuckDBStore:
    def __init__(self, path: str = "") -> None:
        self.path = path or DEFAULT_PATH
        self._conn: duckdb.DuckDBPyConnection | None = None

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            self._conn = duckdb.connect(self.path)
            self._conn.execute(SCHEMA.read_text(encoding="utf-8"))
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def query(self, sql: str, **params: Any) -> list[dict]:
        """Run a parameterised query.

        Always parameterised, never f-strings: a product_id arriving from an
        HTTP path segment is untrusted input, and string-building it into SQL is
        how injection happens.
        """
        cur = self.conn.execute(sql, params) if params else self.conn.execute(sql)
        if cur.description is None:
            return []
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]

    # -- reads ---------------------------------------------------------------

    def current_record(self, product_id: int) -> dict | None:
        rows = self.query(
            "SELECT version, record FROM epd_current WHERE product_id = $product_id",
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
        rows = self.query(
            "SELECT 1 FROM change_events "
            "WHERE request_id = $request_id AND event_type = 'decided' LIMIT 1",
            request_id=request_id,
        )
        return bool(rows)

    def review_queue(self, limit: int = 50) -> list[dict]:
        return self.query("SELECT * FROM review_queue LIMIT $limit", limit=limit)

    def review_queue_depth(self) -> int:
        rows = self.query("SELECT COUNT(*) AS n FROM review_queue")
        return int(rows[0]["n"]) if rows else 0

    def get_request(self, request_id: str) -> dict | None:
        rows = self.query(
            "SELECT * FROM change_requests WHERE request_id = $request_id",
            request_id=request_id,
        )
        return rows[0] if rows else None

    def change_status(self, request_id: str) -> dict | None:
        rows = self.query(
            "SELECT * FROM change_status WHERE request_id = $request_id",
            request_id=request_id,
        )
        return rows[0] if rows else None

    # -- reads used by the nightly job ---------------------------------------

    def expiry_candidates(self) -> list[dict]:
        return self.query(
            "SELECT product_id, epd_code, prod_name, expiry_date, expiry_state "
            "FROM epd_expiry_status "
            "WHERE expiry_state IN ('expired', 'expiring within 90 days')"
        )

    def expired_product_ids(self) -> set[int]:
        return {
            r["product_id"]
            for r in self.query(
                "SELECT DISTINCT product_id FROM epd_current WHERE status = 'expired'"
            )
        }

    def decision_stats(self, window_start: datetime) -> dict:
        # count_if and quantile_cont in place of BigQuery's COUNTIF and
        # APPROX_QUANTILES(...)[OFFSET(95)].
        rows = self.query(
            """
            SELECT
              COUNT(*) AS total,
              count_if(action = 'applied') AS applied,
              count_if(action = 'rejected') AS rejected,
              count_if(action = 'pending_review') AS pending,
              count_if(model IS NOT NULL) AS model_calls,
              AVG(confidence) AS mean_confidence,
              quantile_cont(latency_ms, 0.50) AS p50_latency_ms,
              quantile_cont(latency_ms, 0.95) AS p95_latency_ms,
              SUM(cost_usd) AS total_cost
            FROM change_events
            WHERE occurred_at >= $window_start AND event_type = 'decided'
            """,
            window_start=window_start,
        )
        return rows[0] if rows else {}

    def overdue_reviews(self, hours: int) -> int:
        rows = self.query(
            "SELECT COUNT(*) AS n FROM review_queue WHERE hours_waiting > $hours",
            hours=hours,
        )
        return int(rows[0]["n"]) if rows else 0

    def epd_count(self) -> int:
        rows = self.query("SELECT COUNT(*) AS n FROM epd_current")
        return int(rows[0]["n"]) if rows else 0

    def latest_reconciliation(self) -> dict | None:
        rows = self.query(
            "SELECT * FROM reconciliation_runs ORDER BY run_at DESC LIMIT 1"
        )
        return rows[0] if rows else None

    def agents_needing_attention(self) -> list[dict]:
        return self.query("SELECT * FROM agent_registry_attention")

    # -- writes --------------------------------------------------------------

    def _insert(self, table: str, row: dict) -> None:
        columns = list(row)
        placeholders = ", ".join(f"${c}" for c in columns)
        try:
            self.conn.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                row,
            )
        except duckdb.Error as exc:
            raise InsertError(f"{table}: {exc}") from exc

    def save_request(self, request: Any) -> None:
        self._insert("change_requests", request_row(request))

    def save_event(
        self,
        decision: Any,
        product_id: int,
        event_type: str = "decided",
        actor: str = "pipeline",
        triage_outcome: Any = None,
        occurred_at: str = "",
    ) -> str:
        event_id = str(uuid.uuid4())
        row = event_row(
            decision, product_id, event_id, event_type, actor, triage_outcome,
            occurred_at,
        )
        # The BigQuery table has the model columns whether or not they are sent;
        # an INSERT has to name every column it is not supplying.
        row.setdefault("model", None)
        row.setdefault("prompt_tokens", None)
        row.setdefault("completion_tokens", None)
        row.setdefault("cost_usd", None)
        row.setdefault("latency_ms", None)
        self._insert("change_events", row)
        return event_id

    def save_version(
        self,
        record: dict,
        product_id: int,
        version: int,
        change_request_id: str | None,
        status: str = "active",
    ) -> None:
        self._insert(
            "epd_records",
            version_row(record, product_id, version, change_request_id, status),
        )

    def save_reconciliation(self, row: dict) -> None:
        self._insert("reconciliation_runs", row)
