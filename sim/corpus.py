"""Record helpers shared by the eval case generator and the simulator."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "seed" / "epds.json"


def load_records() -> dict[int, dict]:
    return {r["product_id"]: r for r in json.loads(SEED.read_text(encoding="utf-8"))}


def kg_per_unit(rec: dict) -> float | None:
    """The declared kg per reference unit, if the EPD states one."""
    unit = rec.get("reference_unit")
    for r in rec.get("conversion_ratios") or []:
        if r.get("measured_unit") == "kg" and r.get("per_unit") == unit:
            return r.get("measured_units_per_one_per_unit")
    return None


def cross_checkable(records: dict[int, dict]) -> list[int]:
    """Records where density x thickness can be checked against the declared kg."""
    return [
        pid for pid, r in records.items()
        if r.get("thickness") and r.get("density") and kg_per_unit(r)
    ]


def not_cross_checkable(records: dict[int, dict]) -> list[int]:
    """Records with a density but no ratio to check it against. Changes reach the LLM."""
    checkable = set(cross_checkable(records))
    return [
        pid for pid, r in records.items()
        if pid not in checkable and isinstance(r.get("density"), (int, float))
        and r["density"]
    ]


def with_gwp(records: dict[int, dict]) -> list[int]:
    """Records whose GWP total and fossil component are both declared."""
    return [
        pid for pid, r in records.items()
        if (r.get("impacts") or {}).get("gwp_total") is not None
        and (r.get("impacts") or {}).get("gwp_fossil") is not None
    ]


def with_comp(records: dict[int, dict]) -> list[int]:
    """Records with at least two materials carrying a percentage."""
    return [
        pid for pid, r in records.items()
        if len(percentages(r)) >= 2
    ]


def with_lifespan(records: dict[int, dict]) -> list[int]:
    return [pid for pid, r in records.items() if r.get("lifespan")]


def percentages(rec: dict) -> list[dict]:
    return [
        c for c in (rec.get("product_integrity") or {}).get("comp") or []
        if c.get("percentage") is not None
    ]


def corrupt(record: dict, changes_to_apply: dict[str, Any]) -> dict:
    """A copy of the record with values broken, so restoring them is a correct change."""
    from domain import changes

    result = copy.deepcopy(record)
    for path, value in changes_to_apply.items():
        result = changes.apply_field_update(result, path, value)
    return result
