"""Payout tier (S-F) and EV estimation tests.

Built from real payout phrases run through the normalizer, so a normalizer
regression shows up here too.
"""

import pytest

from kya.models import BenefitTier, Confidence, PayoutTier, PayoutType, Settlement
from kya.normalize import normalize_payout, parse_fund_size
from kya.score import apply_scoring, assign_payout_tier, estimate_ev


def _settlement(phrase: str, fund_size: float | None = None) -> Settlement:
    return Settlement(
        id="test",
        source_url="https://example.com",
        title="Test",
        tiers=normalize_payout(phrase),
        fund_size=fund_size,
    )


# --------------------------------------------------------------------------
# tier assignment
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("$50 Cash", PayoutTier.C),
        ("$150", PayoutTier.C),
        ("Up to $160", PayoutTier.C),
        ("About $150", PayoutTier.C),
        ("Up to $2,750", PayoutTier.A),
        ("Up to $5,000", PayoutTier.S),
        ("$10", PayoutTier.D),
        ("$5", PayoutTier.E),
        ("$50 Cash or Up to $4,000", PayoutTier.A),  # best tier, not first tier
    ],
)
def test_tier_from_best_ceiling(phrase: str, expected: PayoutTier) -> None:
    assert assign_payout_tier(_settlement(phrase)) is expected


def test_pro_rata_with_no_figure_is_tier_f() -> None:
    settlement = _settlement("Pro Rata Cash from a $3M Fund", fund_size=3_000_000)
    assert assign_payout_tier(settlement) is PayoutTier.F


def test_no_tiers_at_all_gets_no_tier() -> None:
    settlement = _settlement("")
    assert assign_payout_tier(settlement) is None


# --------------------------------------------------------------------------
# EV estimation
# --------------------------------------------------------------------------
def test_fixed_amount_is_face_value_high_confidence() -> None:
    ev, confidence, _ = estimate_ev(_settlement("$50 Cash"))
    assert ev == 50
    assert confidence is Confidence.HIGH


def test_range_uses_midpoint_not_the_maximum() -> None:
    ev, confidence, note = estimate_ev(_settlement("$80-$100 Cash"))
    assert ev == 90
    assert confidence is Confidence.MEDIUM
    assert "midpoint" in note


def test_open_ended_maximum_is_halved_at_low_confidence() -> None:
    ev, confidence, _ = estimate_ev(_settlement("Up to $4,000"))
    assert ev == 2_000
    assert confidence is Confidence.LOW


def test_per_unit_assumes_one_unit() -> None:
    ev, confidence, note = estimate_ev(_settlement("$2.00 per unit, capped $6.00 without proof"))
    assert ev == 2
    assert confidence is Confidence.MEDIUM
    assert "1 unit" in note


def test_pro_rata_guesses_from_fund_size_and_says_so() -> None:
    ev, confidence, note = estimate_ev(
        _settlement("Pro Rata Cash from a $3M Fund", fund_size=3_000_000)
    )
    assert ev == pytest.approx(3_000_000 * 0.0005)
    assert confidence is Confidence.LOW
    assert "0.05%" in note


def test_percent_of_cost_gets_no_made_up_number() -> None:
    ev, confidence, note = estimate_ev(_settlement("Up to 80% of repair cost"))
    assert ev is None
    assert "percentage" in note


def test_voucher_is_flagged_not_cash() -> None:
    ev, confidence, note = estimate_ev(_settlement("$25 Voucher"))
    assert ev is None
    assert "voucher" in note or "credit" in note


def test_investigation_with_no_tiers_yields_none() -> None:
    ev, confidence, note = estimate_ev(_settlement(""))
    assert ev is None
    assert confidence is Confidence.LOW


def test_dual_tier_reports_the_easy_path_not_the_headline() -> None:
    """Earth Rated: EV reflects the no-proof path; the note says what it assumed."""
    settlement = _settlement(
        "$2.00 per unit, capped $6.00 without proof, uncapped with proof"
    )
    ev, _, note = estimate_ev(settlement)
    assert ev == 2
    assert note


# --------------------------------------------------------------------------
# apply_scoring fills the Settlement's own fields
# --------------------------------------------------------------------------
def test_apply_scoring_populates_all_fields() -> None:
    settlement = _settlement("$50 Cash or Up to $4,000")
    apply_scoring(settlement)
    assert settlement.payout_tier == PayoutTier.A.value
    # Best achievable path: half of the $4,000 maximum, low confidence.
    assert settlement.ev_estimate == 2_000
    assert settlement.ev_confidence == Confidence.LOW.value


def test_apply_scoring_stores_a_plain_string_tier() -> None:
    """``payout_tier`` must be ``str``, not ``PayoutTier``, after scoring.

    The assignment happens after validation, so ``use_enum_values`` never sees
    it: without an explicit ``.value`` the field keeps its enum, and while a
    str-mixin enum JSON-dumps as ``"A"``, Jinja renders it through ``str()`` as
    ``"PayoutTier.A"`` - which is exactly how the live site shipped a tier
    dropdown where every option filtered every row out. The type is the bug,
    so assert the type, not just the value.
    """
    settlement = _settlement("$50 Cash or Up to $4,000")
    apply_scoring(settlement)
    assert type(settlement.payout_tier) is str

    empty = _settlement("")
    apply_scoring(empty)
    assert empty.payout_tier is None
