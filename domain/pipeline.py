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


class ReviewOutcome(str, Enum):
    """What happened to a review, in enough detail for the caller to answer.

    A bool cannot carry this. "Unknown request" is a broken message and belongs
    in the dead-letter queue; "not pending" is a duplicate or a stale verdict and
    must be acknowledged, because redelivering it will produce the same answer
    forever. The worker maps these to HTTP status codes.
    """

    APPLIED = "applied"
    REJECTED = "rejected"
    UNKNOWN_REQUEST = "unknown_request"
    NOT_PENDING = "not_pending"


def apply_review(review: ReviewDecision, *, store: Any) -> ReviewOutcome:
    """Record a person's verdict on a parked change, and act on it.

    Lives here rather than in the worker because two things now take reviews -
    the Pub/Sub handler and the operator page running without a queue - and a
    second implementation is a second set of rules about what a review means.

    A verdict is only accepted on a request that is actually waiting for one.
    That single condition closes two holes:

      - Pub/Sub delivers at least once, so the same review arrives twice. The
        second one used to write a second version of the record. handle_change
        has guarded against this since the beginning; this path never did.
      - screen() rejects a change that would break the record's internal
        consistency, but the rejected request stays in change_requests. An
        approval arriving afterwards used to apply it anyway, without
        re-validating - so the deterministic rules had a door around them.

    Both are the same question: what does this request's history already say?
    """
    # The latest event for this request. A LEFT JOIN from change_requests, so a
    # row comes back if the request exists at all and `action` is null when it
    # exists but nothing has decided it yet.
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

    # event_type "reviewed", not "decided": the pipeline's original conclusion
    # stays in the log untouched. Both answers are on the record.
    store.save_event(decision, request.product_id,
                     event_type="reviewed", actor=review.reviewer)
    return ReviewOutcome.APPLIED if review.approve else ReviewOutcome.REJECTED
