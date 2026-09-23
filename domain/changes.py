"""Change requests, decisions, and the rules that decide them. No LLM calls here."""

from __future__ import annotations

import copy
import re
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from . import validation
from .schema import EPDProduct


class ChangeKind(str, Enum):
    FIELD_UPDATE = "field_update"                # one value corrected
    RECORD_REPLACEMENT = "record_replacement"    # manufacturer published a new EPD
    EXPIRY = "expiry"                            # validity date passed


class Source(str, Enum):
    HUMAN = "human"
    MANUFACTURER_FEED = "manufacturer_feed"
    SCHEDULER = "scheduler"                      # the nightly job


class Action(str, Enum):
    APPLIED = "applied"
    REJECTED = "rejected"
    PENDING_REVIEW = "pending_review"


class TriageClass(str, Enum):
    """The LLM's classification of a change."""

    DECIMAL_SLIP = "decimal_slip"                # 245 -> 24.5
    UNIT_CONVERSION = "unit_conversion"          # tonnes vs kg
    TRANSCRIPTION = "transcription"              # digits misread from the PDF
    GENUINE_CORRECTION = "genuine_correction"
    IMPLAUSIBLE = "implausible"
    UNCLEAR = "unclear"


# Published figures that others quote. Changes to these always go to a person.
MATERIAL_FIELDS = {
    "date",
    "epd_code",
    "prod_man",
    "reference_unit",
    "impacts.gwp_total",
    "impacts.gwp_fossil",
    "impacts.gwp_luluc",
    "impacts.gwp_bio",
    "impacts.fw_use",
    "impacts.wdp",
}


# Raised from 0.85, which auto-applied 5 wrong changes in a 2000-proposal run.
CONFIDENCE_FLOOR = 0.90


class ChangeRequest(BaseModel):
    """One proposal to change one EPD."""

    request_id: str
    product_id: int
    kind: ChangeKind
    source: Source
    # Length caps because both fields go into the LLM prompt.
    submitted_by: str = Field(max_length=200)
    submitted_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    # FIELD_UPDATE only
    field_path: str | None = Field(
        default=None,
        description=(
            "Dotted path, e.g. 'density' or 'impacts.gwp_total'. List entries are "
            "addressed by name: 'product_integrity.comp[Basalt].percentage'."
        ),
    )
    new_value: Any = None

    # RECORD_REPLACEMENT only
    replacement: dict | None = None

    reason: str = Field(
        max_length=2000, description="Why the submitter thinks this is right."
    )


class Decision(BaseModel):
    """The outcome for one change request."""

    request_id: str
    action: Action
    decided_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    reason: str
    # Validation errors the change would introduce.
    blocking_issues: list[str] = Field(default_factory=list)
    # Set only when the LLM was consulted.
    triage: TriageClass | None = None
    confidence: float | None = None
    rationale: str | None = None
    old_value: Any = None


# ---------------------------------------------------------------------------
# Field paths
# ---------------------------------------------------------------------------

_SEGMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[(.+)\])?$")


class PathError(ValueError):
    """The field path does not point at anything in this record."""


def _walk(record: dict, path: str) -> tuple[Any, str, Any]:
    """Resolve a dotted path to (container, key, current value).

    `comp[Basalt]` selects list entries by name, since positions can shift.
    """
    container: Any = record
    key: str = ""

    parts = path.split(".")
    for i, part in enumerate(parts):
        m = _SEGMENT.match(part)
        if not m:
            raise PathError(f"malformed path segment {part!r} in {path!r}")
        name, selector = m.group(1), m.group(2)

        if not isinstance(container, dict) or name not in container:
            raise PathError(
                f"{'.'.join(parts[:i + 1])!r} does not exist in this record"
            )

        if selector is None:
            key = name
            if i < len(parts) - 1:
                container = container[name]
            continue

        items = container[name]
        if not isinstance(items, list):
            raise PathError(
                f"{name!r} is not a list, so {part!r} cannot select from it"
            )
        match = next(
            (x for x in items if isinstance(x, dict) and x.get("name") == selector),
            None,
        )
        if match is None:
            available = [x.get("name") for x in items if isinstance(x, dict)]
            raise PathError(
                f"no entry named {selector!r} in {name!r}; have {available}"
            )
        container = match
        key = ""
        if i == len(parts) - 1:
            raise PathError(f"path {path!r} selects an object, not a field of one")

    if key == "":
        raise PathError(f"path {path!r} does not end at a field")
    return container, key, container[key]


def read_field(record: dict, path: str) -> Any:
    _, _, value = _walk(record, path)
    return value


def apply_field_update(record: dict, path: str, new_value: Any) -> dict:
    """Return a copy of the record with one field changed."""
    updated = copy.deepcopy(record)
    container, key, _ = _walk(updated, path)
    container[key] = new_value
    return updated


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

def _error_fingerprints(record: dict) -> set[str]:
    return {
        f"{i.field}: {i.detail}"
        for i in validation.errors(validation.validate(record))
    }


def new_validation_errors(before: dict, after: dict) -> list[str]:
    """Errors the change would introduce. Existing errors are ignored."""
    return sorted(_error_fingerprints(after) - _error_fingerprints(before))


def fixed_validation_errors(before: dict, after: dict) -> list[str]:
    """Errors the change would remove."""
    return sorted(_error_fingerprints(before) - _error_fingerprints(after))


def is_material(path: str) -> bool:
    return path in MATERIAL_FIELDS


def screen(record: dict, request: ChangeRequest) -> Decision | None:
    """Decide by rule alone. Returns None when the LLM is needed."""
    if request.kind is ChangeKind.EXPIRY:
        return Decision(
            request_id=request.request_id,
            action=Action.APPLIED,
            reason="Expiry is a fact about the calendar, not a proposal.",
        )

    if request.kind is ChangeKind.RECORD_REPLACEMENT:
        if not request.replacement:
            return Decision(
                request_id=request.request_id,
                action=Action.REJECTED,
                reason="Replacement requested but no replacement record supplied.",
            )
        try:
            EPDProduct.model_validate(request.replacement)
        except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
            return Decision(
                request_id=request.request_id,
                action=Action.REJECTED,
                reason="The replacement record does not match the EPD schema.",
                blocking_issues=[str(exc).splitlines()[0][:300]],
            )

        # A new document supersedes the record, so a person always reviews it.
        return Decision(
            request_id=request.request_id,
            action=Action.PENDING_REVIEW,
            reason="A replacement supersedes the whole record and is always reviewed.",
        )

    # --- field update ---
    if not request.field_path:
        return Decision(
            request_id=request.request_id,
            action=Action.REJECTED,
            reason="A field update must say which field.",
        )

    try:
        old_value = read_field(record, request.field_path)
    except PathError as exc:
        return Decision(
            request_id=request.request_id,
            action=Action.REJECTED,
            reason=f"Invalid field path: {exc}",
        )

    if old_value == request.new_value:
        return Decision(
            request_id=request.request_id,
            action=Action.REJECTED,
            reason="The proposed value is the value already stored.",
            old_value=old_value,
        )

    if old_value is not None and type(old_value) is not type(request.new_value):
        # Catches values like "9,5" arriving as a string from a form.
        return Decision(
            request_id=request.request_id,
            action=Action.REJECTED,
            reason=(
                f"Type mismatch: {request.field_path} holds "
                f"{type(old_value).__name__}, proposal is "
                f"{type(request.new_value).__name__}."
            ),
            old_value=old_value,
        )

    after = apply_field_update(record, request.field_path, request.new_value)

    introduced = new_validation_errors(record, after)
    if introduced:
        return Decision(
            request_id=request.request_id,
            action=Action.REJECTED,
            reason="The change would break internal consistency of the record.",
            blocking_issues=introduced,
            old_value=old_value,
        )

    repaired = fixed_validation_errors(record, after)
    if repaired and not is_material(request.field_path):
        # Fixes an existing inconsistency on an ordinary field: safe to apply.
        return Decision(
            request_id=request.request_id,
            action=Action.APPLIED,
            reason=f"Resolves an existing inconsistency: {repaired[0]}",
            old_value=old_value,
        )

    return None


def finalise(
    request: ChangeRequest,
    old_value: Any,
    triage: TriageClass,
    confidence: float,
    rationale: str,
    confidence_floor: float = CONFIDENCE_FLOOR,
) -> Decision:
    """Turn the LLM's classification into an outcome."""
    base = dict(
        request_id=request.request_id,
        triage=triage,
        confidence=confidence,
        rationale=rationale,
        old_value=old_value,
    )

    # Must come first, so an "implausible" verdict cannot reject a published figure.
    if is_material(request.field_path or ""):
        return Decision(
            action=Action.PENDING_REVIEW,
            reason=(
                f"{request.field_path} is a published figure; these are never "
                f"decided without a person, whatever the model concluded."
            ),
            **base,
        )

    if triage is TriageClass.IMPLAUSIBLE:
        return Decision(
            action=Action.REJECTED, reason="Classified as implausible.", **base
        )

    if confidence < confidence_floor:
        return Decision(
            action=Action.PENDING_REVIEW,
            reason=(
                f"Confidence {confidence:.2f} is below the "
                f"{confidence_floor:.2f} floor."
            ),
            **base,
        )

    return Decision(
        action=Action.APPLIED,
        reason=f"Classified {triage.value} with confidence {confidence:.2f}.",
        **base,
    )


def is_expired(record: dict, on: date | None = None) -> bool:
    """Whether the EPD's expiry date (the `date` field) has passed."""
    raw = record.get("date")
    if not raw:
        return False
    try:
        return date.fromisoformat(str(raw)) < (on or date.today())
    except ValueError:
        return False


class ReviewDecision(BaseModel):
    """A person's verdict on a parked request. Recorded beside the original decision."""

    request_id: str
    reviewer: str = Field(description="who made this call; a person, not a service")
    approve: bool
    note: str = Field(default="", description="why, for whoever reads this later")


class MessageType(str, Enum):
    """What a Pub/Sub message carries."""

    CHANGE_REQUEST = "change_request"
    REVIEW = "review"
