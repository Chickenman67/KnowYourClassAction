"""Tests for the ``llms.txt`` index parser, run against a real capture.

The most important tests here cover the **field-order trap**: upstream's
trailing ``·``-separated field is sometimes a proof requirement and sometimes a
geographic scope, in either order. If that logic regresses, the tracker
silently starts telling people to produce documents they do not need, or shows
them settlements they cannot join.
"""

from __future__ import annotations

import pytest

from kya.deadlines import parse_deadline
from kya.models import DeadlineKind, ProofLevel
from kya.sources.openclassactions_index import IndexDocument

# A frozen capture of the published index.
EXPECTED_LAST_UPDATED = "2026-09-14"


# --------------------------------------------------------------------------
# document shape
# --------------------------------------------------------------------------
def test_parse_produces_entries(index_document: IndexDocument) -> None:
    assert len(index_document) > 200


def test_sections_are_split(index_document: IndexDocument) -> None:
    assert index_document.open_entries()
    assert index_document.unverified_entries()
    total = len(index_document.open_entries()) + len(index_document.unverified_entries())
    assert total == len(index_document)


def test_last_updated_captured(index_document: IndexDocument) -> None:
    assert index_document.last_updated == EXPECTED_LAST_UPDATED


def test_every_entry_has_url_and_title(index_document: IndexDocument) -> None:
    for entry in index_document.entries:
        assert entry.url.startswith("https://")
        assert entry.title.strip()
        assert entry.slug


def test_every_open_entry_has_a_deadline_string(index_document: IndexDocument) -> None:
    for entry in index_document.open_entries():
        assert entry.deadline_raw, f"missing deadline for {entry.url}"


# --------------------------------------------------------------------------
# the field-order trap
# --------------------------------------------------------------------------
def test_proof_and_geo_coexist(index_document: IndexDocument, by_slug) -> None:
    # "... · ID/code from notice required · WA residents"
    quality_inn = by_slug("quality-inn-seatac")
    assert quality_inn.proof_level == ProofLevel.L2.value
    assert quality_inn.geo.states == ["WA"]

    # "... · ID/code from notice required · PA residents"
    sportsmans = by_slug("sportsmans-guide")
    assert sportsmans.proof_level == ProofLevel.L2.value
    assert sportsmans.geo.states == ["PA"]


def test_id_code_is_never_read_as_idaho(index_document: IndexDocument) -> None:
    """``ID/code from notice required`` must not create an Idaho state filter."""
    idaho = [entry for entry in index_document.entries if "ID" in (entry.geo.states or [])]
    assert idaho == [], f"Idaho false positive on: {[e.url for e in idaho]}"


def test_automatic_payment_plus_state(index_document: IndexDocument, by_slug) -> None:
    # "... · Automatic payment — no claim form · CA residents"
    scale_ai = by_slug("scale-ai")
    assert scale_ai.proof_level == ProofLevel.L0.value
    assert scale_ai.geo.states == ["CA"]


# --------------------------------------------------------------------------
# specific known settlements
# --------------------------------------------------------------------------
def test_kia_entry(by_slug) -> None:
    kia = by_slug("kia-window-regulator")
    assert kia.proof_level == ProofLevel.L3.value
    assert kia.deadline_raw == "November 23, 2026"
    assert kia.payout_phrase == "Up to $400 per Repair"
    assert kia.case_name == "Kia Window Regulator Settlement"
    assert kia.geo.scope_type == "unspecified"


def test_country_bank_is_automatic(by_slug) -> None:
    bank = by_slug("country-bank-savings-overdraft")
    assert bank.proof_level == ProofLevel.L0.value
    assert parse_deadline(bank.deadline_raw).kind == DeadlineKind.NO_ACTION


def test_multi_state_purchase_restriction(index_document: IndexDocument) -> None:
    multi = [
        candidate
        for candidate in index_document.entries
        if candidate.geo.states and len(candidate.geo.states) > 5
    ]
    assert multi, "expected at least one multi-state purchase restriction"
    for candidate in multi:
        assert candidate.geo.scope_type == "states"


# --------------------------------------------------------------------------
# investigations must not masquerade as no-proof money
# --------------------------------------------------------------------------
def test_free_case_review_is_not_a_proof_level(index_document: IndexDocument) -> None:
    flagged = [entry for entry in index_document.entries if entry.is_investigation]
    assert len(flagged) == 4
    for entry in flagged:
        # Invitations to join litigation, not claimable settlements.
        assert entry.proof_level is None