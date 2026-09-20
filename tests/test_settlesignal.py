"""SettleSignal source: parsing, matching, cross-checking, importing.

The fixture CSV is real captured catalog rows - chosen so three fixture
settlements match by official domain (Schuster, Hearthside, Kia) under the
shipped rule, plus rows exercising every import edge. A live calibration
against the full catalog fixed the match rule: a domain match must be
corroborated by title similarity, because several administrators serve many
cases from one shared claim-portal host (forms.ksacms.com serves both Kia
window-regulator and Banner Health claim forms).
"""

from __future__ import annotations

from datetime import date

import pytest

from kya.models import Deadline, Settlement
from kya.sources import settlesignal as ss
from tests.conftest import read_fixture


@pytest.fixture(scope="module")
def records() -> list[ss.SettleSignalRecord]:
    return ss.parse_settlesignal_csv(read_fixture("settlesignal_sample.csv"))


def settlement(
    sid: str,
    title: str,
    *,
    claim_url: str | None = None,
    official_website: str | None = None,
    deadline: str | None = None,
    proof_required: bool | None = None,
) -> Settlement:
    return Settlement(
        id=sid,
        source_url="https://openclassactions.com/example.php",
        title=title,
        claim_url=claim_url,
        official_website=official_website,
        deadline=Deadline(raw=deadline or "", date=date.fromisoformat(deadline) if deadline else None),
        proof_required=proof_required,
    )


def kia_record(records) -> ss.SettleSignalRecord:
    return next(r for r in records if "Kia Optima" in r.title)


def by_title(records, fragment: str) -> ss.SettleSignalRecord:
    return next(r for r in records if fragment in r.title)


# --------------------------------------------------------------------------
# parsing the catalog
# --------------------------------------------------------------------------
def test_the_sample_catalog_parses(records) -> None:
    assert len(records) == 7
    kia = kia_record(records)
    assert kia.claim_deadline == "2026-11-23"
    assert kia.deadline_date == date(2026, 11, 23)
    assert kia.evidence_ok is True


def test_state_lists_are_parsed_into_uppercase_codes(records) -> None:
    row = by_title(records, "Gutierrez")
    assert row.states and all(s == s.upper() for s in row.states)


def test_a_malformed_row_is_skipped_not_fatal() -> None:
    text = "title,url\nGood Case,https://example.com/a/\nNoUrl,\n"
    out = ss.parse_settlesignal_csv(text)
    assert len(out) == 1
    assert out[0].title == "Good Case"


def test_an_empty_deadline_is_not_a_date(records) -> None:
    assert by_title(records, "SpotHero").deadline_date is None


# --------------------------------------------------------------------------
# cross-check by official domain
# --------------------------------------------------------------------------
def test_a_matched_pair_agrees_and_carries_the_link(records) -> None:
    s = settlement(
        "kia-window-regulator",
        "Kia Window Regulator Class Action Settlement",
        official_website="https://www.kiawindowregulatorsettlement.com/",
        deadline="2026-11-23",
    )
    out = ss.attach([s], records)
    assert out[0].warnings == []
    assert out[0].cross_refs["settlesignal"][0]["label"] == "cross-checked"


def test_a_deadline_conflict_is_recorded_and_ours_kept(records) -> None:
    s = settlement(
        "kia-window-regulator",
        "Kia Window Regulator Class Action Settlement",
        official_website="https://www.kiawindowregulatorsettlement.com/",
        deadline="2026-11-20",
    )
    out = ss.attach([s], records)
    assert any(
        "settlesignal lists claim deadline 2026-11-23" in w and "ours kept" in w
        for w in out[0].warnings
    )
    assert out[0].deadline.date == date(2026, 11, 20)


def test_a_missing_deadline_is_filled_from_accepted_evidence(records) -> None:
    s = settlement(
        "kia-window-regulator",
        "Kia Window Regulator Class Action Settlement",
        official_website="https://www.kiawindowregulatorsettlement.com/",
    )
    out = ss.attach([s], records)
    assert out[0].deadline.date == date(2026, 11, 23)


def test_a_missing_claim_portal_is_filled(records) -> None:
    s = settlement(
        "kia-window-regulator",
        "Kia Window Regulator Class Action Settlement",
        official_website="https://www.kiawindowregulatorsettlement.com/",
    )
    out = ss.attach([s], records)
    assert out[0].claim_url == kia_record(records).official_claim_url


def test_a_proof_disagreement_is_recorded(records) -> None:
    s = settlement(
        "hearthside-illinois-child-labor-assurance",
        "Hearthside Illinois Child Labor Settlement",
        official_website="https://www.hearthsidefssettlement.com/",
        proof_required=False,
    )
    assert by_title(records, "Hearthside").proof_required == "yes"  # the pair disagrees
    out = ss.attach([s], records)
    assert any("settlesignal says proof required" in w for w in out[0].warnings)


def test_a_shared_claim_portal_is_not_a_case_match() -> None:
    """forms.ksacms.com serves many cases; the domain alone must not match."""
    banner = ss.SettleSignalRecord(
        title="Banner Health - Data Privacy",
        url="https://settlesignal.com/settlements/banner-health-data-privacy/",
        status="Open for claims",
        proof_required="no",
        claim_deadline="2026-09-03",
        official_claim_url="https://forms.ksacms.com/efiling/fr/eform/mcculley_v_banner_claimform/new",
        official_settlement_url="https://bannerhealthdatasettlement.com/",
        accepted_official_evidence=True,
    )
    s = settlement(
        "kia-window-regulator",
        "Kia Window Regulator Class Action Settlement",
        claim_url="https://forms.ksacms.com/efiling/fr/eform/kiawindowregulatorsettlement_claimform/new",
        deadline="2026-11-23",
    )
    out = ss.attach([s], [banner])
    assert out[0].warnings == []  # no false conflict with Banner Health's deadline
    assert out[0].cross_refs == {}


# --------------------------------------------------------------------------
# importing uncovered cases
# --------------------------------------------------------------------------
def test_open_cases_import_into_the_claimable_lane(records) -> None:
    s = settlement("existing", "An Existing Case")
    out = ss.attach([s], records, import_new=True)
    gutierrez = next(x for x in out if x.source == "settlesignal" and "Gutierrez" in x.title)
    assert str(gutierrez.lane) == "claimable"
    assert gutierrez.geo.states == ["CA"]
    assert gutierrez.deadline.date == by_title(records, "Gutierrez").deadline_date
    assert type(gutierrez.payout_tier) is str  # scoring ran (the enum-leak lesson)


def test_automatic_cases_import_into_the_automatic_lane(records) -> None:
    out = ss.attach([settlement("existing", "An Existing Case")], records, import_new=True)
    spot = next(x for x in out if "SpotHero" in x.title)
    assert str(spot.lane) == "automatic"


def test_closed_rows_are_never_imported(records) -> None:
    out = ss.attach([settlement("existing", "An Existing Case")], records, import_new=True)
    assert not any("STIIIZY" in x.title for x in out)


def test_rows_without_accepted_evidence_are_never_imported() -> None:
    manual = ss.SettleSignalRecord(
        title="Unverified Case",
        url="https://settlesignal.com/settlements/unverified-case/",
        status="Open for claims",
        accepted_official_evidence=False,
    )
    out = ss.attach([settlement("existing", "An Existing Case")], [manual], import_new=True)
    assert len(out) == 1, "a row without accepted evidence became a fact"


def test_import_can_be_switched_off(records) -> None:
    out = ss.attach([settlement("existing", "An Existing Case")], records, import_new=False)
    assert not any(x.source == "settlesignal" for x in out)


def test_matched_cases_are_not_re_imported(records) -> None:
    """A domain match covers the case; its row must not become a duplicate."""
    s = settlement(
        "kia-window-regulator",
        "Kia Window Regulator Class Action Settlement",
        official_website="https://www.kiawindowregulatorsettlement.com/",
        deadline="2026-11-23",
    )
    out = ss.attach([s], records, import_new=True)
    assert sum(1 for x in out if "Kia" in x.title) == 1


def test_empty_records_are_a_noop() -> None:
    s = settlement("existing", "An Existing Case")
    out = ss.attach([s], [])
    assert out == [s]


# --------------------------------------------------------------------------
# fetch + wiring
# --------------------------------------------------------------------------
class FakeClient:
    def __init__(self, body: str = "", status: int = 200, fail: bool = False):
        self.body = body
        self.status = status
        self.fail = fail
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if self.fail:
            raise ss.FetchError(url, message="boom")
        from kya.http import FetchResult

        return FetchResult(url=url, status_code=self.status, text=self.body)


def test_a_failed_fetch_costs_nothing() -> None:
    lines_out: list[str] = []
    s = settlement("existing", "An Existing Case")
    out = ss.enrich([s], FakeClient(fail=True), out=lines_out.append)
    assert out == [s]
    assert any("continuing without it" in line for line in lines_out)


def test_an_http_error_costs_nothing() -> None:
    s = settlement("existing", "An Existing Case")
    out = ss.enrich([s], FakeClient(status=503, body="nope"))
    assert out == [s]
