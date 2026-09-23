"""Tests for the dashboard: metrics, review actions and the static export."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from domain import metrics, pipeline, reconcile
from domain.changes import Action, ChangeKind, ChangeRequest, Source
from domain.providers.stub import StubTriager
from domain.store import get_store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(all_records):
    s = get_store("duckdb", path=":memory:")
    for rec in all_records:
        s.save_version(rec, rec["product_id"], 1, None, "active")
    yield s
    s.close()


def park_one(store, request_id: str = "req-1") -> ChangeRequest:
    """A change the pipeline sends to a person: a published figure."""
    request = ChangeRequest(
        request_id=request_id, product_id=6, kind=ChangeKind.FIELD_UPDATE,
        source=Source.HUMAN, submitted_by="tester",
        field_path="epd_code", new_value="EPD-NEW-0001",
        reason="The registration number was reissued.",
    )
    record = store.current_record(6)
    outcome = pipeline.decide(record, request, StubTriager())
    assert outcome.decision.action is Action.PENDING_REVIEW
    store.save_request(request)
    store.save_event(outcome.decision, 6, triage_outcome=outcome.triage)
    return request


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def test_the_sla_matches_the_nightly_job():
    """The dashboard and the nightly job use the same review SLA."""
    assert metrics.REVIEW_SLA_DAYS == reconcile.REVIEW_SLA_DAYS


def test_an_empty_system_reports_zeroes_not_errors(store):
    o = metrics.overview(store)
    assert o.queue_depth == 0
    assert o.today.total == 0
    assert o.today.rules_share == 0.0
    assert o.today.cost_per_decision == 0.0
    assert o.reconciliation is None
    assert o.needs_attention is False


def test_queue_depth_is_the_whole_queue_not_the_page(store):
    for i in range(8):
        park_one(store, f"req-{i}")
    o = metrics.overview(store, queue_limit=3)
    assert len(o.queue) == 3
    assert o.queue_depth == 8


def test_windows_widen(store):
    park_one(store)
    o = metrics.overview(store)
    assert [w.label for w in o.windows] == ["today", "7 days", "30 days"]
    assert o.windows[0].total <= o.windows[1].total <= o.windows[2].total


def test_rules_and_model_split_adds_up(store):
    park_one(store)
    w = metrics.overview(store).today
    assert w.settled_by_rules + w.model_calls == w.total


def test_a_nightly_run_shows_up(store):
    summary = reconcile.run(store, lambda r: None)
    store.save_reconciliation(summary)
    o = metrics.overview(store)
    assert o.reconciliation is not None
    assert o.needs_attention == bool(summary["needs_attention"])


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------

@pytest.fixture
def client(store, monkeypatch, tmp_path):
    """The service, wired to the in-memory store and no Pub/Sub."""
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.setenv("STORE_BACKEND", "duckdb")

    spec = importlib.util.spec_from_file_location(
        "dashboard_service", ROOT / "services" / "dashboard" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["dashboard_service"] = module
    spec.loader.exec_module(module)
    module._store = store
    module.PROJECT = ""
    yield TestClient(module.app), module
    sys.modules.pop("dashboard_service", None)


def test_the_page_renders_with_nothing_in_it(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "Nothing waiting" in r.text


def test_the_page_shows_a_parked_change(client, store):
    c, _ = client
    park_one(store)
    r = c.get("/")
    assert "epd_code" in r.text
    assert "EPD-NEW-0001" in r.text
    # The pipeline's reason is shown with each queued item.
    assert "published figure" in r.text


def test_health_names_the_backend(client):
    c, _ = client
    assert c.get("/health").json()["backend"] == "DuckDBStore"


def test_approving_applies_the_change_and_logs_who_did_it(client, store):
    c, _ = client
    request = park_one(store)
    before = store.current_record(6)["_version"]

    assert c.post(f"/reviews/{request.request_id}/approve").status_code == 200

    assert store.review_queue_depth() == 0
    assert store.current_record(6)["_version"] == before + 1
    assert store.current_record(6)["epd_code"] == "EPD-NEW-0001"

    events = store.query(
        "SELECT event_type, action, actor FROM change_events "
        "WHERE request_id = $rid ORDER BY occurred_at",
        rid=request.request_id,
    )
    # Both the pipeline's decision and the review are recorded.
    assert [e["event_type"] for e in events] == ["decided", "reviewed"]
    assert events[0]["action"] == "pending_review"
    assert events[1]["action"] == "applied"
    assert events[1]["actor"] == "dashboard-user"


def test_rejecting_leaves_the_record_alone(client, store):
    c, _ = client
    request = park_one(store)
    before = store.current_record(6)["_version"]

    assert c.post(f"/reviews/{request.request_id}/reject").status_code == 200

    assert store.review_queue_depth() == 0
    assert store.current_record(6)["_version"] == before
    assert store.current_record(6)["epd_code"] != "EPD-NEW-0001"


def test_a_review_for_an_unknown_request_does_not_crash_the_page(client):
    c, _ = client
    assert c.post("/reviews/no-such-request/approve").status_code == 200


def test_a_second_approval_does_not_write_a_second_version(client, store):
    """A duplicate approval applies the change only once."""
    c, _ = client
    request = park_one(store)
    before = store.current_record(6)["_version"]

    assert c.post(f"/reviews/{request.request_id}/approve").status_code == 200
    after_first = store.current_record(6)["_version"]
    assert after_first == before + 1

    assert c.post(f"/reviews/{request.request_id}/approve").status_code == 200
    assert store.current_record(6)["_version"] == after_first

    events = store.query(
        "SELECT event_type FROM change_events WHERE request_id = $rid",
        rid=request.request_id,
    )
    assert [e["event_type"] for e in events] == ["decided", "reviewed"]


def test_approving_a_rejected_change_does_not_apply_it(client, store):
    """An approval cannot apply a change the rules already rejected."""
    c, _ = client
    record = store.current_record(6)
    request = ChangeRequest(
        request_id="req-rejected", product_id=6, kind=ChangeKind.FIELD_UPDATE,
        source=Source.HUMAN, submitted_by="tester",
        # This density contradicts thickness and the conversion ratio.
        field_path="density", new_value=999999.0,
        reason="Read it off a different datasheet.",
    )
    outcome = pipeline.decide(record, request, StubTriager())
    assert outcome.decision.action is Action.REJECTED
    store.save_request(request)
    store.save_event(outcome.decision, 6, triage_outcome=outcome.triage)

    before = store.current_record(6)["_version"]
    assert c.post(f"/reviews/{request.request_id}/approve").status_code == 200

    assert store.current_record(6)["_version"] == before
    assert store.current_record(6)["density"] != 999999.0


def test_a_review_outcome_says_which_case_it_was(store):
    """Each review outcome maps to a distinct result."""
    from domain.changes import ReviewDecision
    from domain.pipeline import ReviewOutcome

    def verdict(request_id: str) -> ReviewOutcome:
        return pipeline.apply_review(
            ReviewDecision(request_id=request_id, reviewer="tester", approve=True),
            store=store,
        )

    assert verdict("no-such-request") is ReviewOutcome.UNKNOWN_REQUEST

    request = park_one(store)
    assert verdict(request.request_id) is ReviewOutcome.APPLIED
    assert verdict(request.request_id) is ReviewOutcome.NOT_PENDING


# ---------------------------------------------------------------------------
# the public snapshot
# ---------------------------------------------------------------------------

def test_the_static_export_renders_the_same_templates(store, tmp_path):
    """The static export renders the same templates as the live dashboard."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    park_one(store)
    env = Environment(
        loader=FileSystemLoader(
            str(ROOT / "services" / "dashboard" / "templates")
        ),
        autoescape=select_autoescape(["html"]),
    )
    html = env.get_template("base.html").render(
        overview=metrics.overview(store), static=True,
        sla_days=metrics.REVIEW_SLA_DAYS,
    )

    assert "Demo snapshot" in html
    assert "epd_code" in html
    # The static page makes no server calls.
    assert "hx-post" not in html
    assert "htmx" not in html
    assert 'data-act="approve"' in html


def test_the_live_page_is_not_marked_as_a_demo(client, store):
    c, _ = client
    park_one(store)
    html = c.get("/").text
    assert "Demo snapshot" not in html
    assert "hx-post" in html


def test_overdue_counts_only_what_is_actually_old(store):
    park_one(store)
    o = metrics.overview(store)
    assert o.queue_depth == 1
    assert o.overdue == 0  # submitted seconds ago

    # Viewed 30 days later, the change is still queued.
    later = datetime.now(UTC) + timedelta(days=30)
    assert metrics.overview(store, now=later).queue_depth == 1
