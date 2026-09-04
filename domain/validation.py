"""
Deterministic validation. No LLM anywhere in this file.

Your validacion/ step asks a model to check arithmetic, and the prompt is full of
pleading ("this will displease me", "you tend to say two indicators are reversed
when they are not") because the model kept getting it wrong.

Arithmetic is not a judgement call. Every check below is a plain assertion, and
each one runs in microseconds for free. Save the model for the things that are
genuinely fuzzy -- is this really the expiry date, is this really the production
site -- and let code handle the rest.

Each check returns Issue objects. An Issue is machine-readable on purpose: the
repair node in graph.py feeds `field` and `detail` straight back to the model.
"""

from dataclasses import dataclass, asdict
from typing import Literal

Severity = Literal["error", "warning"]

# gwp_total is a rounded sum of rounded parts, so demand closeness, not equality.
GWP_TOLERANCE = 0.05        # 5% relative
PERCENT_TOLERANCE = 1.0     # absolute percentage points


@dataclass
class Issue:
    field: str
    severity: Severity
    detail: str

    def dict(self) -> dict:
        return asdict(self)


def _close(a: float, b: float, rel: float) -> bool:
    scale = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / scale <= rel


def check_gwp(p: dict) -> list[Issue]:
    """gwp_total should equal fossil + luluc + biogenic.

    Note biogenic is frequently NEGATIVE (sequestered carbon), which is exactly
    what the old LLM judge kept misreading as 'the values are swapped'.
    """
    im = p.get("impacts") or {}
    total = im.get("gwp_total")
    parts = [im.get("gwp_fossil"), im.get("gwp_luluc"), im.get("gwp_bio")]
    if total is None or all(x is None for x in parts):
        return []
    s = sum(x for x in parts if x is not None)
    if not _close(total, s, GWP_TOLERANCE):
        return [Issue(
            "impacts.gwp_total", "error",
            f"gwp_total={total} but gwp_fossil+gwp_luluc+gwp_bio={s:.4g}. "
            f"Re-read the A1-A3 impact table; a value was likely taken from the wrong row or column.",
        )]
    return []


# Substrings that mean an entry is packaging, not product material.
PACKAGING_TERMS = (
    "packag", "embalaje", "pallet", "palet", "wrap", "film", "carton", "cardboard",
    "crate", "strapping", "shrink",
)


def check_composition(p: dict) -> list[Issue]:
    """
    Material percentages versus 100%.

    ASYMMETRIC ON PURPOSE, and this took a fabricated answer to learn:

    OVER 100% is a real arithmetic error -- percentages that sum above the whole
    mean something was double counted or misread. Worth repairing.

    UNDER 100% usually is not. EPDs state composition as ranges ("Basalt 55-60"),
    and the midpoints of ranges do not sum to 100. They also routinely omit minor
    constituents. Flagging this as an error told the model to close a gap that
    should not be closed, and it obliged by inventing an 11% 'Packaging' entry
    that appears nowhere in the source document.

    So: under 100 is a warning, and warnings never trigger a repair.
    """
    comp = (p.get("product_integrity") or {}).get("comp") or []
    vals = [c.get("percentage") for c in comp if c.get("percentage") is not None]
    if not vals:
        return []
    total = sum(vals)

    if total > 100.0 + PERCENT_TOLERANCE:
        return [Issue(
            "product_integrity.comp", "error",
            f"Material percentages total {total:.1f}%, which exceeds 100%. "
            f"A value was likely double counted or misread. Do NOT add or remove materials "
            f"to force a total -- re-read the composition table.",
        )]

    if total < 100.0 - PERCENT_TOLERANCE:
        return [Issue(
            "product_integrity.comp", "warning",
            f"Material percentages total {total:.1f}%, under 100%. Often legitimate: "
            f"range midpoints and undeclared minor constituents do not sum to 100. "
            f"Not repaired automatically.",
        )]
    return []


def check_no_packaging(p: dict) -> list[Issue]:
    """
    Packaging must never appear in the product composition.

    This exists because a repair cycle added one. A rule stated only in the prompt
    is a request; a rule stated in the validator is enforced.
    """
    issues = []
    for c in (p.get("product_integrity") or {}).get("comp") or []:
        name = str(c.get("name") or "").casefold()
        if any(term in name for term in PACKAGING_TERMS):
            issues.append(Issue(
                "product_integrity.comp", "error",
                f"'{c.get('name')}' is packaging and must not appear in the material "
                f"composition. Remove it. Composition covers product materials only.",
            ))
    return issues


def check_circularity(p: dict) -> list[Issue]:
    """End-of-life routes should total 100 per material. `orig` is excluded."""
    eol = ["reuse", "comp", "recycl", "WEEErecycl", "backfill", "refurb",
           "incin", "landfill", "hazard", "nonrecov", "takebackrecycl", "unknown"]
    issues = []
    for entry in (p.get("product_integrity") or {}).get("circ") or []:
        vals = [entry.get(f) for f in eol if entry.get(f) is not None]
        if not vals:
            continue
        total = sum(vals)
        if abs(total - 100.0) > PERCENT_TOLERANCE:
            issues.append(Issue(
                f"product_integrity.circ[{entry.get('name')}]", "warning",
                f"End-of-life routes total {total:.1f}%, not 100%. "
                f"Assign any remainder to nonrecov, or to unknown if the EPD does not say.",
            ))
    return issues


def check_variant_names(p: dict) -> list[Issue]:
    """Every variant referenced elsewhere must exist in `variants`."""
    declared = {v.get("name") for v in p.get("variants") or []}
    issues = []
    referenced: set[str] = set()
    for cr in p.get("conversion_ratios") or []:
        if cr.get("variant"):
            referenced.add(cr["variant"])
    for cf in p.get("correction_factors") or []:
        if cf.get("variant"):
            referenced.add(cf["variant"])
    for vi in p.get("variant_impacts") or []:
        if vi.get("variant"):
            referenced.add(vi["variant"])
    if p.get("variant_ref"):
        referenced.add(p["variant_ref"])

    orphans = referenced - declared
    if orphans:
        issues.append(Issue(
            "variants", "error",
            f"These variant names are referenced but missing from `variants`: {sorted(orphans)}. "
            f"List EVERY variant named in the EPD.",
        ))
    return issues


def check_flag(p: dict) -> list[Issue]:
    """flag=0 means simple; several variants or correction factors contradict that."""
    n_variants = len(p.get("variants") or [])
    has_corr = bool(p.get("correction_factors"))
    has_vi = bool(p.get("variant_impacts"))
    if p.get("flag") == 0 and (n_variants > 1 or has_corr or has_vi):
        return [Issue(
            "flag", "error",
            f"flag=0 (simple) but the record has {n_variants} variants, "
            f"correction_factors={has_corr}, variant_impacts={has_vi}. "
            f"A multi-variant EPD should be flag=1.",
        )]
    return []


def check_c2c(p: dict) -> list[Issue]:
    if p.get("C2C_lvl") and not p.get("C2C"):
        return [Issue("C2C_lvl", "warning",
                      "A C2C level is set but C2C is not true. Clear the level or set C2C.")]
    return []


def check_lifespan(p: dict) -> list[Issue]:
    ls, le = p.get("lifespan"), p.get("lifespan_exp")
    if ls is None and le is not None:
        return [Issue("lifespan", "warning",
                      "lifespan_exp is set but lifespan is null. If the EPD gives one value, use it for both.")]
    if le is None and ls is not None:
        return [Issue("lifespan_exp", "warning",
                      "lifespan is set but lifespan_exp is null. If the EPD gives one value, use it for both.")]
    return []


CHECKS = [
    check_gwp, check_composition, check_no_packaging, check_circularity,
    check_variant_names, check_flag, check_c2c, check_lifespan,
]


def validate(product: dict) -> list[Issue]:
    """Run every check. Errors first, then warnings."""
    issues: list[Issue] = []
    for check in CHECKS:
        issues.extend(check(product))
    return sorted(issues, key=lambda i: 0 if i.severity == "error" else 1)


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]
