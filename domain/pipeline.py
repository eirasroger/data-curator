"""Decide one change request: screen() by rule, then triage() by LLM, then finalise()."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from . import changes
from .changes import Action, ChangeKind, ChangeRequest, Decision, ReviewDecision
from .triage import TriageOutcome, Triager

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    """What happened to one change request."""

    decision: Decision
    triage: TriageOutcome | None  # None when the rules settled it alone

    @property
    def used_model(self) -> bool:
        return self.triage is not None

    @property
    def cost_usd(self) -> float:
        return self.triage.cost_usd if self.triage else 0.0

    @property
    def latency_ms(self) -> int:
        return self.triage.latency_ms if self.triage else 0


def decide(record: dict, request: ChangeRequest, triager: Triager) -> Outcome:
    """Decide what to do with one proposed change."""
    settled = changes.screen(record, request)
    if settled is not None:
        return Outcome(decision=settled, triage=None)

    try:
        outcome = triager.triage(record, request)
    except Exception as exc:  # noqa: BLE001
        # An LLM outage sends the request to a person.
        log.warning("triage unavailable for %s: %s", request.request_id, exc)
        return Outcome(
            decision=Decision(
                request_id=request.request_id,
                action=Action.PENDING_REVIEW,
                reason=f"Triage unavailable ({type(exc).__name__}); parked for review.",
                old_value=_old_value(record, request),
            ),
            triage=None,
        )

    decision = changes.finalise(
        request=request,
        old_value=_old_value(record, request),
        triage=outcome.result.triage,
        confidence=outcome.result.confidence,
        rationale=outcome.result.rationale,
    )
    return Outcome(decision=decision, triage=outcome)


def _old_value(record: dict, request: ChangeRequest):
    if not request.field_path:
        return None
    try:
        return changes.read_field(record, request.field_path)
    except changes.PathError:
        return None


def apply_decision(record: dict, request: ChangeRequest, decision: Decision) -> dict:
    """The updated record if the change was applied, otherwise the record unchanged."""
    if decision.action is not Action.APPLIED:
        return record

    if request.kind is changes.ChangeKind.EXPIRY:
        updated = dict(record)
        updated["status"] = "expired"
        return updated

    if request.kind is changes.ChangeKind.RECORD_REPLACEMENT and request.replacement:
        return dict(request.replacement)

    return changes.apply_field_update(record, request.field_path or "", request.new_value)


class ReviewOutcome(str, Enum):
    """Result of applying a review. The worker maps these to HTTP status codes."""

    APPLIED = "applied"
    REJECTED = "rejected"
    UNKNOWN_REQUEST = "unknown_request"
    NOT_PENDING = "not_pending"


def apply_review(review: ReviewDecision, *, store: Any) -> ReviewOutcome:
    """Record a person's verdict and act on it. Used by the worker and the dashboard.

    Accepts only requests awaiting review, so duplicates and rejected ones are ignored.
    """
    status = store.change_status(review.request_id)
    if status is None:
        return ReviewOutcome.UNKNOWN_REQUEST
    if status.get("action") != Action.PENDING_REVIEW.value:
        return ReviewOutcome.NOT_PENDING

    original = store.get_request(review.request_id)
    if original is None:
        return ReviewOutcome.UNKNOWN_REQUEST

    from .store import to_change_request

    request = to_change_request(original)
    decision = Decision(
        request_id=review.request_id,
        action=Action.APPLIED if review.approve else Action.REJECTED,
        reason=f"Reviewed by {review.reviewer}: "
               f"{'approved' if review.approve else 'rejected'}."
               + (f" {review.note}" if review.note else ""),
    )

    if review.approve:
        record = store.current_record(request.product_id)
        if record is None:
            return ReviewOutcome.UNKNOWN_REQUEST
        updated = apply_decision(record, request, decision)
        status_value = "expired" if request.kind is ChangeKind.EXPIRY else "active"
        store.save_version(
            updated, request.product_id, int(record.get("_version", 1)) + 1,
            request.request_id, status=status_value,
        )

    # A separate "reviewed" event keeps the pipeline's original decision in the log.
    store.save_event(decision, request.product_id,
                     event_type="reviewed", actor=review.reviewer)
    return ReviewOutcome.APPLIED if review.approve else ReviewOutcome.REJECTED
