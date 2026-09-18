"""The proposal simulator.

What matters about generated data is that it is reproducible and that it is not
secretly rigged. A history nobody can regenerate is an anecdote, and a
stationary mix that quietly shifts over time would manufacture the drift the
analysis is meant to look for.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from domain import pipeline
from domain.changes import ChangeRequest
from domain.providers.stub import StubTriager
from domain.store import get_store
from sim import corpus
from sim.generate import BASE_MIX, DRIFT_MIX, Generator, generate

END = datetime(2026, 9, 18, tzinfo=UTC)


def fingerprint(proposals) -> list[tuple]:
    return [
        (p.request.request_id, p.request.field_path, str(p.request.new_value),
         p.request.submitted_at, p.generator)
        for p in proposals
    ]


def test_the_same_seed_gives_the_same_history():
    a = generate(200, 90, seed=1234, end=END)
    b = generate(200, 90, seed=1234, end=END)
    assert fingerprint(a) == fingerprint(b)


def test_a_different_seed_gives_a_different_history():
    a = generate(200, 90, seed=1234, end=END)
    b = generate(200, 90, seed=5678, end=END)
    assert fingerprint(a) != fingerprint(b)


def test_proposals_arrive_in_order_and_inside_the_window():
    proposals = generate(300, 30, seed=1, end=END)
    times = [datetime.fromisoformat(p.request.submitted_at) for p in proposals]
    assert times == sorted(times)
    assert times[0] >= END - timedelta(days=30)
    assert times[-1] <= END


def test_every_shape_in_the_mix_can_actually_be_built():
    """A weight pointing at a method that always declines is a silent hole."""
    records = corpus.load_records()
    import random

    gen = Generator(records, random.Random(0))
    for name in set(BASE_MIX) | set(DRIFT_MIX):
        built = [getattr(gen, name)() for _ in range(25)]
        assert any(p is not None for p in built), f"{name} never produced anything"


def test_proposals_are_built_from_real_records():
    records = corpus.load_records()
    for p in generate(300, 90, seed=7, end=END):
        assert p.request.product_id in records
        assert isinstance(p.request, ChangeRequest)


def test_the_default_mix_does_not_move_over_time():
    """The stationary run has to be genuinely stationary.

    If the blend of proposal shapes shifted on its own, any drift the analysis
    found would be drift that was planted.
    """
    proposals = generate(3000, 90, seed=42, end=END)
    third = len(proposals) // 3
    first = Counter(p.generator for p in proposals[:third])
    last = Counter(p.generator for p in proposals[-third:])

    for name in BASE_MIX:
        a = first[name] / third
        b = last[name] / third
        assert abs(a - b) < 0.04, f"{name} moved from {a:.3f} to {b:.3f}"


def test_the_drift_option_does_move_the_mix():
    proposals = generate(3000, 90, seed=42, end=END, drift=True)
    third = len(proposals) // 3
    first = Counter(p.generator for p in proposals[:third])
    last = Counter(p.generator for p in proposals[-third:])

    # bad_density is weighted up sharply in the drifted mix.
    assert last["bad_density"] > first["bad_density"] * 2


def test_a_run_writes_one_event_per_proposal(all_records):
    store = get_store("duckdb", path=":memory:")
    for rec in all_records:
        store.save_version(rec, rec["product_id"], 1, None, "active")

    proposals = generate(120, 30, seed=3, end=END)
    for p in proposals:
        outcome = pipeline.decide(p.record, p.request, StubTriager())
        store.save_request(p.request)
        store.save_event(
            outcome.decision, p.request.product_id,
            triage_outcome=outcome.triage,
            occurred_at=p.request.submitted_at,
        )

    n = store.query("SELECT COUNT(*) AS n FROM change_events")[0]["n"]
    assert n == len(proposals)

    # Every event is attributable to a request that was also stored.
    orphans = store.query(
        "SELECT COUNT(*) AS n FROM change_events e "
        "LEFT JOIN change_requests r USING (request_id) "
        "WHERE r.request_id IS NULL"
    )[0]["n"]
    assert orphans == 0
    store.close()


def test_events_carry_the_time_they_were_given(all_records):
    """The backfill timestamp has to reach the database.

    Without it every event in a generated history shares one timestamp, and the
    whole window collapses into a spike.
    """
    store = get_store("duckdb", path=":memory:")
    for rec in all_records:
        store.save_version(rec, rec["product_id"], 1, None, "active")

    proposals = generate(50, 60, seed=9, end=END)
    for p in proposals:
        outcome = pipeline.decide(p.record, p.request, StubTriager())
        store.save_request(p.request)
        store.save_event(
            outcome.decision, p.request.product_id,
            occurred_at=p.request.submitted_at,
        )

    spread = store.query(
        "SELECT MIN(occurred_at) AS lo, MAX(occurred_at) AS hi FROM change_events"
    )[0]
    assert (spread["hi"] - spread["lo"]) > timedelta(days=30)
    store.close()


def test_every_labelled_shape_actually_reaches_the_model():
    """The benchmark's whole premise.

    SHAPE_TRUTH labels proposals so a triager can be scored on them. If a shape
    were settled by screen() it would never reach a model, and scoring it would
    measure the rules instead - flattering the model with cases it never saw.
    """
    import random

    from domain import changes
    from sim.generate import SHAPE_TRUTH

    records = corpus.load_records()
    gen = Generator(records, random.Random(11))

    for shape in SHAPE_TRUTH:
        reached = 0
        for _ in range(30):
            proposal = getattr(gen, shape)()
            if proposal is None:
                continue
            if changes.screen(proposal.record, proposal.request) is None:
                reached += 1
        assert reached > 0, f"{shape} is always settled by the rules"


def test_shape_truth_only_labels_shapes_that_exist():
    from sim.generate import SHAPE_TRUTH

    records = corpus.load_records()
    import random

    gen = Generator(records, random.Random(0))
    for shape in SHAPE_TRUTH:
        assert hasattr(gen, shape), f"SHAPE_TRUTH names a missing shape: {shape}"
