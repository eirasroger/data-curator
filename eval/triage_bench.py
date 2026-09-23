"""Measure whether the triager's confidence separates correct proposals from wrong ones.

Usage: python eval/triage_bench.py [--provider openai] [--count N]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Loads the local API key, if there is one.
from domain.localenv import load as _load_local_env  # noqa: E402

_load_local_env()

from domain import changes  # noqa: E402
from domain.changes import TriageClass  # noqa: E402
from domain.triage import get_triager  # noqa: E402
from sim import corpus  # noqa: E402
from sim.generate import SHAPE_TRUTH, Generator  # noqa: E402

OUT = ROOT / "eval" / "triage_results.json"

LABELS = SHAPE_TRUTH

FLOORS = [0.95, 0.90, 0.85, 0.80, 0.75, 0.70, 0.60]


def build(count: int, seed: int) -> list[dict]:
    """A balanced set of cases that reach the model."""
    rng = random.Random(seed)
    records = corpus.load_records()
    gen = Generator(records, rng)

    per_shape = max(1, count // len(LABELS))
    cases: list[dict] = []
    for shape, label in LABELS.items():
        built = 0
        attempts = 0
        while built < per_shape and attempts < per_shape * 20:
            attempts += 1
            proposal = getattr(gen, shape)()
            if proposal is None:
                continue
            # Keep only cases that screen() leaves open.
            if changes.screen(proposal.record, proposal.request) is not None:
                continue
            cases.append({
                "shape": shape,
                "label": label,
                "record": proposal.record,
                "request": proposal.request,
            })
            built += 1
    rng.shuffle(cases)
    return cases


def safe_at(results: list[dict], floor: float) -> tuple[int, int]:
    """(auto-applied correct, auto-applied wrong) at this confidence floor."""
    ok = bad = 0
    for r in results:
        if r["triage"] == TriageClass.IMPLAUSIBLE.value or r["confidence"] < floor:
            continue
        if r["label"] == "correct":
            ok += 1
        elif r["label"] == "wrong":
            bad += 1
    return ok, bad


def report(results: list[dict], cost: float, args) -> dict:
    """Print the scorecard. Shared by a live run and a --replay."""
    correct = [r["confidence"] for r in results if r["label"] == "correct"]
    wrong = [r["confidence"] for r in results if r["label"] == "wrong"]
    ambiguous = [r for r in results if r["label"] == "ambiguous"]

    mean_correct = sum(correct) / len(correct) if correct else 0.0
    mean_wrong = sum(wrong) / len(wrong) if wrong else 0.0
    separation = mean_correct - mean_wrong

    print()
    print("=" * 72)
    print(f"provider={args.provider}"
          + (f" model={args.model}" if args.provider != "stub" else ""))
    print("=" * 72)
    print(f"cases            {len(results)} that survived the rules")
    print()
    print(f"mean confidence, correct proposals   {mean_correct:.3f}  "
          f"(n={len(correct)})")
    print(f"mean confidence, wrong proposals     {mean_wrong:.3f}  (n={len(wrong)})")
    print(f"separation, all cases                {separation:+.3f}"
          f"   <- misleading on its own; see below")

    # How often a wrong proposal is called implausible (and so auto-rejected).
    caught = sum(
        1 for r in results
        if r["label"] == "wrong" and r["triage"] == TriageClass.IMPLAUSIBLE.value
    )
    false_alarm = sum(
        1 for r in results
        if r["label"] == "correct" and r["triage"] == TriageClass.IMPLAUSIBLE.value
    )
    print()
    print(f"wrong proposals called implausible   {caught}/{len(wrong)}")
    print(f"correct proposals called implausible {false_alarm}/{len(correct)}"
          f"   <- a correction silently thrown away")

    # Separation excluding "implausible", since those are rejected on the class.
    offered = [r for r in results if r["triage"] != TriageClass.IMPLAUSIBLE.value]
    off_ok = [r["confidence"] for r in offered if r["label"] == "correct"]
    off_bad = [r["confidence"] for r in offered if r["label"] == "wrong"]
    conditioned = (
        (sum(off_ok) / len(off_ok) if off_ok else 0.0)
        - (sum(off_bad) / len(off_bad) if off_bad else 0.0)
    )
    print(f"SEPARATION among offered cases       {conditioned:+.3f}"
          f"   <- the one that matters (n={len(offered)})")

    deferred = sum(
        1 for r in ambiguous
        if r["triage"] in (TriageClass.UNCLEAR.value,) or r["confidence"] < 0.85
    )
    print(f"ambiguous cases deferred             {deferred}/{len(ambiguous)}")

    print()
    print("what each confidence floor would do:")
    print(f"  {'floor':<8} {'auto-applied':<14} {'of which WRONG':<16} {'reviewed'}")
    for floor in FLOORS:
        applied_ok = applied_bad = reviewed = 0
        for r in results:
            implausible = r["triage"] == TriageClass.IMPLAUSIBLE.value
            if implausible or r["confidence"] < floor:
                reviewed += 1
                continue
            if r["label"] == "correct":
                applied_ok += 1
            elif r["label"] == "wrong":
                applied_bad += 1
        mark = "  <- unsafe" if applied_bad else ""
        print(f"  {floor:<8.2f} {applied_ok:<14} {applied_bad:<16} {reviewed}{mark}")

    print()
    by_shape: dict[str, list[float]] = defaultdict(list)
    for r in results:
        by_shape[r["shape"]].append(r["confidence"])
    print("mean confidence by proposal shape:")
    for shape in LABELS:
        values = by_shape.get(shape, [])
        if values:
            print(f"  {shape:<26} {LABELS[shape]:<10} "
                  f"{sum(values) / len(values):.3f}  (n={len(values)})")

    if args.provider == "openai":
        print()
        print(f"cost             ${cost:.4f}")

    return {
        "mean_confidence_correct": round(mean_correct, 4),
        "mean_confidence_wrong": round(mean_wrong, 4),
        "separation": round(separation, 4),
        "separation_offered": round(conditioned, 4),
        "wrong_called_implausible": caught,
        "correct_called_implausible": false_alarm,
        "safe_floors": [f for f in FLOORS if safe_at(results, f)[1] == 0
                        and safe_at(results, f)[0] > 0],
    }



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="stub", choices=["stub", "openai"])
    ap.add_argument("--model", default="gpt-5-mini")
    ap.add_argument("--count", type=int, default=120,
                    help="total cases; split evenly across shapes")
    ap.add_argument("--seed", type=int, default=20260918)
    ap.add_argument("--out", default="", help="write results JSON here")
    ap.add_argument("--replay", default="",
                    help="re-score a saved results file without calling anything")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if args.replay:
        saved = json.loads(Path(args.replay).read_text(encoding="utf-8"))
        results = saved["results"]
        cost = saved.get("cost_usd", 0.0)
        args.provider, args.model = saved["provider"], saved["model"]
        report(results, cost, args)
        return 0

    cases = build(args.count, args.seed)
    triager = get_triager(args.provider, args.model)

    if args.provider == "openai":
        print(f"Sending {len(cases)} cases to {args.model}.")

    results = []
    cost = 0.0
    for i, case in enumerate(cases, 1):
        outcome = triager.triage(case["record"], case["request"])
        results.append({
            "shape": case["shape"],
            "label": case["label"],
            "field": case["request"].field_path,
            "triage": outcome.result.triage.value,
            "confidence": outcome.result.confidence,
            "rationale": outcome.result.rationale,
            "latency_ms": outcome.latency_ms,
            "prompt_tokens": outcome.prompt_tokens,
            "completion_tokens": outcome.completion_tokens,
            "cost_usd": outcome.cost_usd,
        })
        cost += outcome.cost_usd
        if args.provider == "openai" and i % 20 == 0:
            print(f"  {i}/{len(cases)}  (${cost:.3f} so far)")

    metrics = report(results, cost, args)

    out = Path(args.out) if args.out else OUT
    out.write_text(json.dumps({
        "run_at": datetime.now(UTC).isoformat(),
        "provider": args.provider,
        "model": args.model if args.provider != "stub" else "stub",
        "seed": args.seed,
        "cases": len(results),
        **metrics,
        "cost_usd": round(cost, 6),
        "results": results,
    }, indent=2), encoding="utf-8")
    print(f"\nwrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
