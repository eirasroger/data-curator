"""The DuckDB store, exercised through the same calls the worker makes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from domain import pipeline, reconcile
from domain.changes import Action, ChangeKind, ChangeRequest, Source
from domain.providers.stub import StubTriager
from domain.store import get_store, to_change_request


@pytest.fixture
def store(all_records):
    """An in-memory database holding the real corpus at version 1."""
    s = get_store("duckdb", path=":memory:")
    for rec in all_records:
        s.save_version(rec, rec["product_id"], 1, None, "active")
    yield s
    s.close()


def request_for(product_id: int, **kw) -> ChangeRequest:
    defaults = dict(
        request_id=f"req-{product_id}",
        product_id=product_id,
        kind=ChangeKind.FIELD_UPDATE,
        source=Source.HUMAN,
        submitted_by="tester",
        reason="because",
    )
    return ChangeRequest(**{**defaults, **kw})


def test_unknown_backend_is_refused():
    with pytest.raises(ValueError, match="unknown store backend"):
        get_store("postgres")


def test_the_corpus_loads(store, all_records):
    assert store.epd_count() == len(all_records)


def test_a_record_comes_back_as_the_extractor_shaped_it(store, records_by_id):
    record = store.current_record(6)
    assert record is not None
    assert record["_version"] == 1
    assert record["prod_name"] == records_by_id[6]["prod_name"]
    # The JSON column round-trips to a dict, not to a string.
    assert isinstance(record["impacts"], dict)


def test_an_unknown_product_is_none(store):
    assert store.current_record(999_999) is None


def test_a_request_round_trips(store):
    request = request_for(6, field_path="density", new_value=123.0)
    store.save_request(request)

    row = store.get_request("req-6")
    assert row is not None
    rebuilt = to_change_request(row)
    assert rebuilt.request_id == request.request_id
    assert rebuilt.field_path == "density"
    # new_value is stored as JSON, so the float has to survive as a float.
    assert rebuilt.new_value == 123.0


def test_a_decision_is_recorded_once_and_seen_as_decided(store):
    request = request_for(6, field_path="density", new_value=123.0)
    record = store.current_record(6)
    outcome = pipeline.decide(record, request, StubTriager())

    assert store.already_decided(request.request_id) is False
    store.save_request(request)
    store.save_event(outcome.decision, request.product_id)
    assert store.already_decided(request.request_id) is True


def test_model_columns_stay_null_when_the_rules_settled_it(store):
    """A rules-only decision must not look like a model call in the data."""
    request = request_for(6, field_path="density", new_value=123.0)
    record = store.current_record(6)
    outcome = pipeline.decide(record, request, StubTriager())
    store.save_request(request)
    store.save_event(outcome.decision, request.product_id, triage_outcome=outcome.triage)

    rows = store.query("SELECT model, cost_usd, latency_ms FROM change_events")
    assert len(rows) == 1
    if not outcome.used_model:
        assert rows[0]["model"] is None
        assert rows[0]["cost_usd"] is None


def test_applying_a_change_writes_a_new_version_and_keeps_the_old(store):
    request = request_for(6, field_path="lifespan", new_value=55.0)
    record = store.current_record(6)
    outcome = pipeline.decide(record, request, StubTriager())
    new_record = pipeline.apply_decision(record, request, outcome.decision)
    store.save_request(request)
    store.save_event(outcome.decision, request.product_id)
    store.save_version(new_record, 6, 2, request.request_id)

    assert store.current_record(6)["_version"] == 2
    versions = store.query(
        "SELECT version FROM epd_records WHERE product_id = 6 ORDER BY version"
    )
    assert [v["version"] for v in versions] == [1, 2]


def test_a_parked_change_appears_in_the_review_queue(store):
    # epd_code is a published figure, so it is reviewed however confident the
    # model is. A number like gwp_total would be rejected earlier by the
    # arithmetic check and never reach that rule.
    request = request_for(6, field_path="epd_code", new_value="EPD-NEW-0001")
    record = store.current_record(6)
    outcome = pipeline.decide(record, request, StubTriager())
    assert outcome.decision.action is Action.PENDING_REVIEW

    store.save_request(request)
    store.save_event(outcome.decision, request.product_id)

    queue = store.review_queue()
    assert [r["request_id"] for r in queue] == ["req-6"]
    assert queue[0]["field_path"] == "epd_code"
    assert queue[0]["hours_waiting"] >= 0


def test_change_status_shows_the_latest_event(store):
    request = request_for(6, field_path="epd_code", new_value="EPD-NEW-0001")
    record = store.current_record(6)
    outcome = pipeline.decide(record, request, StubTriager())
    store.save_request(request)
    store.save_event(outcome.decision, request.product_id)

    status = store.change_status("req-6")
    assert status is not None
    assert status["action"] == "pending_review"
    assert status["event_type"] == "decided"


def test_blocking_issues_survive_as_a_list(store):
    """A rejected change carries its validation errors; they are an array column."""
    request = request_for(6, field_path="impacts.gwp_fossil", new_value=1210.0)
    record = store.current_record(6)
    outcome = pipeline.decide(record, request, StubTriager())
    store.save_request(request)
    store.save_event(outcome.decision, request.product_id)

    row = store.query("SELECT blocking_issues FROM change_events")[0]
    assert isinstance(row["blocking_issues"], list)


def test_decision_stats_counts_the_window(store):
    for i, pid in enumerate([5, 6, 7]):
        request = request_for(pid, request_id=f"r{i}",
                              field_path="impacts.gwp_total", new_value=99.0)
        record = store.current_record(pid)
        outcome = pipeline.decide(record, request, StubTriager())
        store.save_request(request)
        store.save_event(outcome.decision, pid)

    stats = store.decision_stats(datetime.now(UTC) - timedelta(hours=24))
    assert stats["total"] == 3
    # A gwp_total change is parked on one record and rejected on the others by
    # the arithmetic check, so the split is not the point - the accounting is.
    assert stats["applied"] + stats["rejected"] + stats["pending"] == 3

    # A window that ended before any of this happened sees nothing.
    empty = store.decision_stats(datetime.now(UTC) + timedelta(hours=1))
    assert empty["total"] == 0


def test_overdue_reviews_needs_time_to_pass(store):
    request = request_for(6, field_path="epd_code", new_value="EPD-NEW-0001")
    record = store.current_record(6)
    outcome = pipeline.decide(record, request, StubTriager())
    store.save_request(request)
    store.save_event(outcome.decision, request.product_id)

    assert store.overdue_reviews(hours=0) == 0  # submitted seconds ago
    assert store.overdue_reviews(hours=24 * 7) == 0


def test_the_nightly_job_runs_against_the_real_store(store):
    """reconcile.run end to end, with no fake anywhere."""
    published: list = []
    summary = reconcile.run(store, published.append)

    assert summary["epds_total"] == store.epd_count()
    # The corpus contains records past their date, and none is marked expired
    # yet, so the job should raise a request for each.
    assert summary["epds_expired"] == len(published)
    assert all(r.kind is ChangeKind.EXPIRY for r in published)

    store.save_reconciliation(summary)
    assert store.query("SELECT COUNT(*) AS n FROM reconciliation_runs")[0]["n"] == 1


def test_an_expiry_already_applied_is_not_raised_again(store):
    published: list = []
    first = reconcile.run(store, published.append)
    assert first["expiry_requests_raised"] > 0

    # Apply them, the way the worker would.
    for request in published:
        record = store.current_record(request.product_id)
        outcome = pipeline.decide(record, request, StubTriager())
        new_record = pipeline.apply_decision(record, request, outcome.decision)
        store.save_version(new_record, request.product_id,
                           record["_version"] + 1, request.request_id, "expired")

    again: list = []
    second = reconcile.run(store, again.append)
    assert second["expiry_requests_raised"] == 0
    assert again == []


def test_a_record_stored_with_non_ascii_survives(store):
    record = store.current_record(6)
    record["prod_name"] = "Dämmplatte ÖKOBAUDAT — 20 °C"
    store.save_version(record, 6, 2, "req-x")

    back = store.current_record(6)
    assert back["prod_name"] == "Dämmplatte ÖKOBAUDAT — 20 °C"
    raw = store.query("SELECT record FROM epd_records WHERE version = 2")[0]["record"]
    assert json.loads(raw)["prod_name"] == "Dämmplatte ÖKOBAUDAT — 20 °C"
