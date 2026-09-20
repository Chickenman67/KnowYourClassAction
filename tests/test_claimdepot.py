"""ClaimDepot source: parsing, identity matching, cross-checking, degrading.

The listing fixture is a real captured page (100 cards). The synthetic pairs
in the matching tests encode the two live failures that decided the design:
title similarity both false-pairs unrelated cases that share a dollar figure
and misses true counterparts whose titles differ - so a pair exists only when
the card slug and our published domain reduce to the same identity.
"""

from __future__ import annotations

from datetime import date

import pytest

from kya.models import Settlement
from kya.sources import claimdepot as cd
from tests.conftest import read_fixture


@pytest.fixture(scope="module")
def cards() -> list[cd.ClaimDepotCard]:
    return cd.parse_listing_page(read_fixture("claimdepot_listing.html"))


def _deadline(iso: str):
    from kya.models import Deadline

    return Deadline(raw=iso, date=date.fromisoformat(iso))


def settlement(
    sid: str,
    title: str,
    *,
    official_website: str | None = None,
    claim_url: str | None = None,
    deadline: str | None = None,
    proof_required: bool | None = None,
    proof_level: str | None = None,
    lane: str = "claimable",
) -> Settlement:
    data = dict(
        id=sid,
        source_url="https://openclassactions.com/example.php",
        title=title,
        official_website=official_website,
        claim_url=claim_url,
        proof_required=proof_required,
        proof_level=proof_level,
        lane=lane,
    )
    if deadline:
        data["deadline"] = _deadline(deadline)
    return Settlement(**data)


def card(
    slug: str,
    *,
    status: str = "Open for Claims",
    proof: str = "",
    deadline: str = "",
) -> cd.ClaimDepotCard:
    return cd.ClaimDepotCard(
        slug=slug,
        title=slug.replace("-", " ").title(),
        url=f"https://www.claimdepot.com/settlements/{slug}",
        status=status,
        proof=proof,
        deadline_text=deadline,
    )


# --------------------------------------------------------------------------
# parsing the listing
# --------------------------------------------------------------------------
def test_the_listing_fixture_parses_into_cards(cards) -> None:
    assert len(cards) == 100
    assert len({c.slug for c in cards}) == 100


def test_card_positions_are_read_from_the_right_hooks(cards) -> None:
    alaska = next(c for c in cards if c.slug == "alaska-military-leave-settlement")
    assert alaska.status == "Preliminarily Approved"
    assert alaska.payout == "Pro rata payment"
    assert alaska.deadline_date == date(2026, 10, 31)


def test_nearly_all_open_cards_carry_a_deadline(cards) -> None:
    assert sum(1 for c in cards if c.deadline_date) == 91


def test_the_proof_badge_is_only_ever_no_proof(cards) -> None:
    badged = [c for c in cards if c.proof]
    assert badged and {c.proof for c in badged} == {"No Proof"}


def test_a_days_left_counter_is_never_taken_for_a_payout(cards) -> None:
    assert not any(c.payout and "days left" in c.payout.lower() for c in cards)
    assert not any(c.payout and c.payout.strip().isdigit() for c in cards)


def test_the_pagination_token_comes_from_the_next_link() -> None:
    token = cd.parse_page_token(read_fixture("claimdepot_listing.html"))
    assert token and token.endswith("_page")


# --------------------------------------------------------------------------
# identities: the matching key
# --------------------------------------------------------------------------
def test_a_host_reduces_to_its_case_identity() -> None:
    assert (
        cd.identity_from_host("https://www.AlaskaMilitaryLeaveSettlement.com/")
        == "alaskamilitaryleavesettlement"
    )
    assert cd.identity_from_host("weitzbrsettlement.com") == "weitzbrsettlement"
    assert cd.identity_from_host("https://forms.ksacms.com/efiling/x") == "formsksacms"


def test_compound_suffixes_strip_whole() -> None:
    assert cd.identity_from_host("https://example-settlement.co.uk/faq") == "examplesettlement"


def test_hostless_input_never_matches() -> None:
    assert cd.identity_from_host(None) is None
    assert cd.identity_from_host("") is None
    assert cd.identity_from_host("localhost") is None


def test_a_slug_reduces_to_the_same_form() -> None:
    assert cd.identity_from_slug("Alaska-Military-Leave-Settlement") == "alaskamilitaryleavesettlement"
    assert cd.identity_from_slug("CHA-Settlement") == "chasettlement"


def test_row_identities_offer_both_published_domains() -> None:
    s = settlement(
        "x",
        "X",
        official_website="https://www.weitzbrsettlement.com/",
        claim_url="https://other-portal.com/claim",
    )
    assert cd.row_identities(s) == ["weitzbrsettlement", "otherportal"]


# --------------------------------------------------------------------------
# matching by identity, not by title
# --------------------------------------------------------------------------
def test_a_matching_domain_pairs_regardless_of_titles() -> None:
    # The live counter-example: slug "weitz-br-settlement" vs host
    # "weitzbrsettlement.com" - titles that share almost no words.
    rows = [
        settlement(
            "ours",
            "Banana Republic $1.95M Labor Class Action Settlement",
            official_website="https://www.weitzbrsettlement.com/",
        )
    ]
    theirs = [card("weitz-br-settlement", deadline="November 3, 2026")]
    pairs = cd.match_cards(theirs, rows)
    assert [(s.id, c.slug) for s, c in pairs] == [("ours", "weitz-br-settlement")]


def test_similar_titles_never_pair_without_a_domain_match() -> None:
    # The live counter-example: Thinkware $850K vs Dartmouth $850K ERISA.
    rows = [
        settlement(
            "dartmouth",
            "Dartmouth-Hitchcock $850,000 ERISA Class Action Settlement",
            official_website="https://www.dartmouthsettlement.com/",
        )
    ]
    theirs = [card("thinkware-dashcam-settlement")]
    assert cd.match_cards(theirs, rows) == []


def test_a_host_published_by_two_rows_is_not_a_case_identity() -> None:
    rows = [
        settlement("a", "First Case", official_website="https://shared.com/"),
        settlement("b", "Second Case", official_website="https://shared.com/other"),
    ]
    assert cd.match_cards([card("shared")], rows) == []


def test_duplicate_slugs_exclude_the_identity_entirely() -> None:
    # Two cards behind one identity: we cannot tell which is right, so the
    # identity is dropped from both sides rather than guessed at.
    rows = [settlement("a", "A", official_website="https://acme.com/")]
    assert cd.match_cards([card("acme"), card("acme")], rows) == []


def test_one_row_cannot_absorb_two_cards() -> None:
    rows = [
        settlement(
            "a", "A",
            official_website="https://aa.com/",
            claim_url="https://bb.com/claim",
        )
    ]
    assert len(cd.match_cards([card("aa"), card("bb")], rows)) == 1


# --------------------------------------------------------------------------
# cross-checking: ours is kept, only commitments are compared
# --------------------------------------------------------------------------
def test_a_deadline_conflict_is_recorded_and_ours_kept() -> None:
    s = settlement("a", "A", official_website="https://acme.com/", deadline="2026-11-20")
    out = cd.attach([s], [card("acme", deadline="November 23, 2026")])
    assert any(
        "claimdepot lists claim deadline 2026-11-23" in w and "ours kept" in w
        for w in out[0].warnings
    )
    assert out[0].deadline.date == date(2026, 11, 20)


def test_an_abstention_on_either_side_is_not_a_conflict() -> None:
    s = settlement("a", "A", official_website="https://acme.com/", deadline="2026-11-20")
    out = cd.attach([s], [card("acme")])
    assert not [w for w in out[0].warnings if "deadline" in w]


def test_a_proof_contradiction_is_recorded() -> None:
    s = settlement(
        "a", "A", official_website="https://acme.com/",
        proof_required=True, proof_level="L3",
    )
    out = cd.attach([s], [card("acme", proof="No Proof")])
    assert any("claimdepot says no proof needed" in w for w in out[0].warnings)


def test_the_dual_tier_shape_is_not_called_a_contradiction() -> None:
    # CVS-shape: row-level L4 with an easy tier. Their "No Proof" badge
    # describes the easy path; it does not contradict us.
    s = settlement(
        "a", "A", official_website="https://acme.com/",
        proof_required=True, proof_level="L4",
    )
    out = cd.attach([s], [card("acme", proof="No Proof")])
    assert not [w for w in out[0].warnings if "proof" in w]


def test_the_not_applicable_badge_is_an_abstention() -> None:
    s = settlement(
        "a", "A", official_website="https://acme.com/",
        proof_required=True, proof_level="L3",
    )
    out = cd.attach([s], [card("acme", proof="Not Applicable")])
    assert not [w for w in out[0].warnings if "proof" in w]


def test_closed_versus_our_open_is_surfaced() -> None:
    s = settlement("a", "A", official_website="https://acme.com/", lane="claimable")
    out = cd.attach([s], [card("acme", status="Closed")])
    assert any("'Closed'" in w and "check the claim deadline" in w for w in out[0].warnings)


def test_closed_versus_pending_is_not_a_conflict() -> None:
    s = settlement("a", "A", official_website="https://acme.com/", lane="pending")
    out = cd.attach([s], [card("acme", status="Closed")])
    assert not [w for w in out[0].warnings if "claimdepot" in w]


def test_posture_mismatches_are_compared_but_remedy_lanes_are_not() -> None:
    posture = settlement("a", "A", official_website="https://acme.com/", lane="claimable")
    out = cd.attach([posture], [card("acme", status="Preliminarily Approved")])
    assert any("claimdepot lists this as 'Preliminarily Approved'" in w for w in out[0].warnings)

    # Our 'automatic' lane describes what the reader must do, not the court
    # posture - "Open for Claims" alongside automatic payment is normal.
    automatic = settlement("b", "B", official_website="https://acme.com/", lane="automatic")
    out = cd.attach([automatic], [card("acme", status="Open for Claims")])
    assert not [w for w in out[0].warnings if "claimdepot" in w]


def test_agreement_attaches_the_link_and_no_warning() -> None:
    s = settlement("a", "A", official_website="https://acme.com/", deadline="2026-11-23")
    out = cd.attach([s], [card("acme", deadline="November 23, 2026")])
    assert out[0].warnings == []
    ref = out[0].cross_refs["claimdepot"][0]
    assert ref["label"] == "cross-checked"
    assert ref["href"].endswith("/settlements/acme")


# --------------------------------------------------------------------------
# degrading: a source that is down must cost a cross-check, never a build
# --------------------------------------------------------------------------
def test_enrich_swallows_a_dead_client() -> None:
    from kya.http import FetchError

    class Dead:
        def get(self, url):
            raise FetchError(url, message="no network")

    s = settlement("a", "A", official_website="https://acme.com/")
    assert cd.enrich([s], Dead()) == [s]  # type: ignore[arg-type]


def test_enrich_swallows_a_matching_bug() -> None:
    class Ok:
        def get(self, url):
            result = type("R", (), {})()
            result.ok = True
            result.status_code = 200
            result.text = read_fixture("claimdepot_listing.html")
            return result

    s = settlement("a", "A", official_website="https://acme.com/")
    out = cd.enrich([s], Ok())  # type: ignore[arg-type]
    assert isinstance(out, list) and out



