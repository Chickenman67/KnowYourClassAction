"""The normalized data model.

Everything the pipeline produces is described here. The two axes the project
exists to answer are first-class fields rather than afterthoughts:

* **how much can be won** -> ``BenefitTier`` amounts and ``Settlement.payout_tier``
* **what is needed to claim** -> ``BenefitTier.proof_level``
  (L0 automatic, L1 self-report, L2 notice ID/PIN, L3 documents, L4 dual-tier)

A settlement holds one *or more* benefit tiers, because the common real-world
shape is a small no-proof tier alongside a larger documented one. Collapsing
that into a single "proof required: yes" would hide genuinely free money.
"""

from __future__ import annotations

import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PayoutType(str, Enum):
    FIXED = "fixed"                      # "$50 cash"
    RANGE = "range"                      # "$50 - $500"
    PER_UNIT = "per_unit"                # "$2.00 per unit", "$50 per text"
    PERCENT_OF_COST = "percent_of_recovery"   # "80% of repair cost"
    PRO_RATA_FUND = "pro_rata_fund"      # "pro rata share of a $167.5M fund"
    ESTIMATED_SHARE = "estimated_share"  # "estimated $0.31 per share"
    VOUCHER = "voucher_or_credit"        # "$40 dealer service card"
    NON_CASH = "non_cash"                # "free replacement mower", credit monitoring
    UNDISCLOSED = "undisclosed"          # "varies", "undisclosed", "no fund disclosed"


class ProofLevel(str, Enum):
    """The proof ladder, ordered from easiest to hardest to claim."""

    L0 = "L0"  # automatic payment - no claim form, nothing to file
    L1 = "L1"  # self-report, no documentation of any kind
    L2 = "L2"  # Claim/Notice ID, LoginID or PIN from the mailed notice
    L3 = "L3"  # documents: receipts, statements, records
    L4 = "L4"  # dual tier: a small no-proof tier AND a larger documented tier


class Lane(str, Enum):
    CLAIMABLE = "claimable"
    AUTOMATIC = "automatic"
    INVESTIGATION = "investigation"
    PENDING = "pending"


class CaseKind(str, Enum):
    SETTLEMENT = "settlement"
    PROGRAM = "program"          # e.g. a bankruptcy-approved restitution program
    LAWSUIT = "lawsuit"
    INVESTIGATION = "investigation"
    RECALL = "recall"


class DeadlineKind(str, Enum):
    CLAIM = "claim"
    OPT_OUT = "opt_out"
    OBJECTION = "objection"
    OPTIONAL_ELECTION = "optional_election"
    ROLLING = "rolling"
    NO_ACTION = "no_action"          # automatic payment: there is no deadline to miss
    NOT_YET_KNOWN = "not_yet_known"
    CONDITIONAL = "conditional"      # e.g. "within 90 days of final approval"
    UNPARSED = "unparsed"


class PayoutTier(str, Enum):
    S = "S"  # $5,000+
    A = "A"  # $1,000 - $5,000
    B = "B"  # $250 - $1,000
    C = "C"  # $50 - $250
    D = "D"  # $10 - $50
    E = "E"  # under $10
    F = "F"  # varies / pro rata / undisclosed - ranked on fund size, not sticker price


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# Ordered proof ladder. Kept as plain strings because ``use_enum_values``
# stores the raw value on the model, so a value can be either an enum member
# or its string form depending on how it was constructed.
_PROOF_ORDER: dict[str, int] = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}


def proof_rank(value: object) -> int:
    """Rank a proof level for ordering. Unknown levels sort last, never first."""
    text = getattr(value, "value", value)
    return _PROOF_ORDER.get(str(text), len(_PROOF_ORDER))


class BenefitTier(BaseModel):
    """One way to claim one settlement."""

    model_config = ConfigDict(use_enum_values=True)

    label: str
    payout_type: PayoutType = PayoutType.UNDISCLOSED
    amount_min: float | None = None
    amount_max: float | None = None
    per_unit: float | None = None
    cap: float | None = None
    currency: str = "USD"
    proof_level: ProofLevel = ProofLevel.L3
    requires_claim_form: bool = True
    source_text: str = ""
    confidence: Confidence = Confidence.MEDIUM

    def ceiling(self) -> float | None:
        """Best realistic value for a single claim from this tier.

        A cap beats a nominal per-unit rate (``$2/unit capped $6`` is worth
        $6, not $2), and an explicit maximum beats a minimum.
        """
        for candidate in (self.cap, self.amount_max, self.per_unit, self.amount_min):
            if candidate is not None:
                return float(candidate)
        return None


class GeoScope(BaseModel):
    """Who is actually eligible, geographically.

    Roughly half the open settlements are limited to one or a handful of
    states. Showing a California-only settlement to anyone else is a false
    positive, so this is stored and filtered on rather than discarded.
    """

    model_config = ConfigDict(use_enum_values=True)

    scope_type: Literal["nationwide", "states", "country", "other", "unspecified"] = "unspecified"
    states: list[str] = Field(default_factory=list)
    country: str | None = None
    label: str = ""

    def is_restricted(self) -> bool:
        return self.scope_type in {"states", "country", "other"}


class Deadline(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    raw: str = ""
    kind: DeadlineKind = DeadlineKind.UNPARSED
    # NOTE: qualified as datetime.date on purpose - the field is named ``date``,
    # which would otherwise shadow the type during pydantic's annotation eval.
    date: datetime.date | None = None
    note: str | None = None

    def is_actionable(self) -> bool:
        """Does ignoring this date cost the claimant anything?"""
        return self.kind in {
            DeadlineKind.CLAIM,
            DeadlineKind.OPT_OUT,
            DeadlineKind.OBJECTION,
            DeadlineKind.OPTIONAL_ELECTION,
            DeadlineKind.CONDITIONAL,
        }


class Settlement(BaseModel):
    """One normalized case, ready to publish."""

    model_config = ConfigDict(use_enum_values=True)

    id: str
    source: str = "openclassactions"
    source_url: str
    title: str
    category: str | None = None
    kind: CaseKind = CaseKind.SETTLEMENT
    lane: Lane = Lane.CLAIMABLE
    lane_reason: str = ""

    # --- what you can get ------------------------------------------------
    tiers: list[BenefitTier] = Field(default_factory=list)
    payout_raw: str | None = None
    payout_sub: str | None = None
    payout_tier: PayoutTier | None = None
    fund_size: float | None = None
    fund_size_raw: str | None = None

    # --- what you need ---------------------------------------------------
    proof_required: bool | None = None
    proof_level: ProofLevel | None = None
    proof_label_raw: str | None = None
    proof_detail: str | None = None

    # --- when ------------------------------------------------------------
    deadline: Deadline = Field(default_factory=Deadline)
    extra_dates: dict[str, str] = Field(default_factory=dict)

    # --- who / where -----------------------------------------------------
    geo: GeoScope = Field(default_factory=GeoScope)

    # --- case metadata ---------------------------------------------------
    status_text: str | None = None
    case_title: str | None = None
    case_number: str | None = None
    court: str | None = None
    administrator: str | None = None
    official_website: str | None = None
    claim_url: str | None = None

    # --- ranking ---------------------------------------------------------
    ev_estimate: float | None = None
    ev_confidence: Confidence = Confidence.LOW
    ev_note: str | None = None

    # --- provenance ------------------------------------------------------
    index_updated: str | None = None
    last_modified: str | None = None
    verified_as_of: str | None = None
    content_hash: str = ""
    warnings: list[str] = Field(default_factory=list)
    cross_refs: dict[str, list[dict[str, str]]] = Field(default_factory=dict)

    def simplest_tier(self) -> BenefitTier | None:
        """The tier requiring the least proof - the easy-money path, if any."""
        if not self.tiers:
            return None
        return min(
            self.tiers,
            key=lambda t: (proof_rank(t.proof_level), -(t.ceiling() or 0)),
        )

    def best_tier(self) -> BenefitTier | None:
        """The highest-value tier, which may demand documentation."""
        if not self.tiers:
            return None
        return max(self.tiers, key=lambda t: (t.ceiling() or 0))

    def best_tier_ceiling(self) -> float | None:
        """The best single-claim ceiling across all tiers, or ``None``."""
        ceilings = [t.ceiling() for t in self.tiers if t.ceiling() is not None]
        return max(ceilings) if ceilings else None

    def is_dual_tier(self) -> bool:
        """True when a no-proof path and a documented path both exist.

        This is the shape that matters most, and the one a single
        "proof required: yes/no" flag would destroy.
        """
        levels = {str(getattr(t.proof_level, "value", t.proof_level)) for t in self.tiers}
        return bool(levels & {"L0", "L1"}) and bool(levels & {"L2", "L3", "L4"})