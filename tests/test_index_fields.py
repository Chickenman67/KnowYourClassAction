"""Unit tests for the field-level parsing rules.

These rules decide whether a settlement is shown as "no proof needed", "notice
ID needed" or "documents needed" - and whether a state restriction is applied
at all. Getting them wrong is worse than getting nothing, because the output
still looks authoritative.
"""

from __future__ import annotations

import pytest

from kya.models import ProofLevel
from kya.sources.openclassactions_index import (
    guess_proof_level,
    normalize_label,
    parse_geo,
    parse_index,
    split_title,
)


# --------------------------------------------------------------------------
# normalize_label
# --------------------------------------------------------------------------
def test_normalize_label_unifies_dash_variants() -> None:
    # Upstream uses an em dash; config and tests may use an ASCII hyphen.
    assert normalize_label("Automatic payment \u2014 no claim form") == normalize_label(
        "Automatic payment - no claim form"
    )


def test_normalize_label_strips_trailing_period_and_case() -> None:
    assert normalize_label("  Documentation Required.  ") == "documentation required"


# --------------------------------------------------------------------------
# split_title
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("title", "expected_name", "expected_phrase"),
    [
        (
            "Kia Window Regulator Settlement \u2014 Up to $400 per Repair",
            "Kia Window Regulator Settlement",
            "Up to $400 per Repair",
        ),
        (
            "Guitar Center $2.4M Wage and Hour Class Action Settlement",
            "Guitar Center $2.4M Wage and Hour Class Action Settlement",
            None,
        ),
        # A bare hyphen must not split a product name in half.
        (
            "Bestway Above-Ground Pool Settlement",
            "Bestway Above-Ground Pool Settlement",
            None,
        ),
        # A spaced ASCII hyphen is a legitimate separator.
        (
            "Something Settlement - $10 Cash",
            "Something Settlement",
            "$10 Cash",
        ),
    ],
)
def test_split_title(title: str, expected_name: str, expected_phrase: str | None) -> None:
    name, phrase = split_title(title)
    assert name == expected_name
    assert phrase == expected_phrase


# --------------------------------------------------------------------------
# parse_geo
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("segment", "scope_type", "states"),
    [
        ("CA residents", "states", ["CA"]),
        ("WA job applicants", "states", ["WA"]),
        ("CA & CO purchases only", "states", ["CA", "CO"]),
        (
            "CA, CT, DE, ME, MD, MA, OR, PA, RI, VT & WA purchases only",
            "states",
            ["CA", "CT", "DE", "MA", "MD", "ME", "OR", "PA", "RI", "VT", "WA"],
        ),
        ("NYC retirees only", "states", ["NY"]),
        ("Ohio clubs only", "states", ["OH"]),
        ("Pennsylvania record requests", "states", ["PA"]),
        ("Missouri Schnucks Rewards members", "states", ["MO"]),
        ("Canada only", "country", []),
    ],
)
def test_parse_geo_recognises_selectors(
    segment: str, scope_type: str, states: list[str]
) -> None:
    geo = parse_geo(segment)
    assert geo is not None
    assert geo.scope_type == scope_type
    if states:
        assert geo.states == sorted(states)


@pytest.mark.parametrize(
    "segment",
    [
        "Documentation required",
        "No proof required",
        "Class Member ID Required",
        "recall remedy only; the class action has nothing to claim",
        "Servicemembers and dependents who took a FirstCash or Cash America pawn "
        "loan since October 3, 2016",
        "Model number required",
    ],
)
def test_non_geo_segments_are_not_geography(segment: str) -> None:
    assert parse_geo(segment) is None


def test_long_narrative_with_a_state_is_not_a_state_filter() -> None:
    segment = (
        "California reps are paid unless they opt out; SDRs in every other state "
        "must cash the check or elect electronic payment"
    )
    geo = parse_geo(segment)
    assert geo is not None
    # It mentions California, but it is not a clean selector, so we refuse to
    # turn it into a precise state filter and keep the original wording instead.
    assert geo.scope_type == "other"
    assert geo.label == segment


# --------------------------------------------------------------------------
# guess_proof_level
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Documentation required", ProofLevel.L3.value),
        ("ID/code from notice required", ProofLevel.L2.value),
        ("Class Member ID Required", ProofLevel.L2.value),
        ("using the Notice ID and PIN", ProofLevel.L2.value),
        ("receipts required for the up-to-$2,500 losses option", ProofLevel.L3.value),
        ("No proof required", ProofLevel.L1.value),
        ("Automatic payment \u2014 no claim form", ProofLevel.L0.value),
        ("something entirely unrecognisable", None),
        ("", None),
        (None, None),
    ],
)
def test_guess_proof_level(text: str | None, expected: str | None) -> None:
    assert guess_proof_level(text) == expected


# --------------------------------------------------------------------------
# section gating
# --------------------------------------------------------------------------
def test_parse_index_ignores_non_entry_sections() -> None:
    payload = (
        "# Title\n\n"
        "## Guides & Core Logic\n"
        "- [How to file a claim](https://example.com/blog/how.php): An explainer.\n"
        "\n"
        "## Open Settlements \u2014 Claims Currently Open (Directory)\n"
        "Last updated: 2026-01-02\n"
        "- [Example \u2014 $10](https://example.com/settlements/ex.php): "
        "Deadline: March 4, 2026 \u00b7 No proof required\n"
    )
    document = parse_index(payload)
    assert len(document) == 1
    assert document.last_updated == "2026-01-02"
    entry = document.entries[0]
    assert entry.proof_level == ProofLevel.L1.value
    assert entry.payout_phrase == "$10"


def test_unverified_section_marks_entries_pending() -> None:
    payload = (
        "## Recent Settlements and Lawsuits \u2014 No Verified Claim Portal\n"
        "- [Equifax $100M Settlement](https://example.com/settlements/eq.php): "
        "Pending final approval \u00b7 Claim deadline and proof requirements not yet verified\n"
    )
    document = parse_index(payload)
    assert len(document) == 1
    entry = document.entries[0]
    assert entry.section == "unverified"
    assert entry.is_pending is True
    assert entry.proof_level is None