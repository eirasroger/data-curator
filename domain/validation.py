"""Consistency checks on an EPD record. Each check returns a list of Issues."""

from dataclasses import asdict, dataclass
from typing import Literal

Severity = Literal["error", "warning"]

# gwp_total is a sum of rounded parts, hence a tolerance.
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
    """gwp_total should equal fossil + luluc + biogenic. Biogenic is often negative."""
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
            f"Re-read the A1-A3 impact table; a value was likely taken from the "
            f"wrong row or column.",
        )]
    return []


# Name fragments that identify packaging.
PACKAGING_TERMS = (
    "packag", "embalaje", "pallet", "palet", "wrap", "film", "carton", "cardboard",
    "crate", "strapping", "shrink",
)


def check_composition(p: dict) -> list[Issue]:
    """Over 100% is an error; under 100% is a warning, since range midpoints vary."""
    comp = (p.get("product_integrity") or {}).get("comp") or []
    vals = [c.get("percentage") for c in comp if c.get("percentage") is not None]
    if not vals:
        return []
    total = sum(vals)

    if total > 100.0 + PERCENT_TOLERANCE:
        return [Issue(
            "product_integrity.comp", "error",
            f"Material percentages total {total:.1f}%, which exceeds 100%. "
            f"A value was likely double counted or misread. Do NOT add or remove "
            f"materials "
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
    """Packaging must stay out of the product composition."""
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
                f"Assign any remainder to nonrecov, or to unknown if the EPD does "
                f"not say.",
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
            f"These variant names are referenced but missing from `variants`: "
            f"{sorted(orphans)}. "
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
        return [Issue(
            "C2C_lvl", "warning",
            "A C2C level is set but C2C is not true. Clear the level or set C2C.",
        )]
    return []


def check_lifespan(p: dict) -> list[Issue]:
    ls, le = p.get("lifespan"), p.get("lifespan_exp")
    if ls is None and le is not None:
        return [Issue(
            "lifespan", "warning",
            "lifespan_exp is set but lifespan is null. If the EPD gives one value, "
            "use it for both.",
        )]
    if le is None and ls is not None:
        return [Issue(
            "lifespan_exp", "warning",
            "lifespan is set but lifespan_exp is null. If the EPD gives one value, "
            "use it for both.",
        )]
    return []


# Specific to data-curator; worth adding to the extractor.
DENSITY_TOLERANCE = 0.05    # 5% relative


def _declared_kg_per_unit(p: dict) -> float | None:
    """The kg-per-declared-unit figure the EPD states outright, if it does."""
    unit = p.get("reference_unit")
    for r in p.get("conversion_ratios") or []:
        if r.get("measured_unit") == "kg" and r.get("per_unit") == unit:
            v = r.get("measured_units_per_one_per_unit")
            if isinstance(v, (int, float)):
                return float(v)
    return None


def check_density_thickness(p: dict) -> list[Issue]:
    """density (kg/m3) x thickness (m) should equal the declared kg per unit.

    Runs only when all three figures are present.
    """
    density, thickness = p.get("density"), p.get("thickness")
    declared = _declared_kg_per_unit(p)
    if density is None or thickness is None or declared is None or not thickness:
        return []

    computed = float(density) * float(thickness)
    if _close(computed, declared, DENSITY_TOLERANCE):
        return []

    return [Issue(
        "density", "error",
        f"density {density} kg/m3 x thickness {thickness} m = {computed:.4g} kg per "
        f"{p.get('reference_unit')}, but the EPD declares {declared:g}. One of the "
        f"three is wrong; the declared conversion ratio is the one copied straight "
        f"from the document, so prefer it.",
    )]


CHECKS = [
    check_gwp, check_density_thickness, check_composition, check_no_packaging,
    check_circularity, check_variant_names, check_flag, check_c2c, check_lifespan,
]


def validate(product: dict) -> list[Issue]:
    """Run every check. Errors first, then warnings."""
    issues: list[Issue] = []
    for check in CHECKS:
        issues.extend(check(product))
    return sorted(issues, key=lambda i: 0 if i.severity == "error" else 1)


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]
