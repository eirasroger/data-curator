"""Tests for the nightly job, with a fake store.

The job's whole value is what it notices, so the tests are about what it
notices. A fake store means these run in milliseconds with no cloud and no
credentials, and lets us construct situations - a review forgotten for two
weeks, a night where the model lost its nerve - that would be tedious to arrange
against real data.
"""

from __future__ import annotations

from datetime import UTC, datetime

from domain import reconcile


class FakeStore:
    """Returns whatever the test set up, one method at a time.

    tests/test_store.py covers the real queries against a real database. What
    these tests need is the ability to construct a situation - a review
    forgotten for two weeks, a night where the model lost its nerve - so the
    store is only a source of answers here.
    """

    def __init__(self, **results) -> None:
        self.results = results

    def expiry_candidates(self) -> list[dict]:
        return self.results.get("expiry", [])

    def expired_product_ids(self) -> set[int]:
        return {r["product_id"] for r in self.results.get("already_expired", [])}

    def decision_stats(self, window_start) -> dict:
        return self.results.get("stats", {})

    def overdue_reviews(self, hours: int) -> int:
        return self.results.get("overdue", 0)

    def epd_count(self) -> int:
        return self.results.get("epd_count", 63)


def run(store, **kw):
    published: list = []
    summary = reconcile.run(
        store, published.append,
        now=datetime(2026, 9, 4, 2, 0, tzinfo=UTC), **kw
    )
    return summary, published


def test_quiet_night_needs_no_attention():
    store = FakeStore(stats={"total": 4, "applied": 3, "rejected": 1,
                             "pending": 0, "model_calls": 2,
                             "mean_confidence": 0.91, "p95_latency_ms": 5100,
                             "total_cost": 0.0018})
    summary, published = run(store)
    assert summary["needs_attention"] is False
    assert summary["drift_flags"] == []
    assert published == []


def test_expired_epd_raises_a_change_request():
    """The job does not edit the record. It asks, like anyone else would."""
    store = FakeStore(expiry=[
        {"product_id": 5, "epd_code": "S-P-01848", "prod_name": "FKD-S Product Range",
         "expiry_date": "2025-04-29", "expiry_state": "expired"},
    ])
    summary, published = run(store)

    assert summary["expiry_requests_raised"] == 1
    assert len(published) == 1
    request = published[0]
    assert request.product_id == 5
    assert request.kind.value == "expiry"
    assert request.source.value == "scheduler"
    assert "2025-04-29" in request.reason
    assert summary["needs_attention"] is True


def test_an_already_expired_record_is_not_raised_again():
    """Without this the job would re-raise the same expiry every single night."""
    store = FakeStore(
        expiry=[{"product_id": 5, "epd_code": "x", "prod_name": "y",
                 "expiry_date": "2025-04-29", "expiry_state": "expired"}],
        already_expired=[{"product_id": 5}],
    )
    summary, published = run(store)
    assert published == []
    assert summary["expiry_requests_raised"] == 0


def test_upcoming_expiry_is_reported_but_not_acted_on():
    store = FakeStore(expiry=[
        {"product_id": 20, "epd_code": "S-P-04767", "prod_name": "Ready Mixed Concrete",
         "expiry_date": "2026-10-03", "expiry_state": "expiring within 90 days"},
    ])
    summary, published = run(store)
    assert published == [], "not expired yet - nothing to change"
    assert summary["epds_expiring_90d"] == 1
    assert any("90 days" in f for f in summary["drift_flags"])


def test_high_rejection_rate_is_flagged():
    store = FakeStore(stats={"total": 20, "applied": 4, "rejected": 15,
                             "pending": 1, "model_calls": 8,
                             "mean_confidence": 0.88, "p95_latency_ms": 5000,
                             "total_cost": 0.007})
    summary, _ = run(store)
    assert summary["needs_attention"] is True
    assert any("rejection rate" in f for f in summary["drift_flags"])


def test_a_small_sample_is_not_treated_as_drift():
    """Two rejections out of three is 67%, and means nothing."""
    store = FakeStore(stats={"total": 3, "applied": 1, "rejected": 2, "pending": 0,
                             "model_calls": 1, "mean_confidence": 0.9,
                             "p95_latency_ms": 4000, "total_cost": 0.001})
    summary, _ = run(store)
    assert not any("rejection rate" in f for f in summary["drift_flags"])


def test_sagging_confidence_is_flagged():
    """Drift that raises no errors: everything succeeds, the model is just unsure."""
    store = FakeStore(stats={"total": 10, "applied": 2, "rejected": 1, "pending": 7,
                             "model_calls": 10, "mean_confidence": 0.41,
                             "p95_latency_ms": 6000, "total_cost": 0.009})
    summary, _ = run(store)
    assert summary["needs_attention"] is True
    assert any("confidence" in f for f in summary["drift_flags"])


def test_forgotten_reviews_are_flagged():
    store = FakeStore(overdue=3)
    summary, _ = run(store)
    assert summary["reviews_overdue"] == 3
    assert any("more than 7 days" in f for f in summary["drift_flags"])
