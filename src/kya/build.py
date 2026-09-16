"""Assembly: raw index entries (+ optional page enrichment) -> Settlements.

This is the hinge between the parsers and everything downstream. It does no
parsing and no policy of its own - parsing lives in ``sources/``, policy in
``classify``/``normalize``/``score``. What it does guarantee:

* every field on :class:`~kya.models.Settlement` is filled from exactly one
  authoritative place, and where two sources could disagree (proof, deadline)
  the page wins and a warning is recorded rather than the conflict hidden;
* scoring runs **last**, after all enrichment, so tiers and EV always
  reflect the final data, never an intermediate state.
"""

from __future__ import annotations

import hashlib
from datetime import date as date_cls

from kya.classify import classify_kind, classify_lane
from kya.deadlines import extract_first_date, parse_deadline
from kya.models import Settlement
from kya.normalize import classify_proof, normalize_payout, parse_fund_size
from kya.score import apply_scoring
from kya.sources.openclassactions_index import IndexDocument, IndexEntry
from kya.sources.openclassactions_page import PageData
from kya.textutil import collapse_ws


def _content_hash(*parts: object) -> str:
    """A short stable fingerprint of the fields a diff watches."""
    joined = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


# Detail labels carrying a second, distinct date worth publishing. Anything
# else in the details block is metadata, not a date the claimant acts on.
_EXTRA_DATE_LABELS = (
    "final approval hearing",
    "opt-out deadline",
    "objection deadline",
    "exclusion deadline",
    "date filed",
    "recall date",
)


def _extra_dates(page: PageData) -> dict[str, str]:
    """Secondary dates worth publishing, from quick facts and details alike."""
    dates: dict[str, str] = {}
    for label, fact in page.facts.items():
        if label.lower() in _EXTRA_DATE_LABELS and extract_first_date(fact.value):
            dates[label] = fact.value
    for label, value in page.details.values.items():
        if label.lower() in _EXTRA_DATE_LABELS and label not in dates and extract_first_date(value):
            dates[label] = value
    return dates


def _detail_value(details, *labels: str) -> str | None:
    """Case-insensitive lookup in the page's case-details block."""
    lowered = {key.lower(): value for key, value in details.values.items()}
    for label in labels:
        value = lowered.get(label.lower())
        if value:
            return value
    return None


def _detail_link(details, *labels: str) -> str | None:
    lowered = {key.lower(): value for key, value in details.links.items()}
    for label in labels:
        value = lowered.get(label.lower())
        if value:
            return value
    return None


def _fund_size(entry: IndexEntry, tiers) -> float | None:
    """A fund figure from the title, but only where a fund is the remedy.

    ``$495K Country Bank`` in a title is a *case* figure, not necessarily a
    common fund, so it is only trusted for pro-rata cases - the ones whose
    honest per-claim EV needs a fund to be estimated at all.
    """
    if not any(str(t.payout_type) == "pro_rata_fund" for t in tiers):
        return None
    return parse_fund_size(entry.title)


def build_settlement(
    entry: IndexEntry,
    *,
    index_updated: str | None = None,
    today: date_cls | None = None,
) -> Settlement:
    """One index line -> a fully scored :class:`Settlement`."""
    lane, lane_reason = classify_lane(entry)
    assessment = classify_proof(
        index_level=entry.proof_level,
        index_label=entry.proof_label_raw,
    )

    # Investigations publish no payout phrase; tiers stay empty and the EV
    # note explains why there is no number.
    phrase = (entry.payout_phrase or "").strip()
    tiers = (
        normalize_payout(
            phrase,
            proof_level=entry.proof_level,
            requires_claim_form=assessment.required is not False,
        )
        if phrase
        else []
    )

    settlement = Settlement(
        id=entry.slug,
        source_url=entry.url,
        title=collapse_ws(entry.title),
        kind=classify_kind(entry),
        lane=lane,
        lane_reason=lane_reason,
        tiers=tiers,
        payout_raw=phrase or None,
        fund_size=_fund_size(entry, tiers),
        proof_required=assessment.required,
        proof_level=assessment.level,
        proof_label_raw=entry.proof_label_raw,
        proof_detail=assessment.detail,
        deadline=parse_deadline(entry.deadline_raw),
        geo=entry.geo.model_copy(deep=True),
        index_updated=index_updated,
        # ``verified_as_of`` is reserved for a real page/portal read (set in
        # merge_page); index-only data carries ``index_updated`` instead, so
        # the site never overstates how well a case has been checked.
        warnings=list(assessment.warnings),
    )
    settlement.content_hash = _content_hash(
        settlement.title,
        settlement.payout_raw,
        settlement.deadline.raw,
        settlement.proof_level,
        settlement.proof_required,
    )
    return apply_scoring(settlement)


def merge_page(
    settlement: Settlement, page: PageData, *, today: date_cls | None = None
) -> Settlement:
    """Fold one parsed case page into its settlement. The page is authoritative.

    Only facts the page actually states are applied; every conflict with the
    index becomes a warning, never a silent overwrite.
    """
    if page.category and not settlement.category:
        settlement.category = page.category
    if page.date_modified:
        settlement.last_modified = page.date_modified
    settlement.verified_as_of = (today or date_cls.today()).isoformat()

    # A page may know the payout when the index line does not - but the
    # derivation is recorded, never presented as if the index carried it.
    if not settlement.tiers and str(settlement.kind) == "settlement":
        payout_fact = page.fact("Estimated Payout")
        if payout_fact is not None and payout_fact.is_known():
            tiers = normalize_payout(
                payout_fact.value,
                proof_level=settlement.proof_level,
                requires_claim_form=settlement.proof_required is not False,
            )
            if tiers:
                settlement.tiers = tiers
                settlement.payout_raw = payout_fact.value
                settlement.warnings.append(
                    "tiers derived from the page fact (index had no payout text)"
                )

    # --- proof: the page's own fact is authoritative -----------------------
    # "Claim Required" is read alongside "Proof Required": the boolean cannot
    # express the automatic lane, and Country Bank's page answers the filing
    # question only in the claim fact ("No - Automatic").
    proof_fact = page.fact("Proof Required")
    claim_fact_obj = page.fact("Claim Required")
    claim_text = (
        claim_fact_obj.value
        if claim_fact_obj is not None and claim_fact_obj.is_known()
        else None
    )
    if proof_fact is not None or claim_text:
        assessment = classify_proof(
            fact_value=(proof_fact.value or None) if proof_fact is not None else None,
            fact_detail=(proof_fact.sub or None) if proof_fact is not None else None,
            claim_fact=claim_text,
            index_level=settlement.proof_level,
            index_label=settlement.proof_label_raw,
        )
        if assessment.level is not None:
            settlement.proof_level = assessment.level
            settlement.proof_required = assessment.required
            settlement.proof_detail = assessment.detail or settlement.proof_detail
        settlement.warnings.extend(assessment.warnings)

    # --- deadline: fill gaps, flag conflicts -------------------------------
    page_deadline_text = page.fact_value("Claim Deadline") or page.fact_value("Deadline")
    if page_deadline_text:
        page_deadline = parse_deadline(page_deadline_text)
        if page_deadline.date is None:
            pass  # the page states no usable date; keep the index's
        elif settlement.deadline.date is None:
            settlement.deadline = page_deadline
        elif page_deadline.date != settlement.deadline.date:
            settlement.warnings.append(
                f"page deadline {page_deadline.date} differs from index "
                f"{settlement.deadline.date}; page wins"
            )
            settlement.deadline = page_deadline

    # --- case metadata ------------------------------------------------------
    settlement.case_title = _detail_value(page.details, "Case Title") or settlement.case_title
    settlement.case_number = _detail_value(page.details, "Case Number")
    settlement.court = _detail_value(page.details, "Court")
    settlement.administrator = _detail_value(page.details, "Administrator")
    settlement.official_website = _detail_link(
        page.details, "Official Website", "Settlement Website"
    )
    settlement.claim_url = page.claim_url or settlement.claim_url
    settlement.status_text = page.fact_value("Settlement Status") or settlement.status_text

    settlement.warnings.extend(page.warnings)
    settlement.extra_dates = _extra_dates(page)
    return apply_scoring(settlement)


def build_all(
    document: IndexDocument,
    pages: dict[str, PageData] | None = None,
    *,
    today: date_cls | None = None,
) -> list[Settlement]:
    """The whole index -> scored settlements, with page enrichment where given."""
    settlements = [
        build_settlement(entry, index_updated=document.last_updated, today=today)
        for entry in document.entries
    ]
    if pages:
        for settlement in settlements:
            page = pages.get(settlement.id)
            if page is not None:
                merge_page(settlement, page, today=today)
    return settlements
