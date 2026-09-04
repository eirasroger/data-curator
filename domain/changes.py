"""What a "change to an EPD" is, and what the rules say should happen to it.

There is no LLM in this file and no network call. Everything here is plain
Python that runs in microseconds, which means the rules can be tested
exhaustively and for free. That is deliberate, and it follows the rule already
established in validation.py: code decides what code can decide, and the model
is only asked the questions that are genuinely a matter of judgement.
"""

from __future__ import annotations

import copy
import re
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from . import validation
from .schema import EPDProduct


class ChangeKind(str, Enum):
    """The three ways an EPD record can need to change."""

    FIELD_UPDATE = "field_update"                # someone corrected one value
    RECORD_REPLACEMENT = "record_replacement"    # manufacturer published a new EPD
    EXPIRY = "expiry"                            # the calendar did it; nobody acted


class Source(str, Enum):
    HUMAN = "human"                              # a person reading the record
    MANUFACTURER_FEED = "manufacturer_feed"      # a new EPD arrived
    SCHEDULER = "scheduler"                      # the nightly job noticed something


class Action(str, Enum):
    APPLIED = "applied"
    REJECTED = "rejected"
    PENDING_REVIEW = "pending_review"


class TriageClass(str, Enum):
    """What kind of change this appears to be. Only the model assigns these."""

    DECIMAL_SLIP = "decimal_slip"                # 245 -> 24.5, a misplaced point
    UNIT_CONVERSION = "unit_conversion"          # value was in tonnes, should be kg
    TRANSCRIPTION = "transcription"              # digits read wrong off the PDF
    GENUINE_CORRECTION = "genuine_correction"    # plainly a better value
    IMPLAUSIBLE = "implausible"                  # nothing about this makes sense
    UNCLEAR = "unclear"                          # the model does not know


# Fields whose numbers end up quoted in someone else's report. A wrong value here
# propagates outside the system and cannot be quietly walked back, so no amount
# of model confidence is allowed to auto-apply one. A person signs off, always.
MATERIAL_FIELDS = {
    "date",              # expiry: governs whether the EPD may be cited at all
    "epd_code",          # the registration number identifies the document
    "prod_man",
    "reference_unit",    # changes the meaning of every number attached to it
    "impacts.gwp_total",
    "impacts.gwp_fossil",
    "impacts.gwp_luluc",
    "impacts.gwp_bio",
    "impacts.fw_use",
    "impacts.wdp",
}


class ChangeRequest(BaseModel):
    """One proposal to change one EPD. Immutable once submitted."""

    request_id: str
    product_id: int
    kind: ChangeKind
    source: Source
    submitted_by: str
    submitted_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # FIELD_UPDATE only
    field_path: Optional[str] = Field(
        default=None,
        description=(
            "Dotted path, e.g. 'density' or 'impacts.gwp_total'. List entries are "
            "addressed by name: 'product_integrity.comp[Basalt].percentage'."
        ),
    )
    new_value: Any = None

    # RECORD_REPLACEMENT only: the whole new record
    replacement: Optional[dict] = None

    reason: str = Field(description="Why the submitter thinks this is right.")


class Decision(BaseModel):
    """What the system concluded about a change request."""

    request_id: str
    action: Action
    decided_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # Which rule produced this outcome, in words a person can read.
    reason: str
    # Deterministic findings: validation errors the change would introduce.
    blocking_issues: list[str] = Field(default_factory=list)
    # Model output, only present when the model was actually consulted.
    triage: Optional[TriageClass] = None
    confidence: Optional[float] = None
    rationale: Optional[str] = None
    old_value: Any = None


# ---------------------------------------------------------------------------
# Field paths
# ---------------------------------------------------------------------------

_SEGMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[(.+)\])?$")


class PathError(ValueError):
    """The field path does not point at anything in this record."""


def _walk(record: dict, path: str) -> tuple[Any, str, Any]:
    """Resolve a dotted path to (container, key, current value).

    Returning the container and key rather than just the value is what lets the
    caller write back to it. `comp[Basalt]` selects the entry in the list whose
    `name` is "Basalt" - EPD sub-objects are identified by name, not position,
    because positions shift whenever the extractor re-runs.
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
    """Return a COPY of the record with one field changed.

    A copy, not an edit in place: the caller needs to validate the hypothetical
    result before deciding whether to keep it, and must still hold the original
    if the answer turns out to be no.
    """
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
    """Validation errors the change would INTRODUCE.

    Comparing before against after, rather than just checking `after`, matters:
    plenty of these records already carry warnings and the odd error. A change
    should be judged on whether it makes things worse, not on whether it leaves
    the record perfect - otherwise no change to an already-imperfect record
    could ever be accepted.
    """
    return sorted(_error_fingerprints(after) - _error_fingerprints(before))


def fixed_validation_errors(before: dict, after: dict) -> list[str]:
    """Validation errors the change would REMOVE."""
    return sorted(_error_fingerprints(before) - _error_fingerprints(after))


def is_material(path: str) -> bool:
    return path in MATERIAL_FIELDS


def screen(record: dict, request: ChangeRequest) -> Optional[Decision]:
    """Decide without the model, if the rules already settle it.

    Returns a Decision when the answer is certain, or None when the request
    survives to the point where judgement is actually required. Every request
    passes through here first, so the model is never asked something a rule
    could have answered - which is both cheaper and more reliable.
    """
    if request.kind is ChangeKind.EXPIRY:
        # The nightly job produced this by reading a date. There is no judgement
        # to make and nothing to second-guess.
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
        # A replacement is a whole document, so it has to BE a whole document.
        # EPDProduct is the same model the extractor produces, so validating
        # here means a malformed replacement is refused at the door rather than
        # discovered later by something that tried to read a missing field.
        try:
            EPDProduct.model_validate(request.replacement)
        except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
            return Decision(
                request_id=request.request_id,
                action=Action.REJECTED,
                reason="The replacement record does not match the EPD schema.",
                blocking_issues=[str(exc).splitlines()[0][:300]],
            )

        # A valid new document still always gets human eyes. It is not a
        # correction to a number, it is a different document superseding one.
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
        # A number must stay a number. This catches "9,5" arriving as a string
        # from a form, which would silently poison every later calculation.
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
        # The record contradicted itself, this change resolves it, and the field
        # is not one people quote. That is about as close to certain as a change
        # gets, and asking a model would add cost and doubt, not accuracy.
        return Decision(
            request_id=request.request_id,
            action=Action.APPLIED,
            reason=f"Resolves an existing inconsistency: {repaired[0]}",
            old_value=old_value,
        )

    return None  # genuinely needs judgement


def finalise(
    request: ChangeRequest,
    old_value: Any,
    triage: TriageClass,
    confidence: float,
    rationale: str,
    confidence_floor: float = 0.85,
) -> Decision:
    """Turn the model's opinion into an outcome, under rules the model cannot bend.

    The model classifies and scores. It does not decide. The three rules below
    are the decision, and they live here in code so they can be read, argued
    with and tested - rather than inside a prompt where nobody can see them.
    """
    base = dict(
        request_id=request.request_id,
        triage=triage,
        confidence=confidence,
        rationale=rationale,
        old_value=old_value,
    )

    # ORDER MATTERS, and getting it wrong here was caught by the eval.
    #
    # The material-field rule has to come FIRST. If the implausible check runs
    # first, an "implausible" verdict lets the model reject a published figure
    # on its own - which is the same authority we just said it must not have.
    # A wrongly rejected correction is not harmless: the error it was trying to
    # fix stays in the data, and the person who reported it is told nothing.
    #
    # So for these fields the model's opinion is advice attached to a review,
    # never a verdict.
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


def is_expired(record: dict, on: Optional[date] = None) -> bool:
    """Whether the EPD's validity has lapsed.

    `date` in these records is the EXPIRY date, not the publication date - see
    the field description in schema.py.
    """
    raw = record.get("date")
    if not raw:
        return False
    try:
        return date.fromisoformat(str(raw)) < (on or date.today())
    except ValueError:
        return False


class ReviewDecision(BaseModel):
    """A person's verdict on a request the pipeline parked.

    This does not overwrite what the pipeline concluded - it is appended to the
    same request's history as a second event. Both answers stay on the record:
    what the machine decided, and what the human decided afterwards.
    """

    request_id: str
    reviewer: str = Field(description="who made this call; a person, not a service")
    approve: bool
    note: str = Field(default="", description="why, for whoever reads this later")


class MessageType(str, Enum):
    """What a Pub/Sub message carries. The worker branches on this."""

    CHANGE_REQUEST = "change_request"
    REVIEW = "review"
