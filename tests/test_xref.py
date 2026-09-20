"""Secondary-source cross-references: feed parsing, matching, docket search.

All offline. The matching rules and thresholds in xref.py were tuned against
a live probe of the real RSS feed and the real CourtListener API - these
tests freeze the decisions that probe justified (see the docstrings there).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from kya.http import FetchError, FetchResult
from kya.models import Settlement
from kya import xref


def settlement(sid="case-a", title="Case A", *, lane="claimable", case_title=None, ev=None):
    return Settlement(
        id=sid,
        source_url="https://example.com/a",
        title=title,
        tiers=[],
        lane=lane,
        case_title=case_title,
        ev_estimate=ev,
    )


def item(title, link="https://topclassactions.com/x/", published="Thu, 17 Sep 2026"):
    return xref.FeedItem(title=title, link=link, published=published)


# --- feed parsing ----------------------------------------------------------------
FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Top Class Actions</title>
<item><title><![CDATA[$9.37M Concora Credit &amp; TCPA class action settlement]]></title>
<link>https://topclassactions.com/concora/</link><pubDate>Thu, 17 Sep 2026 18:00:00 +0000</pubDate></item>
<item><title>Second item &rsquo;with entities&#124; here</title>
<link>https://topclassactions.com/second/</link></item>
<item><title>Item without a link</title></item>
</channel></rss>"""


def test_parse_feed_handles_cdata_entities_and_skips_incomplete_items() -> None:
    items = xref.parse_feed(FEED_XML)
    assert [i.title for i in items] == [
        "$9.37M Concora Credit & TCPA class action settlement",
        "Second item \u2019with entities| here",
    ]
    assert items[0].link == "https://topclassactions.com/concora/"
    assert items[0].published.startswith("Thu, 17 Sep 2026")


def test_parse_feed_yields_nothing_for_garbage_and_honours_the_limit() -> None:
    assert xref.parse_feed("<html>not a feed</html>") == []
    assert len(xref.parse_feed(FEED_XML, limit=1)) == 1


# --- tokenizing and similarity ---------------------------------------------------
def test_tokenizer_folds_hyphens_apostrophes_and_hashes() -> None:
    assert xref.title_tokens("Non-bank ATM's") == xref.title_tokens("nonbank atms")
    assert xref.title_tokens("VSL#3") == xref.title_tokens("vsl3")


def test_similarity_ignores_generic_legal_boilerplate() -> None:
    assert xref.similarity("Guitar Center wage settlement", "CRST Expedited wage case") == 0.0
    assert xref.similarity("Costa Del Mar sunglasses", "sunglasses from Costa Del Mar") == 1.0
    assert xref.similarity("anything", "") == 0.0


def test_distinctive_tokens_break_length_ties_alphabetically() -> None:
    """Ties resolve to the alphabetically first tokens, not to set order.

    Four equal-length distinctive tokens for a three-token budget: without a
    total order the winner is whichever the frozenset happens to yield first.
    """
    title = "Kibble Widget Gadget Brands Settlement"
    assert xref.match_tokens(title) == {"kibble", "widget", "gadget", "brands"}
    assert xref._distinctive_tokens(title) == {"brands", "gadget", "kibble"}


def test_token_selection_is_hash_seed_independent() -> None:
    """The same case must select the same tokens in a different process.

    ``sorted(..., key=len)`` leaves equal-length tokens in frozenset iteration
    order, which follows the interpreter's hash seed - so two runs could pick
    different distinctive subsets and emit different docket queries. That is
    noise in a committed dataset, and the digest would report the change.
    Seeds 1-3 genuinely shuffle str hashing; 0 disables it and proves nothing.
    """
    title = "Kibble Widget Gadget Brands Settlement"
    case_title = "Gibson v. National Association of Realtors"
    program = (
        "from kya import xref\n"
        f"s = xref.Settlement(id='x', source_url='u', title={title!r},"
        f" tiers=[], case_title={case_title!r})\n"
        f"print(sorted(xref._distinctive_tokens({title!r})), xref.docket_query(s))\n"
    )
    src = Path(xref.__file__).resolve().parents[1]
    outputs = set()
    for seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(src)}
        proc = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, env=env
        )
        assert proc.returncode == 0, proc.stderr
        outputs.add(proc.stdout.strip())
    assert len(outputs) == 1, outputs


# --- the news matcher ------------------------------------------------------------
def test_true_counterpart_matches_despite_title_boilerplate_and_money() -> None:
    s = settlement(
        "costa-warranty",
        "Costa Del Mar $4.095M Sunglasses Warranty Fee Settlement — Automatic Payments",
    )
    refs = xref.cross_reference_news(
        [s], [item("$4.095M Costa Del Mar sunglasses warranty class action settlement")]
    )
    assert list(refs) == ["costa-warranty"]
    assert refs["costa-warranty"][0]["href"] == "https://topclassactions.com/x/"


def test_two_different_cases_sharing_only_boilerplate_stay_unmatched() -> None:
    s = settlement("guitar-center", "Guitar Center $2.4M Wage and Hour Class Action Settlement")
    refs = xref.cross_reference_news(
        [s], [item("$14.5M CRST Expedited minimum wage class action settlement")]
    )
    assert refs == {}


def test_one_feed_item_backs_at_most_two_settlements() -> None:
    dupes = [
        settlement("d1", "Brand X $1M Widget Fee Settlement"),
        settlement("d2", "Brand X $2M Widget Fee Settlement"),
        settlement("d3", "Brand X $3M Widget Fee Settlement"),
    ]
    refs = xref.cross_reference_news(
        dupes, [item("Brand X widget class action settlement", "https://tca/brand-x/")]
    )
    assert sorted(refs) == ["d1", "d2"], "the third claim exceeds max_per_item"


def test_older_or_higher_scored_matches_are_deterministic() -> None:
    s = settlement("solo", "Pork Price-Fixing Settlement")
    refs = xref.cross_reference_news(
        [s],
        [
            item("Pork price-fixing settlement update", "https://tca/older/"),
            item("Pork price-fixing settlement", "https://tca/main/"),
        ],
    )
    assert [r["href"] for r in refs["solo"]] == ["https://tca/main/", "https://tca/older/"]


# --- the docket verifier ----------------------------------------------------------
DOCKET_RESULT = {
    "caseName": "In re Turkey Antitrust Litigation",
    "case_name_full": "",
    "court_citation_string": "N.D. Ill.",
    "docketNumber": "1:19-cv-08318",
    "docket_absolute_url": "/docket/12345/in-re-turkey/",
}


def test_a_verifying_hit_becomes_a_docket_ref() -> None:
    verified = xref._verify_docket_hit(
        DOCKET_RESULT, xref.match_tokens("In re Turkey Antitrust Litigation")
    )
    assert verified is not None
    assert verified["href"] == "https://www.courtlistener.com/docket/12345/in-re-turkey/"
    assert verified["court"] == "N.D. Ill."


def test_a_near_miss_is_rejected_even_from_the_full_party_list() -> None:
    near_miss = dict(DOCKET_RESULT, caseName="In re Pork Antitrust Litigation")
    assert xref._verify_docket_hit(near_miss, xref.match_tokens("turkey antitrust")) is None
    partial = dict(DOCKET_RESULT, caseName="In re Turkey", case_name_full="")
    assert (
        xref._verify_docket_hit(partial, xref.match_tokens("turkey antitrust litigation")) is None
    )


def test_docket_query_comes_from_the_case_title_not_the_headline() -> None:
    s = settlement(
        "nar",
        "NAR Homebuyer Antitrust Settlement — $120.3M for MLS Home Purchases",
        case_title="Gibson v. National Association of Realtors",
    )
    assert xref.docket_query(s) == "association national realtors gibson"
    assert xref.docket_query(settlement("no-case", "No Case Title Here")) is None


# --- orchestration against a fake client ------------------------------------------
class FakeSession:
    def __init__(self):
        self.headers = {}


class FakeClient:
    """Serves canned responses per host; records everything. No network."""

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[str] = []
        self.session = FakeSession()

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        for fragment, (status, text) in self.responses.items():
            if fragment in url:
                return FetchResult(url=url, status_code=status, text=text)
        raise FetchError(url, message="no canned response")


def test_fetch_docket_refs_verifies_against_the_api() -> None:
    client = FakeClient(
        {"search": (200, json.dumps({"results": [DOCKET_RESULT]}))},
    )
    s = settlement(
        "turkey",
        "Turkey Price-Fixing Settlements — $93.5M",
        case_title="In re Turkey Antitrust Litigation",
    )
    refs = xref.fetch_docket_refs([s], client=client, limit_cases=5)
    assert list(refs) == ["turkey"]
    assert refs["turkey"][0]["docket_number"] == "1:19-cv-08318"
    assert all("search" in url for url in client.calls)
    assert not client.session.headers, "the auth header must be scoped, not leaked"


def test_an_auth_refusal_stops_the_docket_phase() -> None:
    client = FakeClient({"search": (403, "forbidden")})
    s = settlement(
        "turkey", "Turkey Settlements", case_title="In re Turkey Antitrust Litigation"
    )
    lines: list[str] = []
    refs = xref.fetch_docket_refs([s], client=client, limit_cases=5, out=lines.append)
    assert refs == {}
    assert len(client.calls) == 1, "25 dead queries after a 403 help nobody"
    assert any("403" in line for line in lines)


def test_only_claimable_cases_are_queried() -> None:
    client = FakeClient({"search": (200, json.dumps({"results": []}))})
    cases = [
        settlement("investigating", "An Open Investigation", lane="investigation"),
        settlement("claimable", "A Claimable Settlement", case_title="In re Something"),
    ]
    xref.fetch_docket_refs(cases, client=client, limit_cases=5)
    assert len(client.calls) == 1


# --- orchestration: each phase fails on its own, the build never does --------------


def test_a_dead_news_feed_costs_only_the_news_links() -> None:
    """An unreachable feed must not take the docket phase down with it.

    ``FetchError`` is the connection-level failure after retries are
    exhausted - the class of failure an unattended build actually meets.
    """
    client = FakeClient({"search": (200, json.dumps({"results": [DOCKET_RESULT]}))})
    s = settlement(
        "turkey", "Turkey Price-Fixing - $93.5M", case_title="In re Turkey Antitrust Litigation"
    )
    lines: list[str] = []
    out = xref.attach_cross_references([s], client, out=lines.append)

    assert sorted(out[0].cross_refs) == ["docket"]
    assert any("news feed" in line for line in lines)


def test_the_news_phase_can_be_switched_off_without_a_request() -> None:
    client = FakeClient({"search": (200, "{}")})
    s = settlement("turkey", "Turkey Settlements", case_title="In re Turkey Antitrust")
    xref.attach_cross_references([s], client, news_enabled=False)
    assert client.calls and all("search" in url for url in client.calls)


# --- carried-forward docket links ---------------------------------------------------

_DOCKET_REF = {
    "label": "docket",
    "href": "https://www.courtlistener.com/docket/1/webb-v-csx/",
    "source": "courtlistener",
    "title": "Webb v. CSX Transportation, Inc.",
}


def _previous(**overrides) -> dict:
    """A prior dataset record for ``case-a``, with a verified docket link."""
    record = {
        "id": "case-a",
        "case_title": "Webb v. CSX Transportation, Inc.",
        "cross_refs": {"docket": [dict(_DOCKET_REF)]},
    }
    record.update(overrides)
    return {"case-a": record}


def test_a_case_out_of_lookup_scope_keeps_its_verified_docket_link() -> None:
    """Only the top cases by expected value are re-checked each run.

    A case that slides out of that window must not lose the court record it
    already had - it was verified against an unchanged case title, and a docket
    URL does not stop being correct. Losing it would also churn the committed
    dataset, and churn there buries the real diff.
    """
    cases = [settlement("case-a", case_title="Webb v. CSX Transportation, Inc.")]
    assert xref.carry_forward_dockets(cases, _previous()) == {"case-a": [_DOCKET_REF]}


def test_a_changed_case_title_drops_the_carried_link() -> None:
    """The link is the record for *that* proceeding.

    If the page's case title moves, the old hit may describe a different case,
    so the link is dropped rather than carried - it waits for a lookup.
    """
    cases = [settlement("case-a", case_title="Some Other Case v. Someone")]
    assert xref.carry_forward_dockets(cases, _previous()) == {}


def test_carry_forward_is_a_no_op_without_a_previous_build() -> None:
    cases = [settlement("case-a", case_title="Webb v. CSX Transportation, Inc.")]
    assert xref.carry_forward_dockets(cases, None) == {}
    assert xref.carry_forward_dockets(cases, {}) == {}


def test_carry_forward_ignores_a_case_that_was_never_here() -> None:
    cases = [settlement("case-a", case_title="Webb v. CSX Transportation, Inc.")]
    assert xref.carry_forward_dockets(cases, {"unrelated": {"id": "unrelated"}}) == {}


def test_a_fresh_lookup_replaces_the_carried_link() -> None:
    """Carry-over is a fallback, never a cache that outranks a new result."""
    client = FakeClient({"search": (200, json.dumps({"results": [DOCKET_RESULT]}))})
    s = settlement(
        "turkey", "Turkey Price-Fixing - $93.5M", case_title="In re Turkey Antitrust Litigation"
    )
    previous = {
        "turkey": {
            "id": "turkey",
            "case_title": "In re Turkey Antitrust Litigation",
            "cross_refs": {"docket": [_DOCKET_REF]},
        }
    }
    out = xref.attach_cross_references([s], client, previous=previous, news_enabled=False)
    href = out[0].cross_refs["docket"][0]["href"]
    assert href == "https://www.courtlistener.com/docket/12345/in-re-turkey/"
    assert href != _DOCKET_REF["href"]


def test_a_rate_limited_phase_keeps_previously_verified_links() -> None:
    """The failure mode this exists for: CI runs anonymous, so a 429 is real.

    The phase stops on the first refusal - and every link found by an earlier
    run survives it, which is what "degrade, never lose data" has to mean here.
    """
    client = FakeClient({"search": (429, "")})
    s = settlement("case-a", "CSX Derailment", case_title="Webb v. CSX Transportation, Inc.")
    lines: list[str] = []
    out = xref.attach_cross_references(
        [s], client, previous=_previous(), news_enabled=False, out=lines.append
    )
    assert out[0].cross_refs == {"docket": [_DOCKET_REF]}
    assert any("429" in line for line in lines)


def test_apply_merges_both_kinds_and_leaves_the_inputs_alone() -> None:
    matched = settlement("a", "Case A")
    other = settlement("b", "Case B")
    news = {"a": [{"label": "in the news", "href": "https://tca/x/", "source": "topclassactions"}]}
    docket = {"a": [{"label": "docket", "href": "https://cl/d/1/", "source": "courtlistener"}]}

    merged = xref.apply([matched, other], news, docket)

    assert sorted(merged[0].cross_refs) == ["docket", "news"]
    assert merged[1].cross_refs == {}
    assert matched.cross_refs == {} and other.cross_refs == {}, "inputs must not be mutated"


def test_apply_keeps_refs_earlier_sources_attached() -> None:
    # Regression: apply() used to replace the whole cross_refs dict, silently
    # dropping what SettleSignal and ClaimDepot had attached earlier in the
    # same run - measured live as 17 + 19 links vanishing whenever a row also
    # earned a news or docket reference.
    matched = settlement("a", "Case A")
    matched.cross_refs["claimdepot"] = [
        {"label": "cross-checked", "href": "https://claimdepot.com/settlements/x", "source": "claimdepot"}
    ]
    news = {"a": [{"label": "in the news", "href": "https://tca/x/", "source": "topclassactions"}]}

    out = xref.apply([matched], news)

    assert sorted(out[0].cross_refs) == ["claimdepot", "news"]
    assert matched.cross_refs == {"claimdepot": [
        {"label": "cross-checked", "href": "https://claimdepot.com/settlements/x", "source": "claimdepot"}
    ]}, "inputs must not be mutated"


def test_apply_lets_a_fresh_lookup_win_its_own_kind() -> None:
    matched = settlement("a", "Case A")
    matched.cross_refs["docket"] = [{"label": "docket", "href": "https://cl/d/old/", "source": "courtlistener"}]
    fresh = {"a": [{"label": "docket", "href": "https://cl/d/new/", "source": "courtlistener"}]}

    out = xref.apply([matched], fresh)

    assert out[0].cross_refs["docket"][0]["href"] == "https://cl/d/new/"

