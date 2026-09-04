"""A triager that costs nothing and needs no network.

This exists so the whole pipeline can be run, tested and demonstrated without
spending anything or depending on a provider being up. It is NOT a substitute
for the model - it only knows the arithmetic relationships that happen to be
easy to express as code, and it will be wrong on anything subtler.

It is deliberately built to mirror the reasoning the real instructions ask for,
so that a run against the stub and a run against the model are comparable, and
so the eval set has a baseline to beat. If the model cannot beat this, the model
is not earning its cost.
"""

from __future__ import annotations

import time

from ..changes import ChangeRequest, TriageClass
from ..triage import TriageOutcome, TriageResult


def _near(a: float, b: float, tol: float = 0.02) -> bool:
    scale = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / scale <= tol


def _kg_per_declared_unit(record: dict) -> float | None:
    """The kg-per-unit figure the EPD states outright, if it states one."""
    unit = record.get("reference_unit")
    for r in record.get("conversion_ratios") or []:
        if r.get("measured_unit") == "kg" and r.get("per_unit") == unit:
            value = r.get("measured_units_per_one_per_unit")
            if isinstance(value, (int, float)):
                return float(value)
    return None


class StubTriager:
    name = "stub"

    def triage(self, record: dict, request: ChangeRequest) -> TriageOutcome:
        started = time.monotonic()
        result = self._classify(record, request)
        return TriageOutcome(
            result=result,
            model="stub",
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _classify(self, record: dict, request: ChangeRequest) -> TriageResult:
        from ..changes import PathError, read_field

        try:
            old = read_field(record, request.field_path or "")
        except PathError:
            return TriageResult(
                triage=TriageClass.UNCLEAR,
                confidence=0.0,
                rationale="Field could not be read.",
            )

        new = request.new_value
        if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
            return TriageResult(
                triage=TriageClass.UNCLEAR,
                confidence=0.3,
                rationale="Non-numeric change; the stub only reasons about numbers.",
            )

        # Cross-check density against thickness and the stated kg-per-unit ratio.
        # density (kg/m3) * thickness (m) should equal kg per m2.
        if request.field_path == "density":
            thickness = record.get("thickness")
            kg_per_unit = _kg_per_declared_unit(record)
            if isinstance(thickness, (int, float)) and thickness and kg_per_unit:
                stored_ok = _near(float(old) * thickness, kg_per_unit)
                proposed_ok = _near(float(new) * thickness, kg_per_unit)
                if stored_ok and not proposed_ok:
                    return TriageResult(
                        triage=TriageClass.IMPLAUSIBLE,
                        confidence=0.95,
                        rationale=(
                            f"Stored density {old} kg/m3 x thickness {thickness} m "
                            f"= {float(old) * thickness:g} kg, which matches the "
                            f"declared {kg_per_unit:g} kg per "
                            f"{record.get('reference_unit')}. The proposed {new} "
                            f"does not."
                        ),
                    )
                if proposed_ok and not stored_ok:
                    return TriageResult(
                        triage=TriageClass.DECIMAL_SLIP,
                        confidence=0.93,
                        rationale=(
                            f"Proposed density {new} kg/m3 x thickness "
                            f"{thickness} m = {float(new) * thickness:g} kg, which "
                            f"matches the declared {kg_per_unit:g} kg per "
                            f"{record.get('reference_unit')}. The stored value "
                            f"does not."
                        ),
                    )

        if old == 0:
            return TriageResult(
                triage=TriageClass.UNCLEAR, confidence=0.3,
                rationale="Stored value is zero; no ratio to reason about.",
            )

        ratio = float(new) / float(old)
        for factor, label in ((10.0, "10x"), (0.1, "one tenth")):
            if _near(ratio, factor, tol=0.05):
                return TriageResult(
                    triage=TriageClass.DECIMAL_SLIP,
                    confidence=0.80,
                    rationale=f"Proposed value is {label} the stored value.",
                )
        for factor in (1000.0, 0.001):
            if _near(ratio, factor, tol=0.05):
                return TriageResult(
                    triage=TriageClass.UNIT_CONVERSION,
                    confidence=0.80,
                    rationale="Factor of 1000 suggests a kg / tonne mix-up.",
                )

        if _near(ratio, 1.0, tol=0.05):
            return TriageResult(
                triage=TriageClass.GENUINE_CORRECTION,
                confidence=0.60,
                rationale="Small adjustment, consistent with re-reading a rounded figure.",
            )

        return TriageResult(
            triage=TriageClass.UNCLEAR,
            confidence=0.35,
            rationale=f"Change of {ratio:.3g}x with no recognisable pattern.",
        )
