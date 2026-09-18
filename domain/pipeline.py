"""Deciding a single change request, start to finish.

This is the whole decision in one place. The worker calls it, and so does the
eval - which matters, because an eval that exercises a re-implementation of the
pipeline measures the re-implementation.

The order is fixed and it is the point of the design:

    1. screen()    rules only. Free, instant, and settles most requests.
    2. triage()    the model, but ONLY on what survived step 1.
    3. finalise()  rules again. The model's answer is an input to the decision,
                   never the decision itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
        # The model being unavailable must never turn into a guess, and it must
        # never turn into an automatic rejection either - a provider outage
        # would then quietly discard everyone's corrections. It parks the
        # request for a person, which is the honest answer to "we do not know".
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
    """Produce the new record, if the decision was to apply the change.

    Returns the record unchanged for any other action, so a caller can write the
    result unconditionally without first re-deriving whether anything happened.
    """
    if decision.action is not Action.APPLIED:
        return record

    if request.kind is changes.ChangeKind.EXPIRY:
        updated = dict(record)
        updated["status"] = "expired"
        return updated

    if request.kind is changes.ChangeKind.RECORD_REPLACEMENT and request.replacement:
        return dict(request.replacement)

    return changes.apply_field_update(record, request.field_path or "", request.new_value)


def apply_review(review: ReviewDecision, *, store: Any) -> bool:
    """Record a person's verdict on a parked change, and act on it.

    Lives here rather than in the worker because two things now take reviews -
    the Pub/Sub handler and the operator page running without a queue - and a
    second implementation is a second set of rules about what a review means.

    Returns False when the request is not one we hold.
    """
    original = store.get_request(review.request_id)
    if original is None:
        return False

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
            return False
        updated = apply_decision(record, request, decision)
        status = "expired" if request.kind is ChangeKind.EXPIRY else "active"
        store.save_version(
            updated, request.product_id, int(record.get("_version", 1)) + 1,
            request.request_id, status=status,
        )

    # event_type "reviewed", not "decided": the pipeline's original conclusion
    # stays in the log untouched. Both answers are on the record.
    store.save_event(decision, request.product_id,
                     event_type="reviewed", actor=review.reviewer)
    return True
