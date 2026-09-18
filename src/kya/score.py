"""Payout tiering (S-F) and expected-value estimation.

Two separate jobs that are easy to confuse:

* **Tier** - how big is the sticker price, from the best tier's ceiling.
  Purely descriptive, one of S..F, with F reserved for cases whose value
  cannot be sticker-priced (pro rata, varies, non-cash) and is instead
  ranked on fund size.
* **EV** - what one claim is *realistically* worth. Deliberately
  conservative, always labelled with a confidence, and never presented as
  a promise. The assumptions live in ``config.yaml`` so they are visible.
"""

from __future__ import annotations

from kya.models import BenefitTier, Confidence, PayoutTier, PayoutType, Settlement

# (lower_bound_inclusive, PayoutTier) - highest bound <= ceiling wins.
_TIER_BOUNDS: list[tuple[float, PayoutTier]] = [
    (5_000, PayoutTier.S),
    (1_000, PayoutTier.A),
    (250, PayoutTier.B),
    (50, PayoutTier.C),
    (10, PayoutTier.D),
    (0, PayoutTier.E),
]

def assign_payout_tier(settlement: Settlement) -> PayoutTier | None:
    """The sticker-price tier, from the single most valuable benefit tier.

    ``None`` only when the case has no benefit tiers at all (investigations).
    Tiers that exist but carry no number (pro rata, varies, non-cash) are
    tier ``F`` - ranked on fund size further down the line, never dropped.
    """
    ceiling = settlement.best_tier_ceiling()
    if ceiling is None:
        return PayoutTier.F if settlement.tiers else None
    for bound, tier in _TIER_BOUNDS:
        if ceiling >= bound:
            return tier
    return None


def _tier_ev(
    tier: BenefitTier,
    *,
    assumed_units: int,
) -> tuple[float, Confidence, str | None] | None:
    """One tier's honest value, or ``None`` when the tier has no basis for one."""
    ptype = str(tier.payout_type)
    if ptype == PayoutType.FIXED.value and tier.amount_max is not None:
        return tier.amount_max, Confidence.HIGH, None
    if ptype == PayoutType.ESTIMATED_SHARE.value and tier.amount_max is not None:
        return tier.amount_max, Confidence.MEDIUM, "issuer-estimated per share"
    if ptype == PayoutType.RANGE.value:
        lo, hi = tier.amount_min, tier.amount_max
        if hi is not None and lo is not None:
            return (lo + hi) / 2, Confidence.MEDIUM, "midpoint of published range"
        if hi is not None:
            return hi / 2, Confidence.LOW, "half of the published maximum"
        if lo is not None:
            return lo, Confidence.LOW, "published minimum"
    if ptype == PayoutType.PER_UNIT.value and tier.per_unit is not None:
        value = tier.per_unit * assumed_units
        note = f"assumes {assumed_units} unit(s) at ${tier.per_unit:g}/unit"
        if tier.cap is not None:
            value = min(value, tier.cap)
            note += f", capped at ${tier.cap:g}"
        return value, Confidence.MEDIUM, note
    return None


_CONFIDENCE_ORDER = {Confidence.HIGH.value: 0, Confidence.MEDIUM.value: 1, Confidence.LOW.value: 2}


def estimate_ev(
    settlement: Settlement,
    *,
    assumed_units: int = 1,
    pro_rata_share_pct: float = 0.0005,
) -> tuple[float | None, Confidence, str | None]:
    """One claim's realistic value as ``(estimate, confidence, note)``.

    Each numeric tier is valued on its own terms and the *best achievable*
    estimate wins - so ``$50 cash or up to $4,000 documented`` prices the
    documented path, at low confidence, never the $50 headline. Assumptions
    are always spelled out in the note; when no honest number exists
    (percentage of costs, vouchers, undisclosed) the result is ``None``
    rather than an invented figure.
    """
    tiers = settlement.tiers
    if not tiers:
        return None, Confidence.LOW, "no benefit tiers published"

    candidates: list[tuple[float, Confidence, str | None]] = []
    for tier in tiers:
        result = _tier_ev(tier, assumed_units=assumed_units)
        if result is not None:
            candidates.append(result)

    if candidates:
        # Highest value wins; ties go to the more confident estimate.
        best = min(
            candidates,
            key=lambda c: (-c[0], _CONFIDENCE_ORDER[getattr(c[1], "value", c[1])]),
        )
        return best

    # Nothing numeric anywhere: fall through to fund-based guesses.
    fund_based = [t for t in tiers if str(t.payout_type) == PayoutType.PRO_RATA_FUND.value]
    if fund_based and settlement.fund_size:
        value = settlement.fund_size * pro_rata_share_pct
        return (
            value,
            Confidence.LOW,
            f"estimated {pro_rata_share_pct * 100:g}% of a "
            f"${settlement.fund_size:,.0f} fund",
        )

    for tier in tiers:
        ptype = str(tier.payout_type)
        if ptype == PayoutType.PERCENT_OF_COST.value:
            return None, Confidence.LOW, "a percentage of your documented costs"
        if ptype == PayoutType.VOUCHER.value:
            return None, Confidence.LOW, "store credit or voucher, not cash"
        if ptype == PayoutType.NON_CASH.value:
            return None, Confidence.LOW, "a non-cash remedy"
        if ptype == PayoutType.UNDISCLOSED.value:
            return None, Confidence.LOW, "payout not disclosed"

    return None, Confidence.LOW, "no basis for an estimate"


def apply_scoring(settlement: Settlement, **ev_kwargs) -> Settlement:
    """Fill in ``payout_tier`` and the ``ev_*`` fields, in place of mutating magic.

    ``payout_tier`` is written directly, after validation, so it bypasses
    ``use_enum_values`` and would otherwise stay a ``PayoutTier`` enum. A
    str-mixin enum JSON-dumps as its value (``"S"``) but Jinja renders it via
    ``str()`` as ``"PayoutTier.S"`` - which is exactly how the site shipped a
    tier dropdown that filtered every row out. Hence the same ``.value``
    coercion the ``ev_confidence`` line below already uses.
    """
    settlement.payout_tier = getattr(assign_payout_tier(settlement), "value", None)
    ev, confidence, note = estimate_ev(settlement, **ev_kwargs)
    settlement.ev_estimate = ev
    settlement.ev_confidence = getattr(confidence, "value", confidence)
    settlement.ev_note = note
    return settlement
