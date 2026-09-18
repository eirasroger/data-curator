"""Score the decision pipeline against the labelled cases.

Three figures, because one hides the interesting part.

END TO END is how often the pipeline reached the labelled outcome. On its own
it flatters the system: the rules settle roughly three quarters of these cases
for free, so the headline mostly measures arithmetic that cannot be wrong.

RULES ONLY and MODEL LAYER split that. The first is the cases `screen()`
settled without asking anything; the second is the cases that survived it and
reached a triager. Only the second changes when the provider changes, and it is
the only one worth comparing between a stub and a paid model.

UNSAFE is how often a change was APPLIED that should have been rejected or sent
to a person. It is not on the same scale as the others. A change wrongly parked
costs somebody five minutes; a change wrongly applied corrupts a published
figure other people go on to quote. A run with 95% accuracy and one unsafe
error is worse than one with 85% and none, which is why this exits non-zero on
any unsafe count regardless of accuracy.

Run:
    python eval/run_eval.py                      # stub, free, no network
    python eval/run_eval.py --provider openai    # calls the API, costs money
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

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
    ap.add_argument("--min-accuracy", type=float, default=0.0,
                    help="exit non-zero below this end-to-end accuracy; the CI gate")
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
    # Split by who actually decided, so the model layer can be scored alone.
    rules_hits = rules_total = 0
    model_hits = model_total = 0

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
            model_total += 1
            model_hits += ok
            total_cost += outcome.cost_usd
            latencies.append(outcome.latency_ms)
        else:
            rules_total += 1
            rules_hits += ok

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
            print(
                f"[{mark}] {case['case_id']:<24} {got:<14} "
                f"{outcome.decision.reason[:80]}"
            )

    n = len(cases)
    print()
    print("=" * 72)
    suffix = f" model={args.model}" if args.provider != "stub" else ""
    print(f"provider={args.provider}{suffix}")
    print("=" * 72)
    print(f"end to end       {correct}/{n}  ({correct / n:.0%})")
    if rules_total:
        print(f"  rules only     {rules_hits}/{rules_total}  "
              f"({rules_hits / rules_total:.0%})"
              f"   settled by screen(), provider-independent")
    if model_total:
        print(f"  model layer    {model_hits}/{model_total}  "
              f"({model_hits / model_total:.0%})"
              f"   what the triager is actually worth")
    if triage_total:
        print(
            f"triage class     {triage_hits}/{triage_total}  "
            f"({triage_hits / triage_total:.0%}) "
            f"on cases where the label is determinate"
        )
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
        median = latencies[len(latencies) // 2]
        print(f"latency          median {median} ms, p95 {p95} ms")
    per_call = (
        f"  (${total_cost / model_calls:.5f} per model call)" if model_calls else ""
    )
    print(f"cost             ${total_cost:.4f}{per_call}")

    if args.out:
        import json as _json
        Path(args.out).write_text(_json.dumps({
            "provider": args.provider,
            "model": args.model if args.provider != "stub" else "stub",
            "cases": n,
            "correct": correct,
            "pass_rate": round(correct / n, 4),
            "rules_only": {"correct": rules_hits, "total": rules_total},
            "model_layer": {
                "correct": model_hits,
                "total": model_total,
                "pass_rate": round(model_hits / model_total, 4) if model_total else None,
            },
            "unsafe": len(unsafe),
            "confidence_floor": changes.CONFIDENCE_FLOOR,
            "model_calls": model_calls,
            "cost_usd": round(total_cost, 6),
            "run_at": datetime.now(UTC).isoformat(),
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

    # Two gates, and they are not the same kind of thing. Unsafe must be zero
    # whatever the accuracy. Accuracy has a floor rather than a target, because
    # a regression matters and the exact figure does not.
    failed = False
    if unsafe:
        failed = True
    if args.min_accuracy and correct / n < args.min_accuracy:
        print()
        print(f"FAIL: accuracy {correct / n:.1%} is below the "
              f"{args.min_accuracy:.0%} baseline.")
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
