"""Build the labelled set of change requests.

These stand in for the real thing: people proposing corrections, manufacturers
republishing, the calendar expiring records. Every case is derived from the
actual 63 extracted EPDs, so the numbers are real even though the proposals are
invented.

Each case carries the answer we believe is correct. That makes this two things
at once - the input the pipeline runs on, and the yardstick for whether the
triage step is any good.

A case may CORRUPT the record before proposing a change. That is how we get
genuine corrections: break a value, then propose putting it back. Without that,
every proposal against a correct record would be a bad proposal, and the set
would only ever measure the system's ability to say no.

Run:  python eval/generate_cases.py    ->  eval/cases.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "seed" / "epds.json"
OUT = ROOT / "eval" / "cases.json"

records = {r["product_id"]: r for r in json.loads(SEED.read_text(encoding="utf-8"))}
cases: list[dict] = []


def case(
    case_id: str,
    category: str,
    product_id: int,
    expected_action: str,
    note: str,
    expected_triage: Optional[str] = None,
    corrupt: Optional[dict[str, Any]] = None,
    kind: str = "field_update",
    field_path: Optional[str] = None,
    new_value: Any = None,
    reason: str = "",
    source: str = "human",
    submitted_by: str = "reviewer",
    replacement: Optional[dict] = None,
) -> None:
    cases.append(
        {
            "case_id": case_id,
            "category": category,
            "product_id": product_id,
            "corrupt": corrupt,
            "request": {
                "kind": kind,
                "field_path": field_path,
                "new_value": new_value,
                "reason": reason,
                "source": source,
                "submitted_by": submitted_by,
                "replacement": replacement,
            },
            "expected_action": expected_action,
            "expected_triage": expected_triage,
            "note": note,
        }
    )


def kg_per_unit(rec: dict) -> Optional[float]:
    unit = rec.get("reference_unit")
    for r in rec.get("conversion_ratios") or []:
        if r.get("measured_unit") == "kg" and r.get("per_unit") == unit:
            return r.get("measured_units_per_one_per_unit")
    return None


CROSS_CHECKABLE = [
    pid for pid, r in records.items()
    if r.get("thickness") and r.get("density") and kg_per_unit(r)
]
WITH_GWP = [
    pid for pid, r in records.items()
    if (r.get("impacts") or {}).get("gwp_total") is not None
    and (r.get("impacts") or {}).get("gwp_fossil") is not None
]
WITH_COMP = [
    pid for pid, r in records.items()
    if len([c for c in (r.get("product_integrity") or {}).get("comp") or []
            if c.get("percentage") is not None]) >= 2
]


# ---------------------------------------------------------------------------
# A. Settled by the rules: rejected before any model is consulted
# ---------------------------------------------------------------------------

for pid in WITH_GWP[:6]:
    r = records[pid]
    fossil = r["impacts"]["gwp_fossil"]
    case(
        f"A_gwp_break_{pid}", "rejected_by_rules", pid, "rejected",
        "Ten-times the fossil GWP no longer sums to the declared total; "
        "check_gwp catches it.",
        field_path="impacts.gwp_fossil", new_value=round(fossil * 10, 6),
        reason="I think the fossil figure was under-reported by a factor of ten.",
    )

for pid in list(records)[:4]:
    r = records[pid]
    if r.get("density") is None:
        continue
    case(
        f"A_type_{pid}", "rejected_by_rules", pid, "rejected",
        "Comma decimal arriving as text. A number must stay a number.",
        field_path="density", new_value=str(r["density"]).replace(".", ","),
        reason="Copied straight from the datasheet.",
    )

for pid in list(records)[:3]:
    case(
        f"A_noop_{pid}", "rejected_by_rules", pid, "rejected",
        "Proposed value equals the stored value.",
        field_path="lifespan", new_value=records[pid].get("lifespan"),
        reason="Confirming this is right.",
    )

for pid in list(records)[:3]:
    case(
        f"A_badpath_{pid}", "rejected_by_rules", pid, "rejected",
        "Field does not exist in the schema.",
        field_path="carbon_footprint", new_value=12.0,
        reason="Adding the carbon footprint field.",
    )

for pid in WITH_COMP[:2]:
    case(
        f"A_badmaterial_{pid}", "rejected_by_rules", pid, "rejected",
        "Names a material that is not in this product's composition.",
        field_path="product_integrity.comp[Unobtainium].percentage", new_value=5.0,
        reason="Adding the missing material.",
    )

# Packaging must never enter the composition - a rule learned the hard way in
# the extractor, and it holds for edits too.
for pid in WITH_COMP[:2]:
    r = records[pid]
    first = next(c["name"] for c in r["product_integrity"]["comp"]
                 if c.get("percentage") is not None)
    total = sum(c["percentage"] for c in r["product_integrity"]["comp"]
                if c.get("percentage") is not None)
    case(
        f"A_over100_{pid}", "rejected_by_rules", pid, "rejected",
        "Pushes the composition above 100%, which check_composition treats as "
        "an arithmetic error.",
        field_path=f"product_integrity.comp[{first}].percentage",
        new_value=round(total + 15.0, 4),
        reason="This material is the bulk of the product.",
    )


# ---------------------------------------------------------------------------
# B. Settled by the rules: applied without a model
# ---------------------------------------------------------------------------

for pid in list(records)[:3]:
    case(
        f"B_expiry_{pid}", "applied_by_rules", pid, "applied",
        "Expiry is produced by the nightly job reading a date. Not a proposal.",
        kind="expiry", source="scheduler", submitted_by="reconcile-job",
        reason="Validity date has passed.",
    )

for pid in WITH_COMP[:4]:
    r = records[pid]
    comp = [c for c in r["product_integrity"]["comp"] if c.get("percentage") is not None]
    name, correct = comp[0]["name"], comp[0]["percentage"]
    total = sum(c["percentage"] for c in comp)
    case(
        f"B_repair_{pid}", "applied_by_rules", pid, "applied",
        "The stored record contradicts itself; this change resolves it and "
        "breaks nothing. No judgement required.",
        corrupt={f"product_integrity.comp[{name}].percentage": round(correct + (105.0 - total) + 15.0, 4)},
        field_path=f"product_integrity.comp[{name}].percentage", new_value=correct,
        reason="The percentages currently add up to more than the whole product.",
    )


# ---------------------------------------------------------------------------
# C. Always a person: material fields, whatever the model says
# ---------------------------------------------------------------------------

for pid in WITH_GWP[:3]:
    total = records[pid]["impacts"]["gwp_total"]
    case(
        f"C_material_gwp_{pid}", "always_reviewed", pid, "pending_review",
        "A published headline figure. Never auto-applied.",
        field_path="impacts.gwp_total", new_value=round(total * 1.02, 6),
        reason="The published table rounds differently.",
    )

for pid in list(records)[:2]:
    case(
        f"C_material_unit_{pid}", "always_reviewed", pid, "pending_review",
        "Changing the declared unit reinterprets every number attached to it.",
        field_path="reference_unit", new_value="kg",
        reason="I believe this EPD is declared per kilogram.",
    )

for pid in list(records)[:2]:
    case(
        f"C_material_date_{pid}", "always_reviewed", pid, "pending_review",
        "The expiry date governs whether the EPD may be cited at all.",
        field_path="date", new_value="2030-01-01",
        reason="The manufacturer told me it was extended.",
    )

for pid in list(records)[:2]:
    case(
        f"C_replacement_{pid}", "always_reviewed", pid, "pending_review",
        "A new document supersedes the old one; that is never automatic.",
        kind="record_replacement", source="manufacturer_feed",
        submitted_by="feed-watcher",
        # A replacement must satisfy EPDProduct - product_id and flag are
        # required. An earlier version of this case omitted flag, which the
        # schema check correctly began rejecting once it was wired up.
        replacement={"product_id": pid, "flag": records[pid].get("flag", 0),
                     "prod_name": records[pid].get("prod_name"),
                     "epd_code": records[pid].get("epd_code")},
        reason="Manufacturer published a new version.",
    )


# ---------------------------------------------------------------------------
# D. Needs the model: the proposal is wrong
# ---------------------------------------------------------------------------

for pid in CROSS_CHECKABLE:
    r = records[pid]
    case(
        f"D_bad_density_{pid}", "rejected_by_rules", pid, "rejected", (
            f"Stored density {r['density']} x thickness {r['thickness']} = "
            f"{r['density'] * r['thickness']:g} kg, matching the declared "
            f"{kg_per_unit(r)} kg/{r['reference_unit']}. The proposal breaks that."
        ),
        field_path="density", new_value=round(r["density"] / 10, 6),
        reason="The datasheet says this is the weight per square metre.",
    )


# ---------------------------------------------------------------------------
# E. Needs the model: the proposal is right
# ---------------------------------------------------------------------------

for pid in CROSS_CHECKABLE:
    r = records[pid]
    case(
        f"E_fix_density_{pid}", "applied_by_rules", pid, "applied", (
            f"The record has been corrupted to {r['density'] * 10:g}; the proposal "
            f"restores the value consistent with the stated "
            f"{kg_per_unit(r)} kg/{r['reference_unit']}."
        ),
        corrupt={"density": round(r["density"] * 10, 6)},
        field_path="density", new_value=r["density"],
        reason="Density is out by a factor of ten against the stated weight per unit.",
    )

for pid in [p for p in records if records[p].get("lifespan")][:4]:
    r = records[pid]
    case(
        f"E_fix_lifespan_{pid}", "model_should_apply", pid, "applied",
        "Service life corrupted by a factor of ten; the proposal restores it.",
        # Any of these three is a fair reading of "500 years became 50".
        # Pinning one and scoring exact-match measured my preference, not the
        # model. Only "implausible" would change what the system does.
        expected_triage=["decimal_slip", "transcription", "genuine_correction"],
        corrupt={"lifespan": r["lifespan"] * 10},
        field_path="lifespan", new_value=r["lifespan"],
        reason=f"A reference service life of {r['lifespan'] * 10:g} years is not "
               f"plausible for this product; the EPD states {r['lifespan']:g}.",
    )


# ---------------------------------------------------------------------------
# F. Needs the model: genuinely undecidable from the record
# ---------------------------------------------------------------------------

for pid in [p for p in records if records[p].get("lifespan")][4:8]:
    r = records[pid]
    case(
        f"F_ambiguous_{pid}", "model_should_defer", pid, "pending_review",
        "Nothing in the record cross-checks service life. Neither value can be "
        "confirmed, so a person should look.",
        field_path="lifespan", new_value=float(r["lifespan"]) - 5.0,
        reason="I recall the declared service life being shorter.",
    )

# These were originally labelled "ambiguous, defer to a person", on the
# assumption that nothing in the record cross-checks thickness. That was wrong.
# density (kg/m3) x thickness (m) = the stated kg per declared unit, so thickness
# is pinned by the same relationship that pins density. The model pointed this
# out by classifying them implausible; the label was the thing at fault.
for pid in CROSS_CHECKABLE[:3]:
    r = records[pid]
    case(
        f"D_bad_thickness_{pid}", "rejected_by_rules", pid, "rejected", (
            f"Thickness {r['thickness']} m x density {r['density']} kg/m3 = "
            f"{r['thickness'] * r['density']:g} kg, matching the declared "
            f"{kg_per_unit(r)} kg/{r['reference_unit']}. A 10% thicker value breaks that."
        ),
        field_path="thickness", new_value=round(float(r["thickness"]) * 1.1, 6),
        reason="Measured slightly thicker on the sample we received.",
    )


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    OUT.write_text(json.dumps(cases, indent=2, ensure_ascii=False), encoding="utf-8")

    from collections import Counter

    by_cat = Counter(c["category"] for c in cases)
    by_act = Counter(c["expected_action"] for c in cases)
    print(f"wrote {len(cases)} cases -> {OUT.relative_to(ROOT)}\n")
    print("by category:")
    for k, v in sorted(by_cat.items()):
        print(f"  {k:22} {v:>3}")
    print("\nby expected outcome:")
    for k, v in sorted(by_act.items()):
        print(f"  {k:22} {v:>3}")
    needs_model = sum(1 for c in cases if c["expected_triage"])
    print(f"\n{needs_model} of {len(cases)} require the model; "
          f"{len(cases) - needs_model} are settled by rules alone.")
