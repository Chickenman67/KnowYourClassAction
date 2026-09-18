"""Pipeline assembly tests - index + captured pages -> Settlement."""

import json
from pathlib import Path

import pytest

from kya.build import build_all, build_settlement
from kya.sources.openclassactions_index import IndexEntry, parse_index
from kya.sources.openclassactions_page import parse_page

FIXTURES = Path(__file__).parent / "fixtures"

# NZXT's real page shape: a quick-facts block, a case-details block whose
# "Official Website" entry holds a CourtListener docket, and no a.file-claim.
_DOCKET_PAGE = """
<html><body>
<h1>NZXT Flex PC Rental Settlement</h1>
<section class="settlement-facts">
  <div class="fact"><span class="fact-label">Status</span>
  <span class="fact-value">Claims Open</span></div>
</section>
<section class="settlement-case-details">
  <div class="detail"><span class="detail-label">Case Title</span>
  <span class="detail-value">Burns v. Fragile, Inc. and NZXT, Inc.</span></div>
  <div class="detail"><span class="detail-label">Official Website</span>
  <span class="detail-value"><a href="https://www.courtlistener.com/docket/71033918/burns-v-fragile-inc/">CourtListener Docket</a></span></div>
</section>
</body></html>
"""


@pytest.fixture(scope="module")
def pages() -> dict:
    manifest = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
    out = {}
    for name, meta in manifest.items():
        if not name.startswith("page_"):
            continue
        result = parse_page(
            (FIXTURES / f"{name}.html").read_text(encoding="utf-8"), meta["url"]
        )
        page = result[0] if isinstance(result, tuple) else result
        slug = meta["url"].rsplit("/", 1)[-1].replace(".php", "")
        out[slug] = page
    return out


@pytest.fixture(scope="module")
def settlements(index_document, pages):
    return build_all(index_document, pages)


def by_id(settlements, needle):
    matches = [s for s in settlements if needle in s.id]
    assert matches, f"no settlement matching {needle!r}"
    return matches[0]


# --------------------------------------------------------------------------
# document-level invariants
# --------------------------------------------------------------------------
def test_every_entry_becomes_a_settlement(index_document, settlements) -> None:
    assert len(settlements) == len(index_document.entries)
    assert len({s.id for s in settlements}) == len(settlements)


def test_all_four_lanes_are_inhabited(settlements) -> None:
    lanes = {s.lane for s in settlements}
    assert {"claimable", "automatic", "investigation", "pending"} <= lanes


def test_every_settlement_has_a_lane_reason(settlements) -> None:
    for s in settlements:
        assert s.lane_reason, f"{s.id}: no lane_reason recorded"


def test_content_hash_is_stable_and_sensitive(index_document) -> None:
    """Same inputs -> same hash; a payout change -> a different hash."""
    entry = index_document.open_entries()[0]
    s1 = build_settlement(entry)
    s2 = build_settlement(entry)
    assert s1.content_hash == s2.content_hash
    changed = IndexEntry(
        url=entry.url,
        title=entry.title,
        section=entry.section,
        payout_phrase="Up to $9,000",
        deadline_raw=entry.deadline_raw,
        proof_level=entry.proof_level,
    )
    assert build_settlement(changed).content_hash != s1.content_hash


# --------------------------------------------------------------------------
# page enrichment is wired through
# --------------------------------------------------------------------------
def test_page_enriches_kia_with_court_and_claim_url(settlements) -> None:
    kia = by_id(settlements, "kia-window-regulator")
    assert kia.claim_url
    assert kia.court and "California" in kia.court
    assert kia.case_number
    assert kia.verified_as_of  # a real page was read: stamp it


def test_page_enriches_equifax_with_court(settlements) -> None:
    eq = by_id(settlements, "equifax-credit-score-error")
    assert eq.lane == "pending"
    assert eq.court and "Georgia" in eq.court
    assert eq.claim_url is None  # pending: no portal yet


def test_page_only_settlements_are_stamped_verified(settlements) -> None:
    enriched = [s for s in settlements if s.claim_url or s.case_number]
    assert len(enriched) == 8  # the captured fixtures
    assert all(s.verified_as_of for s in enriched)
    unstamped = [s for s in settlements if not s.verified_as_of]
    assert len(unstamped) == len(settlements) - 8


# --------------------------------------------------------------------------
# disagreements are flagged, never silently resolved
# --------------------------------------------------------------------------
def test_country_bank_automatic_survives_page_enrichment(settlements) -> None:
    """The page's Proof Required boolean cannot say "automatic" - but its
    Claim Required fact can ("No - Automatic"), so both sources agree and no
    false disagreement may be invented. Enrichment must not demote a
    no-action case into a documents-or-nothing claimable row."""
    cb = by_id(settlements, "country-bank")
    assert cb.proof_level == "L0"
    assert cb.proof_required is False
    assert cb.lane == "automatic"
    assert not any("differs" in w for w in cb.warnings)


def test_schuster_id_gate_is_not_reported_as_documents(settlements) -> None:
    """Index says ID/code (L2); the page confirms it - LoginID and PIN to
    file, with receipts scoped only to the up-to-$2,500 losses option. Both
    sources agree, so no disagreement may be invented; the old code read the
    tier-scoped receipts as the entry bar."""
    sch = by_id(settlements, "schuster-data-breach")
    assert sch.proof_level == "L2"
    assert sch.proof_required is True
    assert not any("differs" in w for w in sch.warnings)
    assert "receipts" in (sch.proof_detail or "").lower()


def test_no_settlement_repeats_a_warning(settlements) -> None:
    """The same warning twice reads as two problems.

    Both the index pass and the page pass call ``classify_proof``, and a page
    that publishes no proof requirement yields "no proof requirement published"
    from each - so five rows in the live dataset carried it duplicated. Merge
    is where the two passes meet, so merge is where repeats are collapsed.
    """
    offenders = {
        s.id: [w for w in s.warnings if s.warnings.count(w) > 1]
        for s in settlements
        if len(s.warnings) != len(set(s.warnings))
    }
    assert not offenders, f"duplicated warnings: {offenders}"


def test_a_docket_is_never_the_official_settlement_website(settlements) -> None:
    """No row may present a court record as its own site."""
    from kya.sources.openclassactions_page import is_court_record_link

    for s in settlements:
        for url in (s.claim_url, s.official_website):
            assert not (
                url and is_court_record_link(url)
            ), f"{s.id} links a court record as its own site: {url}"


def test_a_rejected_site_is_recorded_not_silently_dropped() -> None:
    """Regression: NZXT's source page files its docket under "Official Website".

    The link is dropped rather than relabelled, because a docket cannot take a
    claim - and the omission is recorded, because a row whose site was rejected
    must not look like a row that never had one.
    """
    from kya.build import merge_page
    from kya.models import Settlement

    settlement = Settlement(
        id="nzxt-flex-pc-rental-class-action-settlement",
        source_url="https://openclassactions.com/settlements/nzxt.php",
        title="NZXT Flex PC Rental $3.45M Settlement",
    )
    page = parse_page(_DOCKET_PAGE, url="nzxt")

    merged = merge_page(settlement, page)

    assert merged.official_website is None
    assert merged.claim_url is None
    assert any("court record" in w for w in merged.warnings)


def test_ryobi_is_a_recall_in_the_investigation_lane(settlements) -> None:
    ryobi = by_id(settlements, "ryobi-40v-mower-fire")
    assert str(ryobi.kind) == "recall"
    assert ryobi.lane == "investigation"


def test_kia_is_claimable_with_a_payout_tier(settlements) -> None:
    kia = by_id(settlements, "kia-window-regulator")
    assert kia.lane == "claimable"
    assert kia.payout_tier is not None
    assert kia.ev_estimate is not None
