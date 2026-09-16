"""Lane and kind classification against the real corpus.

The classifier is the plan's priority table made executable:

    3. investigation  (checked first - nothing is claimable there)
    4. pending
    2. automatic
    1. claimable      (the default)

Every rule below is a case that actually appears in the live index.
"""

from kya.classify import classify_kind, classify_lane
from kya.models import CaseKind, Lane
from kya.sources.openclassactions_index import IndexEntry


# --------------------------------------------------------------------------
# one representative real case per lane
# --------------------------------------------------------------------------
def test_ryobi_lawsuit_url_is_investigation_lane_despite_being_a_recall(by_slug) -> None:
    entry = by_slug("ryobi-40v-mower-fire")
    lane, reason = classify_lane(entry)
    assert lane is Lane.INVESTIGATION
    assert "/lawsuits/" in reason
    # kind and lane are independent: this is a recall that you cannot claim.
    assert classify_kind(entry) is CaseKind.RECALL


def test_free_case_review_invitations_are_investigation_lane(index_document) -> None:
    """Real corpus: 'free case review' lives in the notes, not the title."""
    candidates = [
        e
        for e in index_document.entries
        if "free case review" in e.title.lower()
        or any("free case review" in n.lower() for n in e.notes)
    ]
    assert candidates, "corpus is expected to contain free-case-review invitations"
    for e in candidates:
        lane, reason = classify_lane(e)
        assert lane is Lane.INVESTIGATION, e.slug


def test_equifax_pending_final_approval_is_pending_lane(by_slug) -> None:
    entry = by_slug("equifax-credit-score-error")
    lane, reason = classify_lane(entry)
    assert lane is Lane.PENDING
    assert reason  # cited from the unverified section or the approval wording
    # the title-marker path must work on its own too, for open-section entries
    synthetic = IndexEntry(
        url="https://openclassactions.com/settlements/example.php",
        title="Example Settlement Pending Final Approval",
        section="open",
    )
    lane2, reason2 = classify_lane(synthetic)
    assert lane2 is Lane.PENDING
    assert "pending" in reason2.lower()


def test_country_bank_automatic_payment_is_automatic_lane(by_slug) -> None:
    entry = by_slug("country-bank-savings-overdraft-nsf-fee")
    lane, reason = classify_lane(entry)
    assert lane is Lane.AUTOMATIC
    assert "automatic" in reason


def test_excel_fitness_open_window_is_claimable(by_slug) -> None:
    entry = by_slug("excel-fitness")
    lane, _ = classify_lane(entry)
    assert lane is Lane.CLAIMABLE


# --------------------------------------------------------------------------
# the traps
# --------------------------------------------------------------------------
def test_proof_gated_settlement_is_not_read_as_an_investigation(by_slug) -> None:
    """'ID/code from notice required' must never look like a case review."""
    entry = by_slug("quality-inn-seatac")
    lane, _ = classify_lane(entry)
    assert lane is Lane.CLAIMABLE
    assert classify_kind(entry) is CaseKind.SETTLEMENT


def test_recall_with_a_real_refund_stays_claimable_but_is_flagged(by_slug) -> None:
    """Clorox/Weber/Zicam recalls pay refunds - claimable, but never 'money' unlabeled."""
    entry = by_slug("clorox-pine-sol-recall")
    lane, _ = classify_lane(entry)
    assert lane is Lane.CLAIMABLE
    assert classify_kind(entry) is CaseKind.RECALL


def test_recall_remedy_only_note_is_investigation_lane(by_slug) -> None:
    entry = by_slug("louisville-ladder-attic-ladder-recall")
    lane, reason = classify_lane(entry)
    assert lane is Lane.INVESTIGATION


# --------------------------------------------------------------------------
# the whole corpus
# --------------------------------------------------------------------------
def test_every_entry_gets_a_lane_and_a_reason(index_document) -> None:
    for entry in index_document.entries:
        lane, reason = classify_lane(entry)
        assert isinstance(lane, Lane)
        assert reason, f"{entry.slug}: empty lane_reason"


def test_corpus_lane_spread(index_document) -> None:
    """Live distribution as of 2026-09-14; a large shift means a rule broke."""
    counts: dict[str, int] = {}
    for entry in index_document.entries:
        lane, _ = classify_lane(entry)
        counts[lane.value] = counts.get(lane.value, 0) + 1
    total = sum(counts.values())
    assert total == len(index_document.entries)
    assert counts.get("claimable", 0) >= 180
    assert counts.get("automatic", 0) >= 30
    assert counts.get("investigation", 0) >= 5
    assert counts.get("pending", 0) >= 1


# --------------------------------------------------------------------------
# kind detection from URL path and title (synthetic cases)
# --------------------------------------------------------------------------
def _entry(url: str, title: str, **kwargs) -> IndexEntry:
    return IndexEntry(url=url, title=title, section="open", **kwargs)


def test_lawsuit_url_without_recall_word_is_investigation_kind() -> None:
    entry = _entry(
        "https://openclassactions.com/lawsuits/antitrust/x-y-class-action-lawsuit.php",
        "X v. Y Class Action Lawsuit",
    )
    assert classify_kind(entry) is CaseKind.INVESTIGATION


def test_recall_word_in_any_title_wins_the_recall_kind() -> None:
    entry = _entry(
        "https://openclassactions.com/settlements/some-recall-settlement.php",
        "Some Product Recall Settlement",
    )
    assert classify_kind(entry) is CaseKind.RECALL


def test_plain_settlement_page_is_settlement_kind() -> None:
    entry = _entry(
        "https://openclassactions.com/settlements/excel-fitness-data-breach-settlement.php",
        "Excel Fitness Data Breach Settlement",
    )
    assert classify_kind(entry) is CaseKind.SETTLEMENT
