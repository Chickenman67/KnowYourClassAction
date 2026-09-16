"""Tests for the case-page parser, run against real captures.

The page is where the payout and proof details actually live: for roughly a
quarter of entries the index carries no payout phrase at all, and the
``settlement-facts`` block is the only place the real figure appears.

These tests also pin the fact that the label sets genuinely differ per page - a
pending case has no ``Estimated Payout``, and a recall carries ``Can I Claim?``
instead. A parser that assumed a fixed set of labels would silently report
"no payout" for both.
"""

from __future__ import annotations

import pytest

from kya.sources.openclassactions_page import (
    derive_category,
    find_faq_answer,
    parse_page,
)


# --------------------------------------------------------------------------
# Kia - a fully-populated, claimable settlement
# --------------------------------------------------------------------------
def test_kia_facts(kia_html: str) -> None:
    page = parse_page(kia_html, url="kia")
    assert page.fact_value("Status") == "Claims Open"
    assert page.fact_value("Claim Deadline") == "November 23, 2026"
    assert page.fact_value("Estimated Payout") == "Up to $400 per Repair"
    assert page.fact_value("Proof Required") == "Yes"
    assert page.fact("Proof Required").sub.startswith("Class Member ID, VIN")


def test_kia_details(kia_html: str) -> None:
    page = parse_page(kia_html, url="kia")
    assert page.details.get("Case Title") == "Le Beau, et al. v. Kia America, Inc., et al."
    assert page.details.get("Case Number") == "8:22-cv-01545-FWS-JDE"
    assert "Central District of California" in (page.details.get("Court") or "")
    assert page.details.get("Administrator") == "Kroll Settlement Administration LLC"
    assert (
        page.details.links["Official Website"]
        == "https://www.kiawindowregulatorsettlement.com/"
    )


def test_kia_claim_url_prefers_the_claim_portal(kia_html: str) -> None:
    page = parse_page(kia_html, url="kia")
    assert page.claim_url is not None
    assert page.claim_url.startswith("https://forms.ksacms.com/")
    # Tracking parameters are stripped; real form parameters are kept.
    assert "utm_source" not in page.claim_url
    assert "form-version=1" in page.claim_url


def test_kia_structured_data(kia_html: str) -> None:
    page = parse_page(kia_html, url="kia")
    assert page.headline
    assert page.date_modified == "2026-08-31T16:00:00+00:00"
    assert page.faq, "expected FAQ pairs from JSON-LD"
    assert page.warnings == []


def test_kia_has_no_category_because_the_crumb_is_generic(kia_html: str) -> None:
    page = parse_page(kia_html, url="kia")
    # Home > Settlements > Kia... : "Settlements" is not a category.
    assert page.breadcrumb == ["Home", "Settlements", "Kia Window Regulator Settlement"]
    assert page.category is None


# --------------------------------------------------------------------------
# Schuster - payout absent from the index, present on the page
# --------------------------------------------------------------------------
def test_schuster_recovers_the_missing_payout(schuster_html: str) -> None:
    page = parse_page(schuster_html, url="schuster")
    # The index lists this case with no payout phrase at all.
    assert page.fact_value("Estimated Payout") == "$50 or up to $2,500"


def test_schuster_proof_is_a_notice_id_gate(schuster_html: str) -> None:
    page = parse_page(schuster_html, url="schuster")
    assert page.fact_value("Proof Required") == "Yes"
    sub = page.fact("Proof Required").sub
    assert "LoginID and PIN" in sub
    # ...and a documented tier sits on top of the flat no-documentation cash.
    assert "receipts required" in sub


def test_schuster_category_and_claim_url(schuster_html: str) -> None:
    page = parse_page(schuster_html, url="schuster")
    assert page.category == "Data Breaches"
    assert page.claim_url is not None
    assert page.claim_url.startswith("https://schusterdataincident.com")
    assert "utm_source" not in page.claim_url



# --------------------------------------------------------------------------
# Equifax - pending: no payout, no claim portal, unverified deadline
# --------------------------------------------------------------------------
def test_pending_case_has_no_estimated_payout(equifax_html: str) -> None:
    page = parse_page(equifax_html, url="equifax")
    assert "Estimated Payout" not in page.facts
    # A fund size is not a payout, and must not be treated as one.
    assert page.fact_value("Settlement Fund") == "$100 million"


def test_pending_placeholders_are_recognised(equifax_html: str) -> None:
    page = parse_page(equifax_html, url="equifax")
    deadline = page.fact("Claim Deadline")
    proof = page.fact("Proof Required")
    assert deadline is not None and proof is not None
    assert deadline.value == "Not yet verified"
    # "Not yet verified" is not a value; it is the absence of one.
    assert deadline.is_known() is False
    assert proof.is_known() is False


def test_pending_case_has_no_claim_url(equifax_html: str) -> None:
    page = parse_page(equifax_html, url="equifax")
    assert page.claim_url is None


# --------------------------------------------------------------------------
# Ryobi - a lawsuit/recall: explicit "nothing to claim"
# --------------------------------------------------------------------------
def test_recall_page_says_nothing_is_claimable(ryobi_html: str) -> None:
    page = parse_page(ryobi_html, url="ryobi")
    answer = page.can_i_claim()
    assert answer is not None
    assert answer.lower().startswith("no")
    assert "Estimated Payout" not in page.facts
    assert page.claim_url is None


def test_recall_page_facts_and_category(ryobi_html: str) -> None:
    page = parse_page(ryobi_html, url="ryobi")
    assert page.fact_value("Recall Remedy") == "Free Replacement Mower"
    assert page.details.get("Settlement Fund") == "None \u2014 no settlement"
    assert page.category == "Class Action Investigations"


# --------------------------------------------------------------------------
# resilience - one changed page must not break a daily run
# --------------------------------------------------------------------------
def test_empty_html_does_not_raise() -> None:
    page = parse_page("", url="empty")
    assert page.facts == {}
    assert page.title is None
    assert "no quick-facts block found" in page.warnings


def test_truncated_html_does_not_raise(kia_html: str) -> None:
    page = parse_page(kia_html[:5000], url="truncated")
    assert page is not None


def test_invalid_jsonld_is_ignored(kia_html: str) -> None:
    broken = kia_html.replace('type="application/ld+json"', 'type="application/ld+json"', 1)
    broken = broken.replace("{\n            \"@context\"", "{not json", 1)
    page = parse_page(broken, url="broken")
    # The HTML blocks still parse even when one JSON-LD block is malformed.
    assert page.fact_value("Estimated Payout") == "Up to $400 per Repair"


@pytest.mark.parametrize(
    ("breadcrumb", "expected"),
    [
        ([], None),
        (["Home", "Settlements"], None),
        (["Home", "Settlements", "Kia Settlement"], None),
        (["Home", "Settlements", "Data Breaches", "Schuster"], "Data Breaches"),
        (["Home", "Class Action Investigations", "Ryobi"], "Class Action Investigations"),
        (["Home", "Lawsuits", "Kia"], None),
    ],
)
def test_derive_category(breadcrumb: list[str], expected: str | None) -> None:
    assert derive_category(breadcrumb) == expected


def test_find_faq_answer(kia_html: str) -> None:
    page = parse_page(kia_html, url="kia")
    answer = find_faq_answer(page, "service card")
    assert answer is not None
    assert "No." in answer or "choose" in answer


def test_every_captured_page_parses_without_warnings(
    kia_html: str, schuster_html: str, cvs_html: str, equifax_html: str, ryobi_html: str
) -> None:
    for html in (kia_html, schuster_html, cvs_html, equifax_html, ryobi_html):
        page = parse_page(html, url="fixture")
        assert page.title, "every case page should yield a title"
        assert page.facts, "every case page should yield a quick-facts block"
        assert page.warnings == []