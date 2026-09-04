"""The nightly check.

It asks two different questions, and they fail in different ways.

  Did anything BREAK?   Requests stuck pending, reviews nobody has touched,
                        EPDs that expired while we were not looking. These are
                        visible if you go and look, and nobody goes and looks.

  Did anything DRIFT?   The pipeline still returns 204 on every message, and is
                        quietly behaving differently than it did last week -
                        rejecting more, less sure of itself, costing more per
                        decision. Drift raises no errors. That is precisely why
                        something has to go looking on a schedule.

Expiry is the interesting part. When this job finds an EPD whose validity has
lapsed it does not edit the record. It submits a change request, exactly like a
person would, and lets the same pipeline handle it. That is what makes the three
sources - a person, a manufacturer, the calendar - one system instead of three.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .changes import ChangeKind, ChangeRequest, Source

# A change request pending longer than this is not "in progress", it is forgotten.
REVIEW_SLA_DAYS = 7

# Thresholds for calling something drift. Deliberately blunt: a nightly job that
# cries wolf gets ignored, which is worse than not having one.
MIN_SAMPLE = 5              # below this, any rate is noise
HIGH_REJECTION_RATE = 0.60
LOW_MEAN_CONFIDENCE = 0.60


def run(
    store: Any,
    publish: Callable[[ChangeRequest], None],
    window_hours: int = 24,
    now: Optional[datetime] = None,
    today: Optional[date] = None,
) -> dict:
    """Do the checks and return the row describing this run.

    `publish` and the clock are passed in rather than reached for, so this can
    be tested end to end without a scheduler, a topic, or waiting until midnight.
    """
    now = now or datetime.now(timezone.utc)
    today = today or now.date()
    window_start = now - timedelta(hours=window_hours)
    run_id = str(uuid.uuid4())

    flags: list[str] = []

    # --- expiry -------------------------------------------------------------
    expiry_rows = store.query(
        f"SELECT product_id, epd_code, prod_name, expiry_date, expiry_state "
        f"FROM `{store.table('epd_expiry_status')}` "
        f"WHERE expiry_state IN ('expired', 'expiring within 90 days')"
    )
    expired = [r for r in expiry_rows if r["expiry_state"] == "expired"]
    expiring_soon = [r for r in expiry_rows if r["expiry_state"] != "expired"]

    # Only for records not already marked expired, so this cannot loop: applying
    # the request sets status='expired', and the next run skips it.
    already = {
        r["product_id"] for r in store.query(
            f"SELECT DISTINCT product_id FROM `{store.table('epd_current')}` "
            f"WHERE status = 'expired'"
        )
    }
    raised = 0
    for row in expired:
        if row["product_id"] in already:
            continue
        publish(ChangeRequest(
            request_id=str(uuid.uuid4()),
            product_id=row["product_id"],
            kind=ChangeKind.EXPIRY,
            source=Source.SCHEDULER,
            submitted_by="reconcile-job",
            reason=(
                f"Validity lapsed on {row['expiry_date']}. This EPD may no "
                f"longer be cited."
            ),
        ))
        raised += 1

    if raised:
        flags.append(f"{raised} EPD(s) expired and were flagged this run")
    if expiring_soon:
        flags.append(f"{len(expiring_soon)} EPD(s) expire within 90 days")

    # --- throughput in the window -------------------------------------------
    stats = store.query(
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
        FROM `{store.table('change_events')}`
        WHERE occurred_at >= @window_start AND event_type = 'decided'
        """,
        window_start=window_start.isoformat(),
    )
    s = stats[0] if stats else {}
    total = int(s.get("total") or 0)
    rejected = int(s.get("rejected") or 0)
    mean_conf = s.get("mean_confidence")

    if total >= MIN_SAMPLE and rejected / total > HIGH_REJECTION_RATE:
        flags.append(
            f"rejection rate {rejected / total:.0%} over {total} requests - "
            f"either the data got worse or a rule got stricter"
        )
    if mean_conf is not None and float(mean_conf) < LOW_MEAN_CONFIDENCE:
        flags.append(
            f"mean model confidence {float(mean_conf):.2f} - the model is less "
            f"sure than usual, which shows up as more human review, not as errors"
        )

    # --- forgotten reviews ---------------------------------------------------
    overdue = store.query(
        f"SELECT COUNT(*) AS n FROM `{store.table('review_queue')}` "
        f"WHERE hours_waiting > @hours",
        hours=REVIEW_SLA_DAYS * 24,
    )
    overdue_n = int(overdue[0]["n"]) if overdue else 0
    if overdue_n:
        flags.append(
            f"{overdue_n} change(s) waiting on a person for more than "
            f"{REVIEW_SLA_DAYS} days"
        )

    total_epds = store.query(
        f"SELECT COUNT(*) AS n FROM `{store.table('epd_current')}`"
    )

    return {
        "run_id": run_id,
        "run_at": now.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        "epds_total": int(total_epds[0]["n"]) if total_epds else 0,
        "epds_expired": len(expired),
        "epds_expiring_90d": len(expiring_soon),
        "expiry_requests_raised": raised,
        "requests_total": total,
        "requests_applied": int(s.get("applied") or 0),
        "requests_rejected": rejected,
        "requests_pending": int(s.get("pending") or 0),
        "reviews_overdue": overdue_n,
        "model_calls": int(s.get("model_calls") or 0),
        "mean_confidence": float(mean_conf) if mean_conf is not None else None,
        "p95_latency_ms": int(s.get("p95_latency_ms") or 0),
        "total_cost_usd": float(s.get("total_cost") or 0.0),
        "drift_flags": flags,
        "needs_attention": bool(flags),
    }
