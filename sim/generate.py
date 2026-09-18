"""Proposals at volume, for a decision history worth analysing.

The eval has 54 hand-labelled cases and answers "is the pipeline right?". This
answers a different question: "what does the pipeline do over months of
traffic?" - how often the rules settle things, how the model's confidence is
distributed, what a decision costs. Fifty-four cases cannot show that.

Every proposal is built from a real record by the same trick the eval uses:
corrupt a value and propose the original back, or propose something the record
contradicts. What is invented is the arrival of proposals, not the EPDs.

The mix is stationary by default - the same blend of proposal kinds throughout
the window. That matters: if a drift chart bends, it bent because the pipeline
changed its behaviour, not because the input was rigged to bend it. `--drift`
deliberately shifts the mix in the last third, which is how to check that the
nightly job's drift detector actually fires.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from domain.changes import ChangeKind, ChangeRequest, Source

from . import corpus

# Changing this changes every generated history. It is committed so a run is
# reproducible; a run nobody can reproduce is an anecdote.
DEFAULT_SEED = 20260918


@dataclass
class Proposal:
    """One generated proposal, with the record it should be judged against."""

    request: ChangeRequest
    record: dict          # the record as it stands when this is decided
    generator: str        # which shape of proposal this is, for the analysis


class Generator:
    def __init__(self, records: dict[int, dict], rng: random.Random) -> None:
        self.records = records
        self.rng = rng
        self.cross = corpus.cross_checkable(records)
        self.uncheckable = corpus.not_cross_checkable(records)
        self.gwp = corpus.with_gwp(records)
        self.comp = corpus.with_comp(records)
        self.lifespan = corpus.with_lifespan(records)
        self._n = 0

    def _id(self, tag: str) -> str:
        self._n += 1
        return f"sim-{tag}-{self._n:06d}"

    def _request(self, product_id: int, tag: str, submitter: str, **kw) -> ChangeRequest:
        defaults: dict[str, Any] = dict(
            request_id=self._id(tag),
            product_id=product_id,
            kind=ChangeKind.FIELD_UPDATE,
            source=Source.HUMAN,
            submitted_by=submitter,
        )
        return ChangeRequest(**{**defaults, **kw})

    # -- proposal shapes -----------------------------------------------------
    #
    # Each returns a Proposal or None when the chosen record cannot carry that
    # shape. The caller retries with another record.

    def decimal_slip_fix(self) -> Proposal | None:
        """A density out by a factor of ten, corrected back."""
        pid = self.rng.choice(self.cross)
        rec = self.records[pid]
        broken = corpus.corrupt(rec, {"density": round(rec["density"] * 10, 6)})
        return Proposal(
            self._request(
                pid, "decfix", self.rng.choice(REVIEWERS),
                field_path="density", new_value=rec["density"],
                reason="Density is out by a factor of ten against the stated "
                       "weight per declared unit.",
            ),
            broken, "decimal_slip_fix",
        )

    def bad_density(self) -> Proposal | None:
        """A density that contradicts the declared kg per unit."""
        pid = self.rng.choice(self.cross)
        rec = self.records[pid]
        return Proposal(
            self._request(
                pid, "baddens", self.rng.choice(REVIEWERS),
                field_path="density", new_value=round(rec["density"] / 10, 6),
                reason="The datasheet says this is the weight per square metre.",
            ),
            rec, "bad_density",
        )

    def bad_gwp(self) -> Proposal | None:
        """A GWP component that no longer sums to the declared total."""
        pid = self.rng.choice(self.gwp)
        rec = self.records[pid]
        return Proposal(
            self._request(
                pid, "badgwp", self.rng.choice(REVIEWERS),
                field_path="impacts.gwp_fossil",
                new_value=round(rec["impacts"]["gwp_fossil"] * 10, 6),
                reason="I think the fossil figure was under-reported "
                       "by a factor of ten.",
            ),
            rec, "bad_gwp",
        )

    def type_error(self) -> Proposal | None:
        """A comma decimal arriving as text, the way a form sends it."""
        pid = self.rng.choice([p for p in self.records if self.records[p].get("density")])
        rec = self.records[pid]
        return Proposal(
            self._request(
                pid, "typeerr", self.rng.choice(REVIEWERS),
                field_path="density",
                new_value=str(rec["density"]).replace(".", ","),
                reason="Copied straight from the datasheet.",
            ),
            rec, "type_error",
        )

    def no_op(self) -> Proposal | None:
        """Someone proposing the value that is already stored."""
        pid = self.rng.choice(self.lifespan)
        rec = self.records[pid]
        return Proposal(
            self._request(
                pid, "noop", self.rng.choice(REVIEWERS),
                field_path="lifespan", new_value=rec["lifespan"],
                reason="Confirming this is right.",
            ),
            rec, "no_op",
        )

    def bad_path(self) -> Proposal | None:
        """A field that does not exist in the schema."""
        pid = self.rng.choice(list(self.records))
        return Proposal(
            self._request(
                pid, "badpath", self.rng.choice(REVIEWERS),
                field_path="carbon_footprint", new_value=12.0,
                reason="Adding the carbon footprint field.",
            ),
            self.records[pid], "bad_path",
        )

    def composition_repair(self) -> Proposal | None:
        """A composition broken past 100%, put back."""
        pid = self.rng.choice(self.comp)
        rec = self.records[pid]
        comp = corpus.percentages(rec)
        name, correct = comp[0]["name"], comp[0]["percentage"]
        total = sum(c["percentage"] for c in comp)
        path = f"product_integrity.comp[{name}].percentage"
        broken = corpus.corrupt(
            rec, {path: round(correct + (105.0 - total) + 15.0, 4)}
        )
        return Proposal(
            self._request(
                pid, "comprep", self.rng.choice(REVIEWERS),
                field_path=path, new_value=correct,
                reason="The percentages currently add up to more than the "
                       "whole product.",
            ),
            broken, "composition_repair",
        )

    def material_figure(self) -> Proposal | None:
        """A published figure. Always a person, whatever the model concludes."""
        pid = self.rng.choice(list(self.records))
        rec = self.records[pid]
        field, value, why = self.rng.choice([
            ("epd_code", f"EPD-{self.rng.randint(1000, 9999)}",
             "The registration number was reissued."),
            ("reference_unit", "kg", "I believe this EPD is declared per kilogram."),
            ("date", "2030-01-01", "The manufacturer told me it was extended."),
        ])
        if rec.get(field) is None or rec.get(field) == value:
            return None
        return Proposal(
            self._request(
                pid, "material", self.rng.choice(REVIEWERS),
                field_path=field, new_value=value, reason=why,
            ),
            rec, "material_figure",
        )

    def ambiguous_lifespan(self) -> Proposal | None:
        """Nothing in the record settles service life. This is the model's job."""
        pid = self.rng.choice(self.lifespan)
        rec = self.records[pid]
        delta = self.rng.choice([-10.0, -5.0, 5.0, 10.0])
        new = float(rec["lifespan"]) + delta
        if new <= 0:
            return None
        return Proposal(
            self._request(
                pid, "lifespan", self.rng.choice(REVIEWERS),
                field_path="lifespan", new_value=new,
                reason="I recall the declared service life being different.",
            ),
            rec, "ambiguous_lifespan",
        )

    # The four below land on records with no conversion ratio, so no rule can
    # settle them and every one reaches the model. Without these the rules
    # absorb all the arithmetic and the model layer is never exercised.

    def uncheckable_decimal_slip(self) -> Proposal | None:
        """A density out by ten, on a record with nothing to check it against."""
        pid = self.rng.choice(self.uncheckable)
        rec = self.records[pid]
        broken = corpus.corrupt(rec, {"density": round(rec["density"] * 10, 6)})
        return Proposal(
            self._request(
                pid, "uncdec", self.rng.choice(REVIEWERS),
                field_path="density", new_value=rec["density"],
                reason="The decimal point looks to be in the wrong place.",
            ),
            broken, "uncheckable_decimal_slip",
        )

    def uncheckable_lifespan_fix(self) -> Proposal | None:
        """A service life out by ten, put back. Nothing cross-checks lifespan."""
        pid = self.rng.choice(self.lifespan)
        rec = self.records[pid]
        broken = corpus.corrupt(rec, {"lifespan": rec["lifespan"] * 10})
        return Proposal(
            self._request(
                pid, "lifefix", self.rng.choice(REVIEWERS),
                field_path="lifespan", new_value=rec["lifespan"],
                reason=f"A reference service life of {rec['lifespan'] * 10:g} years "
                       f"is not plausible; the EPD states {rec['lifespan']:g}.",
            ),
            broken, "uncheckable_lifespan_fix",
        )

    def unit_confusion(self) -> Proposal | None:
        """A factor of a thousand, the shape of a kg / tonne mix-up."""
        pid = self.rng.choice(self.uncheckable)
        rec = self.records[pid]
        return Proposal(
            self._request(
                pid, "unitconf", self.rng.choice(REVIEWERS),
                field_path="density", new_value=round(rec["density"] * 1000, 6),
                reason="I think this was declared in tonnes per cubic metre.",
            ),
            rec, "unit_confusion",
        )

    def small_adjustment(self) -> Proposal | None:
        """A couple of percent, the shape of a re-read rounded figure."""
        pid = self.rng.choice(self.uncheckable)
        rec = self.records[pid]
        return Proposal(
            self._request(
                pid, "smalladj", self.rng.choice(REVIEWERS),
                field_path="density", new_value=round(rec["density"] * 1.02, 6),
                reason="The published table gives one more significant figure.",
            ),
            rec, "small_adjustment",
        )

    def unpatterned_change(self) -> Proposal | None:
        """A change with no recognisable pattern. Nobody can settle this."""
        pid = self.rng.choice(self.uncheckable)
        rec = self.records[pid]
        factor = self.rng.choice([2.7, 3.4, 0.61, 0.43])
        return Proposal(
            self._request(
                pid, "unpatt", self.rng.choice(REVIEWERS),
                field_path="density", new_value=round(rec["density"] * factor, 6),
                reason="This does not match what we measured.",
            ),
            rec, "unpatterned_change",
        )

    def replacement(self) -> Proposal | None:
        """A manufacturer republishing. Always reviewed."""
        pid = self.rng.choice(list(self.records))
        rec = self.records[pid]
        return Proposal(
            self._request(
                pid, "replace", "feed-watcher",
                kind=ChangeKind.RECORD_REPLACEMENT,
                source=Source.MANUFACTURER_FEED,
                replacement={
                    "product_id": pid,
                    "flag": rec.get("flag", 0),
                    "prod_name": rec.get("prod_name"),
                    "epd_code": rec.get("epd_code"),
                },
                reason="Manufacturer published a new version.",
            ),
            rec, "replacement",
        )

    def malformed_replacement(self) -> Proposal | None:
        """A republication that is not a valid EPD. Refused at the door."""
        pid = self.rng.choice(list(self.records))
        return Proposal(
            self._request(
                pid, "badreplace", "feed-watcher",
                kind=ChangeKind.RECORD_REPLACEMENT,
                source=Source.MANUFACTURER_FEED,
                replacement={"product_id": pid},
                reason="Manufacturer published a new version.",
            ),
            self.records[pid], "malformed_replacement",
        )


# What a proposal of each shape actually IS, for anything that needs to score a
# triager rather than just run one. Only shapes that survive screen() and reach
# the model are listed; the rules settle the others before a model is asked.
#
#   correct    restores the value the record was corrupted away from
#   wrong      the stored value is right and the proposal would damage it
#   ambiguous  the record does not settle it; deferring is the right answer
SHAPE_TRUTH: dict[str, str] = {
    "uncheckable_decimal_slip": "correct",
    "uncheckable_lifespan_fix": "correct",
    "unit_confusion": "wrong",
    "small_adjustment": "wrong",
    "unpatterned_change": "wrong",
    "ambiguous_lifespan": "ambiguous",
}


REVIEWERS = [
    "a.rossi", "b.martinez", "c.okafor", "d.novak", "e.lindqvist", "f.haddad",
]

# How often each shape appears. Weighted so most traffic is ordinary corrections
# and mistakes, with republications and malformed feeds as the long tail - which
# is what a change queue for a document corpus actually looks like.
BASE_MIX: dict[str, int] = {
    "decimal_slip_fix": 10,
    "bad_density": 9,
    "bad_gwp": 9,
    "ambiguous_lifespan": 10,
    "composition_repair": 7,
    "material_figure": 9,
    "type_error": 6,
    "no_op": 4,
    "bad_path": 4,
    "replacement": 4,
    "malformed_replacement": 2,
    "uncheckable_decimal_slip": 9,
    "uncheckable_lifespan_fix": 7,
    "unit_confusion": 6,
    "small_adjustment": 7,
    "unpatterned_change": 4,
}

# The last third of a --drift run. More rejections, fewer clean corrections:
# the shape of a feed that has started sending rubbish.
DRIFT_MIX: dict[str, int] = {
    "decimal_slip_fix": 2,
    "bad_density": 28,
    "bad_gwp": 22,
    "ambiguous_lifespan": 6,
    "composition_repair": 2,
    "material_figure": 6,
    "type_error": 11,
    "no_op": 3,
    "bad_path": 4,
    "replacement": 2,
    "malformed_replacement": 2,
    "uncheckable_decimal_slip": 2,
    "uncheckable_lifespan_fix": 2,
    "unit_confusion": 2,
    "small_adjustment": 2,
    "unpatterned_change": 6,
}


def generate(
    count: int,
    days: int,
    seed: int = DEFAULT_SEED,
    drift: bool = False,
    end: datetime | None = None,
) -> list[Proposal]:
    """`count` proposals, spread over the `days` ending at `end`.

    Returned in time order, because that is the order they would have arrived
    and the order the analysis reads them in.
    """
    rng = random.Random(seed)
    records = corpus.load_records()
    gen = Generator(records, rng)

    end = end or datetime.now().astimezone()
    start = end - timedelta(days=days)
    span = (end - start).total_seconds()

    # Arrival times first, then sorted, so a proposal's shape cannot depend on
    # when it lands - which would be drift smuggled in through the back door.
    times = sorted(start + timedelta(seconds=rng.uniform(0, span)) for _ in range(count))
    drift_from = start + timedelta(days=days * 2 / 3)

    proposals: list[Proposal] = []
    for when in times:
        mix = DRIFT_MIX if (drift and when >= drift_from) else BASE_MIX
        names = list(mix)
        weights = [mix[n] for n in names]

        # A shape can decline a record it cannot use; try a few before moving on.
        for _ in range(10):
            name = rng.choices(names, weights=weights, k=1)[0]
            proposal = getattr(gen, name)()
            if proposal is not None:
                break
        else:
            continue

        proposal.request.submitted_at = when.isoformat()
        proposals.append(proposal)

    return proposals
