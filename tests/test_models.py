"""Invariants of the normalized model: the proof ladder and tier maths.

These matter more than they look. ``simplest_tier`` and ``is_dual_tier`` are
what stop the UI from telling someone "documentation required" when a smaller
no-receipt tier is sitting right there.
"""

from __future__ import annotations

import datetime

from kya.models import (
    BenefitTier,
    Deadline,
    DeadlineKind,
    GeoScope,
    ProofLevel,
    Settlement,
    proof_rank,
)


def make_settlement(tiers: list[BenefitTier]) -> Settlement:
    return Settlement(id="t", source_url="https://example.com", title="T", tiers=tiers)


# --------------------------------------------------------------------------
# proof ladder
# --------------------------------------------------------------------------
def test_proof_rank_orders_easiest_first() -> None:
    assert proof_rank("L0") < proof_rank("L1") < proof_rank("L2") < proof_rank("L3")
    assert proof_rank(ProofLevel.L0) == 0


def test_proof_rank_unknown_sorts_last() -> None:
    # An unrecognised level must never masquerade as "no proof required".
    assert proof_rank(None) == 5
    assert proof_rank("nonsense") == 5


# --------------------------------------------------------------------------
# tier maths
# --------------------------------------------------------------------------
def test_ceiling_prefers_cap_over_per_unit() -> None:
    # Earth Rated shape: $2.00/unit, capped at $6 without proof.
    tier = BenefitTier(
        label="per unit capped",
        payout_type="per_unit",
        per_unit=2.0,
        cap=6.0,
        proof_level=ProofLevel.L1,
    )
    assert tier.ceiling() == 6.0


def test_ceiling_prefers_max_over_min() -> None:
    tier = BenefitTier(label="range", payout_type="range", amount_min=45, amount_max=2000)
    assert tier.ceiling() == 2000


def test_ceiling_is_none_when_undisclosed() -> None:
    assert BenefitTier(label="varies", payout_type="undisclosed").ceiling() is None


def test_simplest_tier_picks_the_least_proof() -> None:
    s = make_settlement(
        [
            BenefitTier(label="documented", amount_max=5000, proof_level=ProofLevel.L3),
            BenefitTier(label="no proof cash", amount_max=100, proof_level=ProofLevel.L1),
        ]
    )
    assert s.simplest_tier() is not None
    assert s.simplest_tier().label == "no proof cash"
    assert s.best_tier().label == "documented"


def test_dual_tier_detected() -> None:
    s = make_settlement(
        [
            BenefitTier(label="no proof", amount_max=5, proof_level=ProofLevel.L1),
            BenefitTier(label="documented", amount_max=5000, proof_level=ProofLevel.L3),
        ]
    )
    assert s.is_dual_tier() is True


def test_single_proof_tier_is_not_dual() -> None:
    s = make_settlement(
        [BenefitTier(label="doc", amount_max=50, proof_level=ProofLevel.L3)]
    )
    assert s.is_dual_tier() is False


def test_automatic_only_is_not_dual() -> None:
    # L0 alone is not "dual": there is no second, harder path to choose.
    s = make_settlement([BenefitTier(label="auto", amount_max=25, proof_level=ProofLevel.L0)])
    assert s.is_dual_tier() is False


# --------------------------------------------------------------------------
# deadlines
# --------------------------------------------------------------------------
def test_deadline_accepts_an_iso_date() -> None:
    d = Deadline(kind="claim", date=datetime.date(2026, 11, 23))
    assert d.date == datetime.date(2026, 11, 23)


def test_actionable_deadlines_only() -> None:
    assert Deadline(kind=DeadlineKind.CLAIM).is_actionable() is True
    assert Deadline(kind=DeadlineKind.OPT_OUT).is_actionable() is True
    assert Deadline(kind=DeadlineKind.OBJECTION).is_actionable() is True
    assert Deadline(kind=DeadlineKind.OPTIONAL_ELECTION).is_actionable() is True
    # An automatic payment has no deadline to miss; a rolling window does not
    # expire; an unknown date cannot be acted on.
    assert Deadline(kind=DeadlineKind.NO_ACTION).is_actionable() is False
    assert Deadline(kind=DeadlineKind.ROLLING).is_actionable() is False
    assert Deadline(kind=DeadlineKind.NOT_YET_KNOWN).is_actionable() is False


# --------------------------------------------------------------------------
# geography
# --------------------------------------------------------------------------
def test_state_restriction_is_restricted() -> None:
    assert GeoScope(scope_type="states", states=["CA"]).is_restricted() is True
    assert GeoScope(scope_type="country", country="CA").is_restricted() is True


def test_nationwide_and_unspecified_are_not_restricted() -> None:
    assert GeoScope(scope_type="nationwide").is_restricted() is False
    assert GeoScope().is_restricted() is False