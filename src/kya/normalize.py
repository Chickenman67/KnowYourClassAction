"""Turn raw upstream strings into typed benefit tiers and proof levels.

This is where the real work of the project lives, because upstream payout text
is genuinely messy. Real examples that must all survive:

* ``Up to $400 per Repair`` -> per-unit, capped at $400
* ``$45 Cash or Up to $2,000 + 1 Year of Credit Monitoring`` -> two tiers
* ``$50 Per Text, Up to $150`` -> per-unit $50, cap $150
* ``Estimated $1,718, Capped at $5,000`` -> an amount with a hard cap
* ``$400 - $300,000 by Injury Level`` -> a range
* ``Pro Rata Cash from a $3M Fund`` -> pro rata, fund size known
* ``80% Repair Reimbursement + Extended Warranty`` -> a percentage
* ``$2.00 per unit ... up to $6.00 without proof ... no cap with proof``
  -> **two tiers at different proof levels**, which is the single most
  important shape to get right: reporting only "proof required" here would
  hide a clean $6 claim.

Two rules keep this honest:

1. **Never invent a number.** If the text does not state an amount, the tier is
   ``undisclosed`` with low confidence - not a guess.
2. **Always keep the source text.** Every tier carries ``source_text`` so a
   reader (or a test) can see exactly what the figure was derived from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import BenefitTier, Confidence, PayoutType, ProofLevel
from .textutil import collapse_ws

__all__ = [
    "parse_money",
    "parse_money_amounts",
    "parse_fund_size",
    "parse_percent",
    "normalize_payout",
    "classify_proof",
]

_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
MAGNITUDES: dict[str, int] = {
    "k": 1_000,
    "thousand": 1_000,
    "m": 1_000_000,
    "mm": 1_000_000,
    "million": 1_000_000,
    "b": 1_000_000_000,
    "bn": 1_000_000_000,
    "billion": 1_000_000_000,
}

# A dollar amount. A currency symbol is required, which is what stops the
# parser reading "1.77M Ladders" or "2 Years" as money.
_AMOUNT_RE = re.compile(
    r"(?P<prefix>C\$|US\$|CA\$|\$)\s*"
    r"(?P<number>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<magnitude>thousand|million|billion|bn|mm|[KMB])?",
)

_PERCENT_RE = re.compile(r"(?P<percent>\d{1,3}(?:\.\d+)?)\s*%")

_RANGE_MARKER_RE = re.compile(rf"[{_DASHES}-]|\bto\b", re.I)

# "approximately" markers, which lower confidence but do not change the shape
_APPROX_RE = re.compile(r"\b(about|approximately|around|est\.?|estimated|~|roughly)\b", re.I)

_PER_UNIT_RE = re.compile(
    r"\bper\s+(?P<unit>unit|repair|text|message|product|bottle|item|device|"
    r"household|person|claim|detention|machine|mattress|vehicle|share|home|"
    r"account|loan|purchase|plan|day|hour)\b",
    re.I,
)

_CAP_LEADING_RE = re.compile(
    r"(?:capped\s+(?:at\s+)?|cap\s+of\s+|max(?:imum)?\s+(?:of\s+)?|up\s+to\s+)"
    r"\$\s*(?P<number>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<magnitude>thousand|million|billion|bn|[KMB])?",
    re.I,
)

# Upstream also writes the cap *after* the figure: "($12 Max)", "$6 cap".
_CAP_TRAILING_RE = re.compile(
    r"\$\s*(?P<number>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<magnitude>thousand|million|billion|bn|[KMB])?\s*"
    r"(?:max(?:imum)?|cap)\b",
    re.I,
)

def _magnitude_value(text: str | None) -> int:
    if not text:
        return 1
    return MAGNITUDES.get(text.strip().lower(), 1)


def parse_money_amounts(text: str | None) -> list[float]:
    """Every dollar amount in the text, in order of appearance.

    A currency symbol is mandatory, so ``"1.77M Ladders"`` and ``"2 Years of
    Credit Monitoring"`` contribute nothing.
    """
    text = collapse_ws(text)
    if not text:
        return []
    amounts: list[float] = []
    for match in _AMOUNT_RE.finditer(text):
        try:
            number = float(match.group("number").replace(",", ""))
        except ValueError:
            continue
        amounts.append(number * _magnitude_value(match.group("magnitude")))
    return amounts


def parse_money(text: str | None) -> float | None:
    """The first dollar amount in the text, or ``None``."""
    amounts = parse_money_amounts(text)
    return amounts[0] if amounts else None


def detect_currency(text: str | None) -> str:
    """``CAD`` for the Canadian settlements that quote ``C$32``, else ``USD``."""
    low = collapse_ws(text).lower()
    if "c$" in low or "cad" in low or "canadian" in low:
        return "CAD"
    return "USD"


def parse_percent(text: str | None) -> float | None:
    match = _PERCENT_RE.search(collapse_ws(text))
    if not match:
        return None
    try:
        return float(match.group("percent"))
    except ValueError:
        return None


_FUND_NEGATIVE_RE = re.compile(
    r"\b(no fixed common fund|undisclosed|not disclosed|none|claims-made|"
    r"no settlement|varies|not yet verified)\b",
    re.I,
)


def parse_fund_size(text: str | None) -> float | None:
    """A settlement fund figure, or ``None`` when the page does not state one.

    ``"No fixed common fund disclosed"``, ``"None - no settlement"`` and
    ``"Undisclosed"`` are explicitly *not* figures. Treating any of them as a
    number would put a fabricated multi-million-dollar figure on a card, so
    they return ``None``.
    """
    text = collapse_ws(text)
    if not text:
        return None

    has_dollar = "$" in text
    if _FUND_NEGATIVE_RE.search(text) and not has_dollar:
        return None

    # Prefer an amount that states its magnitude ("$4.5M", "$100 million").
    for match in _AMOUNT_RE.finditer(text):
        if not match.group("magnitude"):
            continue
        try:
            number = float(match.group("number").replace(",", ""))
        except ValueError:
            continue
        return number * _magnitude_value(match.group("magnitude"))

    amounts = parse_money_amounts(text)
    if not amounts:
        return None
    largest = max(amounts)
    # A $5 co-pay is not a settlement fund.
    return largest if largest >= 10_000 else None


def looks_automatic(text: str | None) -> bool:
    low = collapse_ws(text).lower()
    return "automatic" in low or "no claim form" in low


_TIER_SPLIT_RE = re.compile(r"\s+or\s+", re.I)

# A payout phrase can also hold two tiers separated by a proof marker rather
# than by "or": "... capped $6.00 without proof, uncapped with proof".
_PROOF_TIER_SPLIT_RE = re.compile(
    r",\s*(?=(?:uncapped|with proof|with documentation|documented|no cap)\b)",
    re.I,
)

_NO_PROOF_RE = re.compile(
    r"\b(no proof|without proof|no documents?|no documentation|no receipts?|"
    r"nothing to document|self-report|self report|attestation|on your word)\b",
    re.I,
)
_WITH_PROOF_RE = re.compile(
    r"\b(documented|documentation|with proof|with receipt|with documents?|"
    r"receipts? required|records required|batch code)\b",
    re.I,
)
_ID_GATE_RE = re.compile(
    r"\b(notice id|class member id|claim id|loginid|pin|id/code|id from notice)\b",
    re.I,
)
_UNDISCLOSED_RE = re.compile(
    r"\b(varies|undisclosed|not disclosed|unknown|unstated)\b", re.I
)


_DEADLINE_CLAUSE_RE = re.compile(r"[,;]?\s*\bclaim\s+by\b[^,;]*$", re.I)


def split_tiers(text: str | None) -> list[str]:
    """Split a payout phrase into its alternative benefit tiers.

    ``"`` separates alternatives. ``"$95 or $45 Cash, or Up to $2,000
    Documented"`` therefore yields three tiers, while a plain comma list is left
    alone because it usually joins components of a single tier.
    """
    text = collapse_ws(text)
    if not text:
        return []
    # A trailing "Claim by <date>" clause is stripped first: the deadline is
    # parsed into its own field, and several index lines append it to the
    # payout phrase ("Up to $160, Claim by September 14"). Left in place it
    # would pollute every tier label with text that belongs elsewhere.
    text = _DEADLINE_CLAUSE_RE.sub("", text).strip(" .,;")
    if not text:
        return []
    parts: list[str] = []
    for chunk in _TIER_SPLIT_RE.split(text):
        parts.extend(_PROOF_TIER_SPLIT_RE.split(chunk))
    cleaned = [part.strip(" .,;") for part in parts]
    return [part for part in cleaned if part]


def _amount_from_match(match: re.Match[str]) -> float | None:
    try:
        number = float(match.group("number").replace(",", ""))
    except (ValueError, IndexError):
        return None
    return number * _magnitude_value(match.groupdict().get("magnitude"))


def extract_cap(text: str | None) -> float | None:
    """A stated cap, from either order: ``"up to $150"`` or ``"($12 Max)"``."""
    text = collapse_ws(text)
    if not text:
        return None
    for pattern in (_CAP_LEADING_RE, _CAP_TRAILING_RE):
        match = pattern.search(text)
        if match:
            value = _amount_from_match(match)
            if value is not None:
                return value
    return None


def segment_explicit_level(segment: str) -> str | None:
    """The proof level stated in a tier's own wording, or ``None`` if silent.

    ``None`` means the tier simply inherits the settlement's default - which is
    a different thing from an explicit statement, and the distinction
    :func:`_apply_alternative_proof_levels` depends on.
    """
    if _ID_GATE_RE.search(segment):
        return ProofLevel.L2.value
    if _NO_PROOF_RE.search(segment):
        return ProofLevel.L1.value
    if _WITH_PROOF_RE.search(segment):
        return ProofLevel.L3.value
    if looks_automatic(segment):
        return ProofLevel.L0.value
    return None


def segment_proof_level(segment: str, default: str) -> str:
    """The proof level implied by one tier's own wording.

    Checked in order of specificity, because ``"$5 Without Proof or $10 With
    Documentation"`` puts the two levels in the same phrase.
    """
    explicit = segment_explicit_level(segment)
    return explicit if explicit is not None else default


def _parse_segment(
    segment: str,
    *,
    currency: str,
    default_proof: str,
    requires_claim_form: bool,
) -> BenefitTier | None:
    """Turn one tier phrase into a :class:`BenefitTier`, or ``None`` if empty."""
    text = collapse_ws(segment).strip(" .,;")
    if not text:
        return None

    low = text.lower()
    proof = segment_proof_level(text, default_proof)
    confidence = Confidence.LOW if _APPROX_RE.search(text) else Confidence.MEDIUM

    def tier(**kwargs: object) -> BenefitTier:
        base: dict[str, object] = {
            "label": text,
            "currency": currency,
            "proof_level": proof,
            "requires_claim_form": requires_claim_form,
            "source_text": text,
            "confidence": confidence,
        }
        base.update(kwargs)
        return BenefitTier(**base)  # type: ignore[arg-type]

    percent = parse_percent(text)
    amounts = parse_money_amounts(text)

    # 1. A percentage of costs recovered.
    if percent is not None and not amounts:
        return tier(payout_type=PayoutType.PERCENT_OF_COST, amount_max=percent)

    # 2. Pro rata: the fund size is emphatically NOT this claimant's payout.
    #    Storing it as one would rank a $3M fund above a $2,000 fixed payment.
    if "pro rata" in low or "pro-rata" in low:
        return tier(payout_type=PayoutType.PRO_RATA_FUND)

    # 3. Explicitly nothing to state.
    if _UNDISCLOSED_RE.search(text):
        return tier(payout_type=PayoutType.UNDISCLOSED, confidence=Confidence.LOW)

    per_unit = _PER_UNIT_RE.search(text)
    cap = extract_cap(text)

    # 4. No figures at all: a non-cash remedy, a voucher, or genuinely unknown.
    if not amounts:
        if re.search(r"\b(voucher|service card|gift card|store credit|coupon)\b", low):
            return tier(payout_type=PayoutType.VOUCHER)
        if re.search(
            r"\b(free|replacement|repair kit|monitoring|forgiveness|refund|credit|"
            r"debt|reimbursement|warranty|coverage)\b",
            low,
        ):
            return tier(payout_type=PayoutType.NON_CASH)
        # e.g. "Automatic Payments" - the payment is automatic but no figure
        # is published here. Saying "undisclosed" is honest; inventing a number
        # would not be.
        return tier(payout_type=PayoutType.UNDISCLOSED, confidence=Confidence.LOW)

    # 5. A genuine range, e.g. "$400 - $300,000 by Injury Level".
    if len(amounts) >= 2 and _RANGE_MARKER_RE.search(text):
        low_amount, high_amount = min(amounts[0], amounts[1]), max(amounts[0], amounts[1])
        return tier(
            payout_type=PayoutType.RANGE,
            amount_min=low_amount,
            amount_max=high_amount,
        )

    # 5b. A voucher with a face value is still not cash: "$25 Voucher".
    # The number is kept for the sticker tier, but the type says store credit.
    if re.search(r"\b(voucher|service card|gift card|store credit|coupon)\b", low):
        return tier(payout_type=PayoutType.VOUCHER, amount_max=max(amounts))

    # 6. Per unit, possibly capped: "$50 Per Text, Up to $150".
    if per_unit is not None:
        unit = (per_unit.group("unit") or "").lower()
        rate = amounts[0]
        if unit == "share":
            return tier(payout_type=PayoutType.ESTIMATED_SHARE, per_unit=rate)
        return tier(
            payout_type=PayoutType.PER_UNIT,
            per_unit=rate,
            cap=cap if cap is not None else (max(amounts) if len(amounts) > 1 else None),
        )

    # 7. Otherwise the ceiling is the largest figure the phrase states.
    ceiling = max(amounts)
    return tier(
        payout_type=PayoutType.RANGE if "up to" in low or len(amounts) > 1 else PayoutType.FIXED,
        amount_max=ceiling,
        cap=cap if cap is not None and cap != ceiling else None,
    )


def normalize_payout(
    text: str | None,
    *,
    proof_level: str | None = None,
    requires_claim_form: bool = True,
    currency: str | None = None,
) -> list[BenefitTier]:
    """Normalize a payout phrase into one or more benefit tiers.

    Returns an empty list when there is no payout text at all, so a caller can
    tell "no payout published" apart from "payout worth $0".
    """
    segments = split_tiers(text)
    if not segments:
        return []

    resolved_currency = currency or detect_currency(text)
    default_proof = proof_level or ProofLevel.L3.value

    tiers: list[BenefitTier] = []
    explicits: list[str | None] = []
    seen: set[tuple] = set()
    for segment in segments:
        explicit = segment_explicit_level(segment)
        tier = _parse_segment(
            segment,
            currency=resolved_currency,
            default_proof=default_proof,
            requires_claim_form=requires_claim_form,
        )
        if tier is None:
            continue
        key = (tier.payout_type, tier.amount_min, tier.amount_max, tier.per_unit, tier.cap)
        if key in seen:
            continue
        seen.add(key)
        tiers.append(tier)
        explicits.append(explicit)

    _apply_alternative_proof_levels(tiers, explicits)
    _inherit_uncapped_rate(tiers)
    return tiers


def _apply_alternative_proof_levels(
    tiers: list[BenefitTier], explicits: list[str | None]
) -> None:
    """Mark the silent tier in a documented/no-documentation pair as the easy path.

    In ``"Up to $5,000 Documented or ~$100 Cash"`` only the first tier mentions
    proof. The second is the alternative *because* it needs none, so it becomes
    L1 rather than inheriting the documented default. Without this, the tracker
    would report "documentation required" and hide a clean claim - the exact
    failure the dual-tier model exists to prevent.

    The rule only fires when one tier explicitly requires documents, so phrases
    that simply list components (``"$45 Cash or Up to $2,000"``) are untouched.
    """
    if len(tiers) < 2:
        return
    if not any(explicit == ProofLevel.L3.value for explicit in explicits):
        return
    for tier, explicit in zip(tiers, explicits):
        if explicit is not None:
            continue
        if str(tier.proof_level) != ProofLevel.L3.value:
            continue
        tier.proof_level = ProofLevel.L1.value
        tier.label = tier.label if "no proof" in tier.label.lower() else (
            f"{tier.label} (no documentation)"
        )


def _inherit_uncapped_rate(tiers: list[BenefitTier]) -> None:
    """Give an "uncapped with proof" tier the rate its sibling already states.

    The Earth Rated shape - *"$2.00 per unit, capped $6.00 without proof,
    uncapped with proof"* - states the rate once. The second tier restates only
    the absence of a cap, so it borrows the rate while keeping ``cap = None``.
    Documented in the README because it is an inference, not a quotation.
    """
    rates = [t.per_unit for t in tiers if t.per_unit is not None]
    if not rates:
        return
    shared_rate = min(rates)
    for tier in tiers:
        if tier.per_unit is not None:
            continue
        if str(tier.payout_type) != PayoutType.UNDISCLOSED.value:
            continue
        if re.search(r"\b(uncapped|no cap)\b", tier.source_text, re.I):
            tier.per_unit = shared_rate
            tier.payout_type = PayoutType.PER_UNIT.value
            tier.confidence = Confidence.MEDIUM.value


@dataclass
class ProofAssessment:
    """What is actually required to claim, and how confident we are."""

    level: str | None = None
    required: bool | None = None
    detail: str | None = None
    warnings: list[str] = field(default_factory=list)


_ID_DETAIL_RE = re.compile(
    r"\b(notice id|claim id|claim number|claimant id|class member id|settlement claim id|"
    r"member id|unique id|identifiers?|login id|loginid|log in id|log-in id|apex id|cpt id|"
    r"confirmation code|enrollment code|passcode|pin|id/code)\b",
    re.I,
)
_DOC_DETAIL_RE = re.compile(
    r"\b(receipts?|documentation|documents?|records?|statements?|bills?|invoices?|"
    r"proof|proof of (?:purchase|payment|ownership)|evidence of purchase|itemized|"
    r"bill of sale|batch code|medical records)\b",
    re.I,
)
# "...eligibility is determined from *the bank's own records*" names the
# administrator's records, not a claimant document. Read literally the bare word
# "records" looks like a documents bar, which would demand paperwork from someone
# who has to file nothing at all - and route a no-action case into the claimable
# lane. Stripped before the document test, so only the claimant's own paperwork
# counts.
_ADMIN_RECORDS_RE = re.compile(
    r"\b(?:own|administrator'?s?|company'?s?|defendant'?s?|bank'?s?|"
    r"business'?s?|employer'?s?|their|its)\s+records?\b",
    re.I,
)
_UNVERIFIED_RE = re.compile(r"\b(not yet verified|not yet known|unknown|tbd)\b", re.I)
# The page's "Claim Required" fact states the filing mechanics directly
# ("No - Automatic") that its "Proof Required" boolean cannot express.
_CLAIM_AUTO_RE = re.compile(
    r"\bautomatic(?:ally)?\b|\bno\s+claim\s+(?:form|required|needed)\b|"
    r"\bnone\s+(?:required|needed)\b",
    re.I,
)
# Payment arrives without the claimant doing anything. The page's Proof Required
# boolean cannot express this - "No" covers both "no proof needed, but file a
# claim" and "no claim at all" - so the detail line is what tells them apart.
# That difference is the whole point of the automatic lane.
#
# Only *explicit* automatic wording qualifies. An earlier version also matched
# "the administrator computes your share from X's own records", which is a
# statement about proof, not about filing: Raging Waters says exactly that while
# still requiring a signed claim form. Promoting it to L0 would drop a
# must-file case into the automatic lane and tell the user to do nothing.
_AUTO_DETAIL_RE = re.compile(
    r"\bautomatic(?:ally)?\b|\bno\s+claim\s+form\b|"
    r"\bclaim\s+form\s+(?:is\s+)?not\s+required\b|"
    r"\bno\s+(?:action|filing|paperwork)\s+(?:is\s+)?(?:needed|required)\b",
    re.I,
)

# Documents named as an alternative to an ID ("Unique ID from the notice, or
# proof of ownership showing your name and VIN") do not raise the bar: the ID
# alone still gets you paid, so the easier path is the one to report.
_DOC_ALTERNATIVE_RE = re.compile(r"\bor\b", re.I)
# Negation of a document requirement: "no receipts", "without receipts",
# "receipts are not required". Read literally, the bare word "receipts" looks
# like a documents bar, which is the opposite of what these lines say. Applied
# to the text *around each document mention* - commas do not split clauses, so
# "no receipts, but the online form is gated on a Claim ID" must still read
# the negation.
_DOC_NEGATED_BEFORE_RE = re.compile(
    r"\b(?:no|without|never|lacking|absent|nor)\b.{0,24}$", re.I
)
_DOC_NEGATED_AFTER_RE = re.compile(
    r"^\s*(?:are|is|were|was)?\s*(?:not|never)\s+"
    r"(?:required|needed|necessary|mandatory)\b",
    re.I,
)
# A documents clause scoped to a *higher or optional* tier. The base claim still
# files on the ID/PIN; the receipts only buy a bigger payout. Treating it as the
# entry bar would tell people to dig up documents they do not need.
_TIER_SCOPE_RE = re.compile(
    r"\b(?:option|tier|tiers|higher|additional|extra|documented|undocumented|loss(?:es)?|"
    r"out-of-pocket|reimbursement|amend|increase|to\s+claim\s+more|more\s+than\s+the|"
    r"over\s+the|cap|for\s+the\s+(?:cash|maximum|full|larger|bigger))\b",
    re.I,
)

# A clause that offers documents as an *extra* rather than a requirement.
# "purchase records only to amend the amounts" means the ID gets you paid;
# the records merely raise the figure. Reading that as "documents required"
# would tell people to dig up receipts they do not need, so these clauses are
# discarded before the level is decided.
_OPTIONAL_QUALIFIER_RE = re.compile(
    r"\b(?:optional|only to (?:amend|increase|raise|boost|claim more|challenge|"
    r"dispute|correct|update)|to amend|"
    r"if you (?:have|wish|want|can)|if available|for (?:additional|extra|higher)|"
    r"to increase|to claim more|not required|if)\b",
    re.I,
)
# Documents offered as an *alternative* to the notice ID leave the ID as the
# usable path ("Unique ID from the notice, or proof of ownership"). The "or"
# must sit between the ID and the document - a mere list ("Class Member ID,
# VIN and supporting ownership or repair documents") is a real documents bar,
# so an "or" buried inside the document phrase does not qualify.
_ID_OR_DOC_ALTERNATIVE_RE = re.compile(
    r"\b(?:id|pin|code)\b[^,;]{0,40}?,\s*or\s+(?:the\s+|a\s+|your\s+)?"
    r"(?:proof|receipts?|documentation|documents?|records?|statements?|bills?|invoices?|evidence)\b"
    r"|\b(?:id|pin|code)\b\s+or\s+(?:the\s+|a\s+|your\s+)?"
    r"(?:proof|receipts?|documentation|documents?|records?|statements?|bills?|invoices?|evidence)\b",
    re.I,
)
# Clause separators as they actually appear upstream: sentence punctuation, the
# middle dot the source uses between fact parts, plus dashes and "but".
_CLAUSE_SPLIT_RE = re.compile(r"[.;\u00b7\u2022\u2014\u2013]|\s-\s|\bbut\b")


def _doc_mention_negated(clause: str, match: re.Match[str]) -> bool:
    """Is *this* document mention negated or withdrawn?"""
    if _DOC_NEGATED_BEFORE_RE.search(clause[: match.start()]):
        return True
    return bool(_DOC_NEGATED_AFTER_RE.search(clause[match.end() :]))


def _detail_requirement_level(detail: str) -> str | None:
    """Which proof level the page's detail line actually *requires*.

    Detail lines frequently mention both gates at once, e.g. ``"Class Member ID
    from the notice to file online \u2014 purchase records only to amend the
    amounts"``. Filing needs the ID; the records only change the payout. Each
    clause is therefore judged on its own, and any clause qualified as optional
    or amend-only is dropped. Returns ``None`` when no clause states a bar.
    """
    best: str | None = None
    for clause in _CLAUSE_SPLIT_RE.split(detail):
        clause = collapse_ws(clause)
        if not clause:
            continue
        # "the bank's own records" names the administrator's records, not a
        # claimant document. Stripped before the doc test so only the
        # claimant's own paperwork counts.
        clause = _ADMIN_RECORDS_RE.sub("", clause)
        has_id = _ID_DETAIL_RE.search(clause)
        optional_docs = bool(_OPTIONAL_QUALIFIER_RE.search(clause))
        scoped_docs = bool(_TIER_SCOPE_RE.search(clause))
        doc_bar = False
        for mention in _DOC_DETAIL_RE.finditer(clause):
            if _doc_mention_negated(clause, mention):
                continue
            if optional_docs or (scoped_docs and not has_id):
                continue
            doc_bar = True
            break
        if doc_bar:
            # Documents offered as an alternative to an ID leave the ID as
            # the usable path; on their own they are the higher bar and win.
            if has_id and _ID_OR_DOC_ALTERNATIVE_RE.search(clause):
                best = best or ProofLevel.L2.value
            else:
                return ProofLevel.L3.value
        elif has_id and best is None:
            best = ProofLevel.L2.value
    return best


def classify_proof(
    *,
    fact_value: str | None = None,
    fact_detail: str | None = None,
    index_level: str | None = None,
    index_label: str | None = None,
    tiers: list[BenefitTier] | None = None,
    claim_fact: str | None = None,
) -> ProofAssessment:
    """Resolve the proof ladder for one settlement.

    The page's own ``Proof Required`` fact is authoritative; the index label is
    the fallback for pages that do not state one. Where the two disagree the
    page wins and a warning is recorded - a disagreement is real information,
    and silently overwriting either one would hide a data problem.

    ``claim_fact`` is the page's ``Claim Required`` fact (e.g. ``"No -
    Automatic"``). It speaks about *filing*, which the ``Proof Required``
    boolean cannot express, so it is the only reliable source for the
    automatic lane on an enriched page.
    """
    assessment = ProofAssessment()
    tiers = tiers or []

    levels = {
        str(getattr(t.proof_level, "value", t.proof_level)) for t in tiers
    }
    easy = levels & {"L0", "L1"}
    hard = levels & {"L2", "L3", "L4"}

    # 1. Two paths to the money is the single most useful thing to report.
    if easy and hard:
        assessment.level = ProofLevel.L4.value
        assessment.required = None if easy == {"L0"} else True
        assessment.detail = _detail_text(fact_detail)
        return assessment

    value = collapse_ws(fact_value)
    if value and not _UNVERIFIED_RE.search(value):
        lowered = value.lower()
        assessment.detail = collapse_ws(fact_detail) or None
        if lowered.startswith("no") or lowered in {"none", "false"}:
            # "No" means nothing is needed to *file*. It still cannot express
            # the automatic lane, where the payment arrives unprompted - the
            # detail line is the only thing that separates "no proof needed,
            # but file a claim" from "no claim at all". That difference is the
            # whole point of the automatic lane, so read it before settling
            # on L1.
            auto_claim = bool(claim_fact and _CLAIM_AUTO_RE.search(claim_fact))
            if auto_claim or (fact_detail and _AUTO_DETAIL_RE.search(fact_detail)):
                assessment.required = False
                assessment.level = ProofLevel.L0.value
            elif fact_detail and _detail_requirement_level(fact_detail) == ProofLevel.L3.value:
                # Base claim needs nothing, but documents open a higher tier:
                # two paths to the money, which is an L4.
                assessment.required = True
                assessment.level = ProofLevel.L4.value
            else:
                assessment.required = False
                if str(index_level) == ProofLevel.L0.value:
                    # The index's "automatic payment" is a claim about *filing*,
                    # not proof; a bare "No" boolean neither confirms nor denies
                    # it. Keep the stronger statement rather than inventing a
                    # disagreement the page never made.
                    assessment.level = ProofLevel.L0.value
                else:
                    assessment.level = ProofLevel.L1.value
        elif lowered.startswith("yes") or lowered in {"true", "required"}:
            assessment.required = True
            # Documents are the higher bar, so they win when both are stated -
            # unless the document mention is only an option to raise the amount.
            stated = _detail_requirement_level(fact_detail) if fact_detail else None
            # "Yes" with no usable detail: the index label is real data, so
            # prefer it over assuming documents - assuming the commoner bar
            # when the page names none would invent a requirement.
            assessment.level = stated or index_level or ProofLevel.L3.value

    # 1b. A page that never states Proof Required can still settle the
    # question outright through its Claim Required fact.
    if assessment.level is None and claim_fact and _CLAIM_AUTO_RE.search(claim_fact):
        assessment.required = False
        assessment.level = ProofLevel.L0.value
        assessment.detail = collapse_ws(claim_fact)

    # 2. Fall back to the index label.
    if assessment.level is None and index_level:
        assessment.level = str(index_level)
        assessment.detail = collapse_ws(index_label) or None
        # L0 (automatic) and L1 (no proof / self-report) need nothing from the
        # claimant's files; only an ID gate or documents are a real "required".
        assessment.required = str(index_level) not in {
            ProofLevel.L0.value,
            ProofLevel.L1.value,
        }

    # 3. Report a disagreement rather than hiding it.
    if (
        assessment.level
        and index_level
        and str(assessment.level) != str(index_level)
        and str(assessment.level) != ProofLevel.L4.value
    ):
        assessment.warnings.append(
            f"proof level differs from the index (index={index_level}, page={assessment.level})"
        )

    if assessment.level is None:
        assessment.warnings.append("no proof requirement published")

    return assessment


def _detail_text(fact_detail: str | None) -> str | None:
    """Prefer the page's own wording when describing a dual-tier case."""
    return collapse_ws(fact_detail) or None