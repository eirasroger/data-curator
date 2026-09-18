"""What the operator page shows, assembled from the store.

Composed from methods the `Store` protocol already had rather than new SQL, so
both backends serve the dashboard without either one growing a second dialect.

Every block carries the window it covers and the moment it was read. A page that
cannot say how old its numbers are invites someone to treat last night's
reconciliation as this minute's state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# A change waiting longer than this is not in progress, it is forgotten. Same
# figure the nightly job uses; imported rather than repeated would be circular,
# so it is asserted equal in the tests instead.
REVIEW_SLA_DAYS = 7


@dataclass
class Window:
    """One period's throughput, cost and latency."""

    label: str
    total: int = 0
    applied: int = 0
    rejected: int = 0
    pending: int = 0
    model_calls: int = 0
    cost_usd: float = 0.0
    p50_latency_ms: int = 0
    p95_latency_ms: int = 0
    mean_confidence: float | None = None

    @property
    def settled_by_rules(self) -> int:
        return self.total - self.model_calls

    @property
    def rules_share(self) -> float:
        return self.settled_by_rules / self.total if self.total else 0.0

    @property
    def cost_per_decision(self) -> float:
        return self.cost_usd / self.total if self.total else 0.0


@dataclass
class Overview:
    as_of: datetime
    windows: list[Window] = field(default_factory=list)
    queue: list[dict] = field(default_factory=list)
    # The whole queue, not the page of it being shown.
    queue_depth: int = 0
    overdue: int = 0
    reconciliation: dict | None = None
    agents: list[dict] = field(default_factory=list)
    epd_count: int = 0

    @property
    def today(self) -> Window:
        return self.windows[0]

    @property
    def needs_attention(self) -> bool:
        if self.overdue:
            return True
        if self.agents:
            return True
        return bool((self.reconciliation or {}).get("needs_attention"))


def _window(store: Any, label: str, since: datetime) -> Window:
    stats = store.decision_stats(since) or {}

    def num(key: str, default: float = 0) -> float:
        value = stats.get(key)
        return default if value is None else float(value)

    return Window(
        label=label,
        total=int(num("total")),
        applied=int(num("applied")),
        rejected=int(num("rejected")),
        pending=int(num("pending")),
        model_calls=int(num("model_calls")),
        cost_usd=num("total_cost"),
        p50_latency_ms=int(num("p50_latency_ms")),
        p95_latency_ms=int(num("p95_latency_ms")),
        mean_confidence=(
            None if stats.get("mean_confidence") is None
            else float(stats["mean_confidence"])
        ),
    )


def overview(store: Any, now: datetime | None = None, queue_limit: int = 50) -> Overview:
    """Everything the dashboard renders, in one pass over the store.

    The clock is injectable for the same reason it is in reconcile.run(): a page
    that can only be tested by waiting until tomorrow is a page nobody tests.
    """
    now = now or datetime.now(UTC)

    queue = store.review_queue(limit=queue_limit)
    return Overview(
        as_of=now,
        windows=[
            _window(store, "today", now - timedelta(days=1)),
            _window(store, "7 days", now - timedelta(days=7)),
            _window(store, "30 days", now - timedelta(days=30)),
        ],
        queue=queue,
        queue_depth=store.review_queue_depth(),
        overdue=store.overdue_reviews(REVIEW_SLA_DAYS * 24),
        reconciliation=store.latest_reconciliation(),
        agents=store.agents_needing_attention(),
        epd_count=store.epd_count(),
    )
