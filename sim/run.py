"""Drive generated proposals through the real pipeline and store the results.

The decisions this writes are genuine. `pipeline.decide()` is the same function
the worker calls on a live message, the records are the real corpus, and the
validation arithmetic is the real arithmetic. Only the arrival of the proposals
is invented.

That is the whole point: `reconcile.py` looks for drift and has never had a
history to look at, and there is no way to ask whether the confidence floor is
set right without a few thousand decisions to look at.

Run:
    python sim/run.py                       # 2000 proposals over 90 days, free
    python sim/run.py --count 500 --drift   # shift the mix late, to test the
                                            # nightly job's drift detector
    python sim/run.py --provider openai --count 200   # costs money, see below
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

ROOT_FOR_ENV = Path(__file__).resolve().parents[1]
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT_FOR_ENV / ".env")
except ImportError:
    pass

from domain import pipeline  # noqa: E402
from domain.changes import Action, ChangeKind  # noqa: E402
from domain.store import get_store  # noqa: E402
from domain.triage import get_triager  # noqa: E402
from sim.generate import DEFAULT_SEED, generate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "sim.duckdb"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=2000, help="proposals to generate")
    ap.add_argument("--days", type=int, default=90, help="window to spread them over")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--drift", action="store_true",
                    help="shift the proposal mix in the last third of the window")
    ap.add_argument("--provider", default="stub", choices=["stub", "openai"])
    ap.add_argument("--model", default="gpt-5-mini")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--replace", action="store_true",
                    help="delete the database file first")
    ap.add_argument("--workers", type=int, default=0,
                    help="parallel model calls; defaults to 16 for openai, 1 for "
                         "the stub, which is instant and gains nothing")
    args = ap.parse_args()

    db = Path(args.db)
    if args.replace and db.exists():
        db.unlink()

    store = get_store("duckdb", path=str(db))
    if store.epd_count() == 0:
        print("No EPDs in this database. Seed it first:", file=sys.stderr)
        print(f"  python scripts/seed_local.py --db {db}", file=sys.stderr)
        return 1

    triager = get_triager(args.provider, args.model)
    proposals = generate(args.count, args.days, seed=args.seed, drift=args.drift)
    workers = args.workers or (16 if args.provider == "openai" else 1)

    if args.provider == "openai":
        print(f"About to send up to {len(proposals)} proposals to {args.model}.")
        print("Only those the rules cannot settle reach the model; the rest "
              "cost nothing.")

    by_action: Counter[str] = Counter()
    by_generator: Counter[str] = Counter()
    model_calls = 0
    total_cost = 0.0

    # Deciding is pure: it reads the record carried by the proposal, never the
    # store, so the calls are independent and the slow one is a network round
    # trip. Writes stay on this thread - one DuckDB connection, one writer, and
    # the append-only order preserved.
    def decide_one(proposal):
        return pipeline.decide(proposal.record, proposal.request, triager)

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(decide_one, proposals))
    else:
        outcomes = [decide_one(p) for p in proposals]

    for i, (proposal, outcome) in enumerate(zip(proposals, outcomes, strict=True), 1):
        request = proposal.request
        decision = outcome.decision

        store.save_request(request)
        # The decision lands shortly after the proposal, not now. Without this
        # every event would share one timestamp and the window would be a spike.
        decided_at = datetime.fromisoformat(request.submitted_at) + timedelta(
            milliseconds=max(outcome.latency_ms, 1)
        )
        store.save_event(
            decision, request.product_id,
            triage_outcome=outcome.triage,
            occurred_at=decided_at.isoformat(),
        )

        if decision.action is Action.APPLIED:
            updated = pipeline.apply_decision(proposal.record, request, decision)
            status = "expired" if request.kind is ChangeKind.EXPIRY else "active"
            current = store.current_record(request.product_id)
            version = int((current or {}).get("_version", 1)) + 1
            store.save_version(
                updated, request.product_id, version, request.request_id, status
            )

        by_action[decision.action.value] += 1
        by_generator[proposal.generator] += 1
        if outcome.used_model:
            model_calls += 1
            total_cost += outcome.cost_usd

        if i % 250 == 0:
            print(f"  {i}/{len(proposals)} decided")

    n = len(proposals)
    print()
    print("=" * 66)
    print(f"provider={args.provider} seed={args.seed} drift={args.drift}")
    print("=" * 66)
    print(f"proposals        {n} over {args.days} days -> {db}")
    print()
    print("outcomes:")
    for action, count in sorted(by_action.items()):
        print(f"  {action:<16} {count:>5}  ({count / n:.0%})")
    print()
    print("by proposal shape:")
    for name, count in sorted(by_generator.items()):
        print(f"  {name:<22} {count:>5}")
    print()
    settled = n - model_calls
    print(f"model consulted  {model_calls}/{n} ({settled / n:.0%} settled by rules)")
    print(f"cost             ${total_cost:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
