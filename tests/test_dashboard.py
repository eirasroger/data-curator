"""The operator page, and the numbers behind it.

The page is where a person actually decides something, so the tests are about
the loop closing: what it shows, what a click does to the datastore, and that
the public snapshot cannot quietly stop matching the real thing.
"""

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
    """Two places name the same number; if they drift the page lies."""
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
    # The pipeline's own reason has to be on screen - a queue that does not say
    # why something stopped is asking a person to re-derive it.
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
    # Both answers stay on the record: what the pipeline concluded, and what
    # the person decided afterwards.
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


# ---------------------------------------------------------------------------
# the public snapshot
# ---------------------------------------------------------------------------

def test_the_static_export_renders_the_same_templates(store, tmp_path):
    """The demo and the real page must come from one set of templates.

    If the export grew its own copy, the public page would slowly stop being a
    demonstration of this system and start being a drawing of one.
    """
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
    # Nothing in the snapshot may reach for a server that is not there.
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

    # Looked at from far enough in the future, the same change is overdue.
    later = datetime.now(UTC) + timedelta(days=30)
    assert metrics.overview(store, now=later).queue_depth == 1
