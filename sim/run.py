"""Run generated proposals through the real pipeline into a DuckDB file.

Usage: python sim/run.py [--count N] [--drift] [--provider openai]
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

# Loads the local API key, if there is one.
from domain.localenv import load as _load_local_env  # noqa: E402

_load_local_env()

from domain import pipeline, reconcile  # noqa: E402
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

    # Decisions run in parallel; writes stay on this thread (one DuckDB writer).
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
        # Timestamp the decision just after its simulated submission.
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

    # Run the nightly job once, so the dashboard has a reconciliation to show.
    expiries: list = []

    def publish_expiry(request):
        expiries.append(request)
        record = store.current_record(request.product_id)
        if record is None:
            return
        outcome = pipeline.decide(record, request, triager)
        store.save_request(request)
        store.save_event(outcome.decision, request.product_id)
        if outcome.decision.action is Action.APPLIED:
            updated = pipeline.apply_decision(record, request, outcome.decision)
            store.save_version(updated, request.product_id,
                               int(record.get("_version", 1)) + 1,
                               request.request_id, "expired")

    summary = reconcile.run(store, publish_expiry)
    store.save_reconciliation(summary)
    print()
    print(f"nightly job: {len(expiries)} expiry request(s), "
          f"{len(summary['drift_flags'])} flag(s)")

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
