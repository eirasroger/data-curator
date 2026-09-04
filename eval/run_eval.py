"""Score the decision pipeline against the labelled cases.

ACCURACY is how often the pipeline reached the outcome we labelled as correct.
UNSAFE is how often it APPLIED a change that should have been rejected or sent
to a person. Those are not equally bad. A change wrongly parked for review costs
somebody five minutes. A change wrongly applied silently corrupts a published
figure that other people go on to quote. A run with 95% accuracy and one unsafe
error is worse than a run with 85% accuracy and none.

Run:
    python eval/run_eval.py                      # stub, free, no network
    python eval/run_eval.py --provider openai    # calls the API, costs money
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load OPENAI_API_KEY from .env if present. Absent is fine - the stub provider
# is the default and needs no credentials.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from domain import changes, pipeline  # noqa: E402
from domain.changes import Action, ChangeRequest  # noqa: E402
from domain.triage import get_triager  # noqa: E402

SEED = ROOT / "seed" / "epds.json"
CASES = ROOT / "eval" / "cases.json"


def load_record(records: dict, case: dict) -> dict:
    """The record as this case needs it, corruptions applied."""
    import copy

    record = copy.deepcopy(records[case["product_id"]])
    for path, value in (case.get("corrupt") or {}).items():
        record = changes.apply_field_update(record, path, value)
    return record


def main() -> int:
    # Model rationales contain characters cp1252 cannot encode (arrows, the
    # multiplication sign). Cloud Run is UTF-8 and never sees this; a local
    # Windows console crashes on it mid-run.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="stub", choices=["stub", "openai"])
    ap.add_argument("--model", default="gpt-5-mini")
    ap.add_argument("--limit", type=int, default=0, help="only run the first N cases")
    ap.add_argument("--verbose", action="store_true", help="print every case")
    ap.add_argument("--out", default="", help="write a JSON summary here")
    args = ap.parse_args()

    records = {r["product_id"]: r for r in json.loads(SEED.read_text(encoding="utf-8"))}
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    if args.limit:
        cases = cases[: args.limit]

    triager = get_triager(args.provider, args.model)

    correct = 0
    unsafe: list[dict] = []
    triage_hits = triage_total = 0
    model_calls = 0
    total_cost = 0.0
    latencies: list[int] = []
    per_category: dict[str, list[bool]] = defaultdict(list)
    failures: list[str] = []

    for case in cases:
        record = load_record(records, case)
        req = ChangeRequest(
            request_id=case["case_id"],
            product_id=case["product_id"],
            **{k: v for k, v in case["request"].items() if v is not None},
        )
        outcome = pipeline.decide(record, req, triager)
        got = outcome.decision.action.value
        want = case["expected_action"]
        ok = got == want

        correct += ok
        per_category[case["category"]].append(ok)

        if outcome.used_model:
            model_calls += 1
            total_cost += outcome.cost_usd
            latencies.append(outcome.latency_ms)

        want_triage = case.get("expected_triage")
        if want_triage and outcome.decision.triage:
            # A list means several classifications are equally defensible. Only
            # "implausible" changes what the system does with the request; the
            # rest are context for whoever reviews it.
            accepted = want_triage if isinstance(want_triage, list) else [want_triage]
            triage_total += 1
            triage_hits += outcome.decision.triage.value in accepted

        # The error that actually hurts: applied when it should not have been.
        if got == Action.APPLIED.value and want != Action.APPLIED.value:
            unsafe.append({"case": case["case_id"], "wanted": want,
                           "reason": outcome.decision.reason})

        if not ok:
            failures.append(
                f"  {case['case_id']:<24} wanted {want:<14} got {got:<14} "
                f"{outcome.decision.reason[:70]}"
            )

        if args.verbose:
            mark = "ok " if ok else "MISS"
            print(f"[{mark}] {case['case_id']:<24} {got:<14} {outcome.decision.reason[:80]}")

    n = len(cases)
    print()
    print("=" * 72)
    print(f"provider={args.provider}" + (f" model={args.model}" if args.provider != "stub" else ""))
    print("=" * 72)
    print(f"accuracy         {correct}/{n}  ({correct / n:.0%})")
    if triage_total:
        print(f"triage class     {triage_hits}/{triage_total}  ({triage_hits / triage_total:.0%}) "
              f"on cases where the label is determinate")
    print(f"unsafe applies   {len(unsafe)}   <- the number that must be zero")

    print()
    print("by category:")
    for cat, results in sorted(per_category.items()):
        hits = sum(results)
        print(f"  {cat:<22} {hits:>2}/{len(results):<3} ({hits / len(results):.0%})")

    print()
    print(f"model consulted  {model_calls}/{n} requests "
          f"({(n - model_calls) / n:.0%} settled by rules alone)")
    if latencies:
        latencies.sort()
        p95 = latencies[min(int(len(latencies) * 0.95), len(latencies) - 1)]
        print(f"latency          median {latencies[len(latencies) // 2]} ms, p95 {p95} ms")
    print(f"cost             ${total_cost:.4f}"
          + (f"  (${total_cost / model_calls:.5f} per model call)" if model_calls else ""))

    if args.out:
        import json as _json
        Path(args.out).write_text(_json.dumps({
            "provider": args.provider,
            "model": args.model if args.provider != "stub" else "stub",
            "cases": n,
            "correct": correct,
            "pass_rate": round(correct / n, 4),
            "unsafe": len(unsafe),
            "model_calls": model_calls,
            "cost_usd": round(total_cost, 6),
            "run_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2), encoding="utf-8")
        print(f"summary written to {args.out}")

    if unsafe:
        print()
        print("UNSAFE APPLIES:")
        for u in unsafe:
            print(f"  {u['case']}: should have been {u['wanted']} - {u['reason'][:80]}")

    if failures:
        print()
        print("misses:")
        for f in failures:
            print(f)

    return 1 if unsafe else 0


if __name__ == "__main__":
    raise SystemExit(main())
