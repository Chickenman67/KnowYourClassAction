"""Tests for payout normalisation, run against real upstream strings.

Every string here was taken from a live capture, not invented. Payout text on
these pages really does look like this, and if the parser only handled tidy
cases it would silently report "undisclosed" for the majority of settlements.
"""

from __future__ import annotations

import pytest

from kya.models import PayoutType, ProofLevel
from kya.normalize import (
    classify_proof,
    detect_currency,
    normalize_payout,
    parse_fund_size,
    parse_money,
    parse_money_amounts,
    parse_percent,
    split_tiers,
)


# --------------------------------------------------------------------------
# money extraction
# --------------------------------------------------------------------------
def test_parse_money_amounts_basic() -> None:
    assert parse_money_amounts("Up to $400 per Repair") == [400.0]
    assert parse_money_amounts("$50 Per Text, Up to $150") == [50.0, 150.0]


def test_parse_money_amounts_requires_a_currency_symbol() -> None:
    # These are quantities, not money, and must contribute nothing.
    assert parse_money_amounts("Free Repair Kit for 1.77M Ladders") == []
    assert parse_money_amounts("2 Years of Credit Monitoring") == []
    assert parse_money_amounts("Up to 2 per Household") == []


def test_parse_money_handles_magnitudes() -> None:
    assert parse_money("$167.5M") == 167_500_000
    assert parse_money("$11M") == 11_000_000
    assert parse_money("$495K") == 495_000


def test_parse_money_handles_commas_and_cents() -> None:
    assert parse_money("$2,000") == 2000
    assert parse_money("$2.00 per unit") == 2.0


def test_detect_currency() -> None:
    assert detect_currency("C$32 for Former CIBC Fund Holders") == "CAD"
    assert detect_currency("$50 Cash") == "USD"


def test_parse_percent() -> None:
    assert parse_percent("80% Repair Reimbursement") == 80
    assert parse_percent("$400 per Repair") is None


# --------------------------------------------------------------------------
# fund size - the field most likely to be fabricated by a careless parser
# --------------------------------------------------------------------------
def test_fund_size_from_real_values() -> None:
    assert parse_fund_size("$100 million") == 100_000_000
    assert parse_fund_size("$4.5M Fund") == 4_500_000
    assert parse_fund_size("$167.5M") == 167_500_000


@pytest.mark.parametrize(
    "text",
    [
        "No fixed common fund disclosed \u2014 claims-made benefits",
        "None \u2014 no settlement",
        "Undisclosed",
        "Not yet verified",
        "",
        None,
    ],
)
def test_fund_size_is_none_when_not_stated(text: str | None) -> None:
    assert parse_fund_size(text) is None


def test_a_small_figure_is_not_a_fund_size() -> None:
    # A $5 co-pay is not a settlement fund.
    assert parse_fund_size("$5") is None


# --------------------------------------------------------------------------
# tier splitting
# --------------------------------------------------------------------------
def test_split_tiers_on_or() -> None:
    tiers = split_tiers("$45 Cash or Up to $2,000 + 1 Year of Credit Monitoring")
    assert tiers == ["$45 Cash", "Up to $2,000 + 1 Year of Credit Monitoring"]


def test_split_tiers_keeps_commas_that_join_one_tier() -> None:
    # A comma list is components of a single benefit, not alternatives.
    assert split_tiers("Up to $5,000 + $75 Lost Time") == ["Up to $5,000 + $75 Lost Time"]


def test_split_tiers_on_a_proof_boundary() -> None:
    tiers = split_tiers("$2.00 per unit capped $6.00 without proof, uncapped with proof")
    assert len(tiers) == 2
    assert tiers[0].endswith("without proof")
    assert tiers[1] == "uncapped with proof"


# --------------------------------------------------------------------------
# the real corpus - every live payout phrase must survive normalisation
# --------------------------------------------------------------------------
# These run against the captured llms.txt (260+ real entries). They exist
# because the unit tests above all passed while ``normalize_payout`` was
# calling a function that did not exist - nothing exercised it end-to-end.
# A full-corpus sweep is the regression net that unit strings cannot be.

def test_every_index_payout_phrase_produces_tiers(index_document) -> None:
    checked = 0
    for entry in index_document.entries:
        phrase = (entry.payout_phrase or "").strip()
        if not phrase:
            continue
        tiers = normalize_payout(phrase)
        assert tiers, f"{entry.slug}: payout phrase {phrase!r} produced no tiers"
        checked += 1
    # The live corpus has 190+ phrases with payout text; a big drop means the
    # parser regressed, not that the source got quiet.
    assert checked >= 180, f"only {checked} phrases were checked"


def test_visible_amounts_are_never_reported_as_undisclosed(index_document) -> None:
    """If a dollar figure is visible in the text, some tier must carry it."""
    for entry in index_document.entries:
        phrase = (entry.payout_phrase or "").strip()
        if not phrase or not parse_money_amounts(phrase):
            continue
        tiers = normalize_payout(phrase)
        assert tiers, f"{entry.slug}: no tiers for {phrase!r}"
        assert not all(
            str(t.payout_type) == PayoutType.UNDISCLOSED.value for t in tiers
        ), f"{entry.slug}: dropped visible amounts from {phrase!r}"


def test_deadline_clauses_never_leak_into_tier_labels(index_document) -> None:
    """Several index lines append 'Claim by <date>' to the payout phrase.

    The deadline is parsed into its own field, so tier labels must be free
    of it whatever the source line looks like.
    """
    for entry in index_document.entries:
        phrase = (entry.payout_phrase or "").strip()
        if not phrase:
            continue
        for tier in normalize_payout(phrase):
            assert "claim by" not in tier.label.lower(), (
                f"{entry.slug}: deadline text leaked into tier label "
                f"{tier.label!r} from {phrase!r}"
            )


def test_corpus_produces_the_expected_payout_type_spread(index_document) -> None:
    """The live corpus contains every important shape; losing one is a bug."""
    kinds: set[str] = set()
    for entry in index_document.entries:
        phrase = (entry.payout_phrase or "").strip()
        for tier in normalize_payout(phrase):
            kinds.add(str(tier.payout_type))
    assert {"fixed", "range", "pro_rata_fund"} <= kinds, sorted(kinds)


def test_corpus_spot_check_excel_fitness(by_slug) -> None:
    tiers = normalize_payout(by_slug("excel-fitness").payout_phrase)
    assert [str(t.payout_type) for t in tiers] == ["fixed", "range"]
    assert [t.amount_max for t in tiers] == [50.0, 4_000.0]


def test_corpus_spot_check_system_pavers_en_dash_range(by_slug) -> None:
    tiers = normalize_payout(by_slug("system-pavers").payout_phrase)
    tail = tiers[-1]
    assert tail.amount_min == 80 and tail.amount_max == 100


def test_corpus_spot_check_pro_rata_is_not_a_payout_figure(by_slug) -> None:
    """A fund size must never be stored as a claimant's payout amount."""
    tiers = normalize_payout(by_slug("oppenheimer-cash-sweep").payout_phrase)
    assert len(tiers) == 1
    assert str(tiers[0].payout_type) == "pro_rata_fund"
    assert tiers[0].amount_max is None


# --------------------------------------------------------------------------
# classify_proof - the page's own fact is authoritative
# --------------------------------------------------------------------------
def test_page_says_no_proof_overrides_a_stricter_index_label() -> None:
    assessment = classify_proof(fact_value="No", index_level="L3")
    assert assessment.required is False
    assert assessment.level == ProofLevel.L1.value
    # A disagreement is real information: report it, never hide it.
    assert any("differs" in w for w in assessment.warnings)


def test_page_says_yes_with_a_notice_id_gate_is_l2() -> None:
    # Schuster's real wording.
    assessment = classify_proof(
        fact_value="Yes", fact_detail="using the Notice ID and PIN", index_level=None
    )
    assert assessment.required is True
    assert assessment.level == ProofLevel.L2.value


def test_page_says_yes_with_document_detail_is_l3() -> None:
    assessment = classify_proof(
        fact_value="Yes",
        fact_detail="receipts or other documentation of purchase",
        index_level=None,
    )
    assert assessment.required is True
    assert assessment.level == ProofLevel.L3.value


def test_dual_tier_benefits_report_l4() -> None:
    """The Earth Rated shape: a capped no-proof tier next to an uncapped one."""
    tiers = normalize_payout("$2.00 per unit, capped $6.00 without proof, uncapped with proof")
    assessment = classify_proof(tiers=tiers, fact_value=None, index_level="L3")
    assert assessment.level == ProofLevel.L4.value
    assert assessment.required is True
    # Not a disagreement - the page's single boolean simply cannot express it.
    assert not any("differs" in w for w in assessment.warnings)


def test_unverified_page_fact_defers_to_the_index() -> None:
    assessment = classify_proof(
        fact_value="Not yet verified", index_level="L1", index_label="No proof required"
    )
    assert assessment.level == ProofLevel.L1.value
    assert assessment.required is False


def test_nothing_published_at_all_is_a_warning() -> None:
    assessment = classify_proof(fact_value=None, index_level=None)
    assert assessment.level is None
    assert assessment.warnings


# --------------------------------------------------------------------------
# detail lines that mention both gates at once
# --------------------------------------------------------------------------
def test_amend_only_documents_do_not_raise_the_bar_above_the_id() -> None:
    """Real wording from the diisocyanates page.

    "Class Member ID from the notice to file online · purchase records only to
    amend the amounts" - the ID is what files the claim; the records only
    change the figure. Reporting L3 here would tell people to dig up receipts
    they do not need, which is the worst failure this tracker can have.
    """
    assessment = classify_proof(
        fact_value="Yes",
        fact_detail=(
            "Class Member ID from the notice to file online \u00b7 purchase "
            "records only to amend the amounts"
        ),
        index_level=None,
    )
    assert assessment.level == ProofLevel.L2.value
    assert assessment.required is True


def test_documents_stated_as_required_alongside_an_id_stay_l3() -> None:
    """The inverse: when the documents really are required, say so."""
    assessment = classify_proof(
        fact_value="Yes",
        fact_detail="Notice ID to log in, plus receipts required to substantiate payment",
        index_level=None,
    )
    assert assessment.level == ProofLevel.L3.value


def test_optional_photo_does_not_raise_the_bar() -> None:
    assessment = classify_proof(
        fact_value="Yes",
        fact_detail="Use your Claim ID to file; a photo is optional if you want a higher amount",
        index_level=None,
    )
    assert assessment.level == ProofLevel.L2.value
