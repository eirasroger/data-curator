"""The one question the system asks a model.

A change request has survived every deterministic rule in changes.py. It breaks
no arithmetic, the field exists, the type is right, and it fixes nothing
obvious. So: is this proposal a correction, or is it a mistake?

That is a judgement about a physical product, and it is the only thing here the
model is asked. It classifies and scores. What HAPPENS as a result is decided by
changes.finalise(), in code, under rules the model cannot influence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

from pydantic import BaseModel, Field

from .changes import ChangeRequest, TriageClass

# Per million tokens, from the OpenAI pricing page. Only used to record what a
# decision cost; nothing depends on these being exact.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
}


class TriageResult(BaseModel):
    """What the model returns. Constrained by schema, not by asking politely."""

    triage: TriageClass = Field(
        description="What kind of change this is. See the instructions for each value."
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="How sure you are of the classification. Be honest: a low "
                    "score sends this to a person, which is the right outcome "
                    "when the record does not settle it.",
    )
    rationale: str = Field(
        description="One or two sentences, citing the specific values in the "
                    "record that led you there.",
    )


@dataclass
class TriageOutcome:
    """The result plus what it cost to get it."""

    result: TriageResult
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int

    @property
    def cost_usd(self) -> float:
        pin, pout = PRICES_PER_MTOK.get(self.model, (0.0, 0.0))
        return (self.prompt_tokens * pin + self.completion_tokens * pout) / 1_000_000


INSTRUCTIONS = """\
You review proposed corrections to Environmental Product Declarations (EPDs) for \
construction products. An EPD is a published document; its numbers get quoted in \
other people's calculations, so a wrong correction is worse than no correction.

Someone has proposed changing one field. Decide what kind of change it is.

Classify as exactly one of:

  decimal_slip        The stored value has a misplaced decimal point and the
                      proposal fixes it.
  unit_conversion     The stored value is expressed in the wrong unit and the
                      proposal converts it correctly.
  transcription       Digits were read incorrectly from the source document and
                      the proposal corrects them.
  genuine_correction  The proposal is a better value for some other reason.
  implausible         The proposal is WRONG. It contradicts other values in the
                      record, or is physically unreasonable for this product.
                      Use this whenever the stored value is the correct one.
  unclear             The record does not contain enough to tell.

The record is internally cross-referenced, and that is your main evidence. In \
particular: density (kg/m3) multiplied by thickness (m) should equal the stated \
kg-per-m2 conversion ratio. Declared A1-A3 GWP total should be close to the sum \
of its fossil, land-use and biogenic parts, and biogenic GWP is often negative \
because the product stores carbon. Material percentages describe the product \
only, never its packaging.

Check the proposal against those relationships before you classify. If the \
stored value is consistent with the rest of the record and the proposed value is \
not, the answer is implausible - regardless of how confident the submitter \
sounds. Say which numbers you used in your rationale.\
"""


def build_context(record: dict, request: ChangeRequest) -> str:
    """The facts the model needs, and not the whole record.

    Sending the full record would be ~3000 tokens of mostly irrelevant detail and
    would bury the two or three numbers that actually settle the question. What
    goes in here is the identity of the product, the physical properties that
    cross-check each other, the declared impacts, and the proposal itself.
    """
    lines: list[str] = []
    add = lines.append

    add(f"EPD: {record.get('prod_name')}")
    add(f"Manufacturer: {record.get('prod_man')}")
    add(f"Registration: {record.get('epd_code')}")
    add(f"Declared unit: {record.get('reference_unit')}")
    add(f"Expiry date: {record.get('date')}")

    add("")
    add("Physical properties as currently stored:")
    for field in ("thickness", "density", "lifespan"):
        if record.get(field) is not None:
            unit = {"thickness": " m", "density": " kg/m3", "lifespan": " years"}[field]
            add(f"  {field} = {record[field]}{unit}")

    ratios = record.get("conversion_ratios") or []
    if ratios:
        add("  conversion ratios stated in the EPD:")
        for r in ratios:
            add(
                f"    {r.get('measured_units_per_one_per_unit')} "
                f"{r.get('measured_unit')} per 1 {r.get('per_unit')}"
            )

    impacts = record.get("impacts") or {}
    if any(v is not None for v in impacts.values()):
        add("")
        add("A1-A3 impacts as currently stored:")
        for k, v in impacts.items():
            if v is not None:
                add(f"  {k} = {v}")

    comp = (record.get("product_integrity") or {}).get("comp") or []
    if comp:
        add("")
        add("Material composition (% of product weight):")
        for c in comp:
            add(f"  {c.get('name')} = {c.get('percentage')}")

    add("")
    add("PROPOSED CHANGE")
    add(f"  field: {request.field_path}")
    add(f"  currently stored: {_read_safely(record, request.field_path)}")
    add(f"  proposed value: {request.new_value}")
    add(f"  submitted by: {request.submitted_by}")
    add(f"  their reason: {request.reason}")

    return "\n".join(lines)


def _read_safely(record: dict, path: Optional[str]) -> Any:
    from .changes import PathError, read_field

    if not path:
        return None
    try:
        return read_field(record, path)
    except PathError:
        return None


class Triager(Protocol):
    """Anything that can answer the triage question.

    The pipeline depends on this shape, not on OpenAI. Swapping provider, or
    dropping in the free stub for a local run, changes one line of configuration
    and nothing else.
    """

    name: str

    def triage(self, record: dict, request: ChangeRequest) -> TriageOutcome: ...


def get_triager(provider: str, model: str = "gpt-5-mini") -> Triager:
    """Build a triager by name. Unknown names fail loudly rather than defaulting.

    Defaulting to the stub on a typo would mean a deployment quietly running on
    fake answers, which is the kind of failure nobody notices for a month.
    """
    if provider == "stub":
        from .providers.stub import StubTriager

        return StubTriager()
    if provider == "openai":
        from .providers.openai_provider import OpenAITriager

        return OpenAITriager(model=model)
    raise ValueError(f"unknown triage provider {provider!r}; expected 'openai' or 'stub'")


__all__ = [
    "INSTRUCTIONS",
    "PRICES_PER_MTOK",
    "TriageOutcome",
    "TriageResult",
    "Triager",
    "build_context",
    "get_triager",
]
