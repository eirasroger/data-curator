"""The nightly job: raise expiry requests, flag drift and overdue reviews.

Expired EPDs are submitted as change requests, so they follow the normal pipeline.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from .changes import ChangeKind, ChangeRequest, Source
from .store import Store

REVIEW_SLA_DAYS = 7

# Drift thresholds, kept blunt to avoid false alarms.
MIN_SAMPLE = 5              # minimum decisions before a rate counts
HIGH_REJECTION_RATE = 0.60
LOW_MEAN_CONFIDENCE = 0.60


def run(
    store: Store,
    publish: Callable[[ChangeRequest], None],
    window_hours: int = 24,
    now: datetime | None = None,
    today: date | None = None,
) -> dict:
    """Run the checks and return the summary row. Publish and clock are injectable."""
    now = now or datetime.now(UTC)
    today = today or now.date()
    window_start = now - timedelta(hours=window_hours)
    run_id = str(uuid.uuid4())

    flags: list[str] = []

    # --- expiry -------------------------------------------------------------
    expiry_rows = store.expiry_candidates()
    expired = [r for r in expiry_rows if r["expiry_state"] == "expired"]
    expiring_soon = [r for r in expiry_rows if r["expiry_state"] != "expired"]

    # Skip records already marked expired, so each expiry is raised once.
    already = store.expired_product_ids()
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
    s = store.decision_stats(window_start)
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
    overdue_n = store.overdue_reviews(REVIEW_SLA_DAYS * 24)
    if overdue_n:
        flags.append(
            f"{overdue_n} change(s) waiting on a person for more than "
            f"{REVIEW_SLA_DAYS} days"
        )

    total_epds = store.epd_count()

    return {
        "run_id": run_id,
        "run_at": now.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        "epds_total": total_epds,
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
