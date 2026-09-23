"""Tests for the change rules. All values come from seed/epds.json."""

from __future__ import annotations

import datetime as dt

import pytest

from domain import changes
from domain.changes import Action, ChangeKind, ChangeRequest, Source, TriageClass


def request_for(
    record: dict,
    field_path: str | None = None,
    new_value=None,
    kind: ChangeKind = ChangeKind.FIELD_UPDATE,
    **kw,
) -> ChangeRequest:
    return ChangeRequest(
        request_id="req-test",
        product_id=record["product_id"],
        kind=kind,
        source=kw.pop("source", Source.HUMAN),
        submitted_by=kw.pop("submitted_by", "roger"),
        field_path=field_path,
        new_value=new_value,
        reason=kw.pop("reason", "spotted while reviewing the PDF"),
        **kw,
    )


# ---------------------------------------------------------------------------
# Field paths
# ---------------------------------------------------------------------------

def test_reads_a_top_level_field(record):
    assert changes.read_field(record, "density") == 95.0


def test_reads_a_nested_field(record):
    assert changes.read_field(record, "impacts.gwp_total") == 11.0


def test_reads_a_list_entry_selected_by_name(record):
    # 'Basalt' is one of the five materials in this product's composition.
    assert changes.read_field(
        record, "product_integrity.comp[Basalt].percentage"
    ) == 57.5


def test_unknown_field_is_an_error(record):
    with pytest.raises(changes.PathError):
        changes.read_field(record, "nonsense")


def test_unknown_list_entry_names_what_is_available(record):
    with pytest.raises(changes.PathError) as exc:
        changes.read_field(record, "product_integrity.comp[Titanium].percentage")
    assert "Basalt" in str(exc.value)


def test_applying_a_change_does_not_touch_the_original(record):
    before = record["density"]
    updated = changes.apply_field_update(record, "density", 9.5)
    assert updated["density"] == 9.5
    assert record["density"] == before, "the original record must be untouched"


# ---------------------------------------------------------------------------
# Rejected by the rules, no model involved
# ---------------------------------------------------------------------------

def test_invalid_field_path_is_rejected(record):
    d = changes.screen(record, request_for(record, "does.not.exist", 1.0))
    assert d.action is Action.REJECTED
    assert "Invalid field path" in d.reason


def test_no_op_change_is_rejected(record):
    d = changes.screen(record, request_for(record, "density", 95.0))
    assert d.action is Action.REJECTED
    assert "already stored" in d.reason


def test_type_change_is_rejected(record):
    """A comma decimal sent as text is rejected."""
    d = changes.screen(record, request_for(record, "density", "9,5"))
    assert d.action is Action.REJECTED
    assert "Type mismatch" in d.reason


def test_change_that_breaks_gwp_arithmetic_is_rejected(record):
    """gwp_total 11.0 = 12.1 + 0.008 - 1.06; fossil 121 breaks the sum."""
    d = changes.screen(record, request_for(record, "impacts.gwp_fossil", 121.0))
    assert d.action is Action.REJECTED
    assert d.blocking_issues, "the failing check should be recorded"
    assert "gwp_total" in d.blocking_issues[0]


def test_replacement_without_a_record_is_rejected(record):
    d = changes.screen(
        record, request_for(record, kind=ChangeKind.RECORD_REPLACEMENT)
    )
    assert d.action is Action.REJECTED


# ---------------------------------------------------------------------------
# Settled by the rules without the model
# ---------------------------------------------------------------------------

def test_expiry_is_applied_without_question(record):
    d = changes.screen(
        record,
        request_for(record, kind=ChangeKind.EXPIRY, source=Source.SCHEDULER,
                    submitted_by="reconcile-job"),
    )
    assert d.action is Action.APPLIED
    assert "calendar" in d.reason


def test_valid_replacement_goes_to_a_person(record):
    d = changes.screen(
        record,
        request_for(record, kind=ChangeKind.RECORD_REPLACEMENT,
                    source=Source.MANUFACTURER_FEED,
                    replacement={"product_id": 6, "flag": 0,
                                 "prod_name": "SmartRoof Base v2",
                                 "epd_code": "S-P-05317-v2"}),
    )
    assert d.action is Action.PENDING_REVIEW


def test_replacement_that_is_not_a_valid_epd_is_rejected(record):
    """A replacement must be a complete EPDProduct."""
    d = changes.screen(
        record,
        request_for(record, kind=ChangeKind.RECORD_REPLACEMENT,
                    source=Source.MANUFACTURER_FEED,
                    replacement={"prod_name": "SmartRoof Base v2"}),
    )
    assert d.action is Action.REJECTED
    assert "EPD schema" in d.reason
    assert d.blocking_issues


def test_change_that_repairs_an_inconsistency_is_applied(records_by_id):
    """Fixing a composition that sums over 100% is applied without the model."""
    import copy

    broken = copy.deepcopy(records_by_id[6])
    # Basalt 57.5 -> 87.5 makes the composition total 119%.
    broken["product_integrity"]["comp"][0]["percentage"] = 87.5
    assert changes.new_validation_errors(records_by_id[6], broken), "setup check"

    d = changes.screen(
        broken,
        request_for(broken, "product_integrity.comp[Basalt].percentage", 57.5),
    )
    assert d.action is Action.APPLIED
    assert "inconsistency" in d.reason


# ---------------------------------------------------------------------------
# Passed through to the model
# ---------------------------------------------------------------------------

def test_plausible_looking_change_needs_judgement(record):
    """Service life has no cross-check, so lifespan 50 -> 45 goes to the model."""
    assert changes.screen(record, request_for(record, "lifespan", 45.0)) is None


def test_headline_impact_change_needs_judgement(record):
    assert changes.screen(record, request_for(record, "impacts.gwp_total", 11.05)) is None


# ---------------------------------------------------------------------------
# What the model's answer is allowed to do
# ---------------------------------------------------------------------------

def test_material_field_is_never_auto_applied_however_confident(record):
    """Published fields go to a person even at high confidence."""
    d = changes.finalise(
        request_for(record, "impacts.gwp_total", 11.05),
        old_value=11.0,
        triage=TriageClass.GENUINE_CORRECTION,
        confidence=0.99,
        rationale="rounding of the declared total",
    )
    assert d.action is Action.PENDING_REVIEW
    assert "published figure" in d.reason


def test_confident_change_to_an_ordinary_field_is_applied(record):
    d = changes.finalise(
        request_for(record, "density", 9.5),
        old_value=95.0,
        triage=TriageClass.DECIMAL_SLIP,
        confidence=0.95,
        rationale="9.5 kg/m2 matches the stated conversion ratio",
    )
    assert d.action is Action.APPLIED


def test_low_confidence_goes_to_a_person(record):
    d = changes.finalise(
        request_for(record, "density", 9.5),
        old_value=95.0,
        triage=TriageClass.UNCLEAR,
        confidence=0.40,
        rationale="cannot tell from the record alone",
    )
    assert d.action is Action.PENDING_REVIEW
    assert "below the" in d.reason


def test_implausible_is_rejected(record):
    d = changes.finalise(
        request_for(record, "density", 9.5),
        old_value=95.0,
        triage=TriageClass.IMPLAUSIBLE,
        confidence=0.9,
        rationale="proposed value is nowhere near any figure in the document",
    )
    assert d.action is Action.REJECTED


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

def test_expired_record_is_detected(records_by_id):
    """Product 5 (FKD-S Product Range) expired on 2025-04-29."""
    assert changes.is_expired(records_by_id[5], on=dt.date(2026, 9, 4)) is True


def test_valid_record_is_not_expired(records_by_id):
    """Product 6 expires 2026-12-14."""
    assert changes.is_expired(records_by_id[6], on=dt.date(2026, 9, 4)) is False


def test_every_seed_record_has_a_usable_expiry_date(all_records):
    """Every seed record has a parseable expiry date."""
    undated = [r["product_id"] for r in all_records if not r.get("date")]
    assert undated == []
    unparseable = []
    for r in all_records:
        try:
            dt.date.fromisoformat(str(r["date"]))
        except ValueError:
            unparseable.append(r["product_id"])
    assert unparseable == []


def test_implausible_on_a_material_field_still_goes_to_a_person(record):
    """Regression: "implausible" on a published figure goes to a person."""
    d = changes.finalise(
        request_for(record, "impacts.gwp_total", 11.22),
        old_value=11.0,
        triage=TriageClass.IMPLAUSIBLE,
        confidence=0.95,
        rationale="does not match the sum of the parts",
    )
    assert d.action is Action.PENDING_REVIEW
    assert d.triage is TriageClass.IMPLAUSIBLE, "the model's view is kept as advice"


def test_density_cross_check_rejects_a_bad_density(records_by_id):
    """Product 86: 2287.5 kg/m3 x 0.08 m = 183 kg/m2, so a tenth is rejected by rule."""
    r = records_by_id[86]
    d = changes.screen(r, request_for(r, "density", r["density"] / 10))
    assert d is not None, "must be settled by the rules, not sent to the model"
    assert d.action is Action.REJECTED
    assert "183" in d.blocking_issues[0]


def test_density_cross_check_is_silent_without_all_three_values(all_records):
    """A missing thickness raises no error."""
    from domain import validation

    flagged = [
        r["product_id"] for r in all_records
        if any(i.field == "density" for i in validation.validate(r))
    ]
    assert flagged == []


def test_the_confidence_floor_is_where_the_measurement_put_it(record):
    """Pinned at 0.90; 0.85 auto-applied five wrong changes in the 2000-proposal run."""
    assert changes.CONFIDENCE_FLOOR == 0.90

    just_under = changes.finalise(
        request_for(record, "lifespan", 50.0),
        old_value=500.0,
        triage=TriageClass.DECIMAL_SLIP,
        confidence=0.89,
        rationale="ten times out",
    )
    assert just_under.action is Action.PENDING_REVIEW

    at_the_floor = changes.finalise(
        request_for(record, "lifespan", 50.0),
        old_value=500.0,
        triage=TriageClass.DECIMAL_SLIP,
        confidence=0.90,
        rationale="ten times out",
    )
    assert at_the_floor.action is Action.APPLIED
