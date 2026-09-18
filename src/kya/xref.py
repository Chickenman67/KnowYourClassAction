"""Cross-references from secondary sources.

Two phases, both additive and never load-bearing - a secondary source that is
down must cost nothing, and a wrong link is worse than no link:

* **topclassactions.com RSS** ("in the news"): feed items are matched to
  settlements by conservative title similarity, giving readers a second
  outlet's coverage of the same case. The feed carries 100 items - about three
  weeks - so the parser's cap covers all of it.
* **CourtListener docket search** ("docket"): an independent, court-side
  verification link for the highest-value claimable cases. Anonymous access
  works; a ``KYA_COURTLISTENER_TOKEN`` raises the rate limit. Because only that
  window is re-checked each run and it is ranked by expected value, a verified
  link is carried forward for any case that slid out of it - see
  :func:`carry_forward_dockets`.

Matching is token-based Jaccard with stopwords removed, because scraped titles
embed dollar figures ("$9.37M Concora Credit TCPA...") that must not decide a
match. Every finder returns ``{}`` on any failure, so the build proceeds with
unreferenced settlements rather than dying to a secondary source.
"""

from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence
from urllib.parse import quote

from kya.http import FetchError, FetchResult, PoliteClient
from kya.models import Settlement

TCA_FEED_URL = "https://topclassactions.com/feed/"
COURTLISTENER_SEARCH_URL = "https://www.courtlistener.com/api/rest/v4/search/"

NEWS_THRESHOLD = 0.5
DOCKET_VERIFY_MIN_TOKENS = 2
MAX_NEWS_PER_SETTLEMENT = 2

# Tokens that appear in nearly every title on both sides - useless for
# matching. The domain-boilerplate half (fee/breach/automatic...) came from a
# live probe: openclassactions appends phrases like "- Automatic Payments" to
# otherwise distinctive titles, and without these the longest-token heuristic
# keys on the boilerplate instead of the brand.
GENERIC_TOKENS = frozenset(
    {
        "class", "action", "settlement", "lawsuit", "case", "court", "claim",
        "filed", "new", "automatic", "payment", "payments", "fee", "fees",
        "data", "breach", "privacy", "pixel", "tracking", "wage", "hour",
        "minimum", "price", "fixing", "consumers", "customers", "eligible",
        "payout", "deadline", "litigation", "plaintiff", "plaintiffs",
        "defendant", "defendants",
    }
)

STOPWORDS = frozenset(
    """a an and are as at be by for from has have in into is it its of on or
    that the their there these this to was were will with your you""".split()
)


def title_tokens(title: str) -> frozenset[str]:
    """Lowercase word tokens worth matching on (length > 2, not a stopword).

    Hyphens, apostrophes and ``#`` are folded out first, so "non-bank" and
    "nonbank" (or "VSL#3" and "VSL 3") tokenize the same on both sides.
    """
    folded = re.sub(r"[-'#\u2019]", "", title.lower())
    words = re.findall(r"[a-z0-9]+", folded)
    return frozenset(w for w in words if len(w) > 2 and w not in STOPWORDS)


def match_tokens(title: str) -> frozenset[str]:
    """The tokens two counterpart titles can be expected to share.

    ``title_tokens`` minus :data:`GENERIC_TOKENS` - similarity is computed
    over these, so legal boilerplate ("class action settlement") and domain
    boilerplate ("data breach", "automatic payments") cannot inflate a score.
    """
    return title_tokens(title) - GENERIC_TOKENS


def similarity(a: str, b: str) -> float:
    """Jaccard similarity of two titles' match tokens; 0.0 when either is empty."""
    ta, tb = match_tokens(a), match_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _distinctive_tokens(title: str, count: int = 3) -> frozenset[str]:
    """The longest non-generic tokens - brand names and unique case words.

    These are what a true counterpart title must repeat. Generic legal
    vocabulary ("class action settlement") matches everything, and dollar
    figures match only their own case, so they are exactly the wrong things
    to rank a match by.

    Length ties break alphabetically, and that is load-bearing:
    :func:`match_tokens` returns a frozenset, whose iteration order follows the
    interpreter's hash seed, so ``key=len`` alone would select a different
    subset in a different process - and the site and the digest would disagree
    about which news items match.
    """
    tokens = sorted(match_tokens(title), key=lambda t: (-len(t), t))
    return frozenset(tokens[:count])


# --------------------------------------------------------------------------
# Phase 1: the topclassactions.com RSS feed
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    published: str


def _clean(value: str) -> str:
    value = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", value, flags=re.S)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value).strip()


def parse_feed(xml_text: str, *, limit: int = 100) -> list[FeedItem]:
    """RSS 2.0 items, title + link + pubDate.

    Regex-based on purpose: WordPress titles routinely carry named HTML
    entities (``&rsquo;``) that are illegal in strict XML and would make a
    namespace-aware parser reject the entire feed over one apostrophe. An
    unparseable or truncated feed yields ``[]`` - never an exception.
    """
    items: list[FeedItem] = []
    for block in re.findall(r"<item>(.*?)</item>", xml_text, re.S):
        title = re.search(r"<title>(.*?)</title>", block, re.S)
        link = re.search(r"<link>(.*?)</link>", block, re.S)
        pub_date = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
        if not (title and link):
            continue
        clean_title = _clean(title.group(1))
        clean_link = _clean(link.group(1))
        if clean_title and clean_link:
            items.append(
                FeedItem(
                    title=clean_title,
                    link=clean_link,
                    published=_clean(pub_date.group(1)) if pub_date else "",
                )
            )
        if len(items) >= limit:
            break
    return items


def cross_reference_news(
    settlements: Sequence[Settlement],
    items: Sequence[FeedItem],
    *,
    threshold: float = NEWS_THRESHOLD,
    max_per_item: int = MAX_NEWS_PER_SETTLEMENT,
) -> dict[str, list[dict[str, str]]]:
    """``{settlement_id: [news refs]}`` for titles similar enough to trust.

    Two gates, both required: match-token Jaccard at or above ``threshold``
    (0.5, computed over non-generic tokens so boilerplate cannot inflate it),
    and at least two of the settlement's distinctive tokens repeated in the
    feed title. The second gate is what separates "$4.095M Costa Del Mar
    sunglasses warranty..." (a true counterpart) from "Guitar Center
    wage-and-hour" vs "CRST Expedited minimum wage" (two different cases that
    share only legal boilerplate) - live-run evidence, not a guess.
    """
    scored: list[tuple[float, str, Settlement, FeedItem]] = []
    for item in items:
        item_tokens = match_tokens(item.title)
        for settlement in settlements:
            score = similarity(settlement.title, item.title)
            if score < threshold:
                continue
            distinctive = _distinctive_tokens(settlement.title)
            needed = min(2, len(distinctive))
            if len(distinctive & item_tokens) < needed:
                continue
            scored.append((score, settlement.id, settlement, item))
    scored.sort(key=lambda t: (-t[0], t[1]))

    refs: dict[str, list[dict[str, str]]] = {}
    used: dict[str, int] = {}
    for _, settlement_id, settlement, item in scored:
        if used.get(item.link, 0) >= max_per_item:
            continue
        entry = {
            "label": "in the news",
            "href": item.link,
            "source": "topclassactions",
            "title": item.title,
            "date": item.published,
        }
        bucket = refs.setdefault(settlement_id, [])
        if not any(existing["href"] == entry["href"] for existing in bucket):
            bucket.append(entry)
            used[item.link] = used.get(item.link, 0) + 1
    return refs


# --------------------------------------------------------------------------
# Phase 2: CourtListener docket search (independent, court-side)
# --------------------------------------------------------------------------


def docket_query(settlement: Settlement) -> str | None:
    """A discriminating search query from the enriched case title.

    Scraped settlement titles describe ("$117M Pork Price-Fixing..."), but
    dockets are named ("In re Pork Antitrust Litigation"), so the query is
    built from the case title the page enrichment captured - and without one
    there is nothing a docket search could verify. Generic vocabulary is
    already excluded by ``match_tokens``; the remaining tokens double as the
    verification key for the result.
    """
    source = settlement.case_title or ""
    # Longest first, ties alphabetical: the query must be byte-identical
    # across runs, or the same case would look changed to the digest and the
    # polite client's cache would never hit.
    tokens = sorted(match_tokens(source), key=lambda t: (-len(t), t))
    if not tokens:
        return None
    return " ".join(tokens[:4])


def _parse_docket_results(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    results = data.get("results") if isinstance(data, dict) else None
    return results if isinstance(results, list) else []


def _verify_docket_hit(result: dict, query_tokens: frozenset[str]) -> dict | None:
    """The search result as a docket ref, or ``None`` when it does not verify.

    Search engines return plausible near-misses, so the top hit must repeat
    at least two of the query's tokens in its case name (either the short
    ``caseName`` or the full party list) - and the fallback is no link rather
    than a wrong court record.
    """
    case_name = result.get("caseName") or ""
    case_full = result.get("case_name_full") or ""
    haystack = title_tokens(f"{case_name} {case_full}")
    if len(haystack & query_tokens) < min(DOCKET_VERIFY_MIN_TOKENS, len(query_tokens)):
        return None
    path = result.get("docket_absolute_url") or ""
    if not path:
        return None
    return {
        "label": "docket",
        "href": "https://www.courtlistener.com" + path,
        "source": "courtlistener",
        "title": case_name or case_full,
        "court": result.get("court_citation_string") or result.get("court") or "",
        "docket_number": result.get("docketNumber") or "",
    }


def fetch_docket_refs(
    settlements: Sequence[Settlement],
    *,
    client: PoliteClient,
    limit_cases: int = 25,
    token: str | None = None,
    out: Callable[[str], None] = print,
) -> dict[str, list[dict[str, str]]]:
    """A docket link for the top claimable cases whose hit verifies.

    The first search result must repeat at least two of the query's tokens in
    its case name - search engines return plausible near-misses, and the
    fallback is no link rather than a wrong court record. The first network
    or auth failure stops the phase: if the API is down, 25 dead queries help
    nobody.
    """
    token = token or (os.environ.get("KYA_COURTLISTENER_TOKEN") or "").strip() or None
    headers = {"Authorization": f"Token {token}"} if token else {}

    targets = sorted(
        (s for s in settlements if str(s.lane) == "claimable"),
        key=lambda s: s.ev_estimate or 0.0,
        reverse=True,
    )[:limit_cases]

    # The polite client serializes its own session, so a scoped header set is
    # safe; restore it so later calls (robots, cache) carry no auth header.
    if headers:
        client.session.headers.update(headers)
    try:
        refs = _docket_loop(targets, client=client, out=out)
    finally:
        if headers:
            for key in headers:
                client.session.headers.pop(key, None)
    return refs


def _docket_loop(
    targets: Sequence[Settlement],
    *,
    client: PoliteClient,
    out: Callable[[str], None] = print,
) -> dict[str, list[dict[str, str]]]:
    refs: dict[str, list[dict[str, str]]] = {}
    for settlement in targets:
        query_tokens = title_tokens(docket_query(settlement) or "")
        if not query_tokens:
            continue
        url = f"{COURTLISTENER_SEARCH_URL}?q={quote(' '.join(sorted(query_tokens)))}&type=r"
        try:
            result: FetchResult = client.get(url)
        except FetchError:
            out("  courtlistener: unreachable - skipping remaining docket lookups")
            break
        if result.status_code in (401, 403, 429):
            out(f"  courtlistener: HTTP {result.status_code} - skipping remaining docket lookups")
            break
        if not result.ok:
            continue
        results = _parse_docket_results(result.text)
        verified = None
        for candidate in results:
            verified = _verify_docket_hit(candidate, query_tokens)
            if verified:
                break
        if verified is None:
            continue
        refs[settlement.id] = [verified]
    return refs


# --------------------------------------------------------------------------
# Application + orchestration
# --------------------------------------------------------------------------


def apply(
    settlements: Sequence[Settlement],
    *ref_maps: Mapping[str, list[dict[str, str]]],
) -> list[Settlement]:
    """Copy the settlements, attaching cross-references where they exist."""
    merged: dict[str, dict[str, list[dict[str, str]]]] = {}
    for ref_map in ref_maps:
        for settlement_id, refs in ref_map.items():
            kind = "docket" if refs and refs[0].get("source") == "courtlistener" else "news"
            merged.setdefault(settlement_id, {})[kind] = list(refs)
    return [
        s.model_copy(update={"cross_refs": merged[s.id]}) if s.id in merged else s
        for s in settlements
    ]


def carry_forward_dockets(
    settlements: Sequence[Settlement],
    previous: Mapping[str, Mapping] | None,
) -> dict[str, list[dict[str, str]]]:
    """Docket links already verified, kept for cases this run did not re-check.

    Only the top cases by expected value are looked up each run, and that
    ranking moves whenever an estimate is revised - so a case can slide out of
    scope and silently lose a court record it already had. A verified docket
    URL does not stop being correct, so it is carried forward instead.

    The carry is scoped to cases whose case title is unchanged: the link is the
    record for *that* proceeding, so a title that moved means the old hit may
    describe a different case, and the link waits for a lookup to re-establish
    it. ``previous`` is ``{id: record}`` from the last published dataset.
    """
    if not previous:
        return {}
    carried: dict[str, list[dict[str, str]]] = {}
    for settlement in settlements:
        old = previous.get(settlement.id)
        if not isinstance(old, dict):
            continue
        if (old.get("case_title") or None) != (settlement.case_title or None):
            continue
        refs = (old.get("cross_refs") or {}).get("docket")
        if isinstance(refs, list) and refs and isinstance(refs[0], dict):
            carried[settlement.id] = [dict(ref) for ref in refs if isinstance(ref, dict)]
    return carried


def attach_cross_references(
    settlements: Sequence[Settlement],
    client: PoliteClient,
    *,
    news_enabled: bool = True,
    docket_top_cases: int = 25,
    previous: Mapping[str, Mapping] | None = None,
    out: Callable[[str], None] = print,
) -> list[Settlement]:
    """Both secondary-source phases, each degrading independently to a no-op."""
    news_refs: dict[str, list[dict[str, str]]] = {}
    items: list[FeedItem] = []
    if news_enabled:
        try:
            feed_result = client.get(TCA_FEED_URL)
            if feed_result.ok:
                items = parse_feed(feed_result.text)
                out(f"  news feed: {len(items)} items")
            else:
                out(f"  news feed: HTTP {feed_result.status_code} - continuing without it")
        except (FetchError, OSError) as exc:
            out(f"  news feed: {exc} - continuing without it")
        try:
            news_refs = cross_reference_news(settlements, items)
        except Exception as exc:  # noqa: BLE001 - a matching bug must not sink a build
            out(f"  news matching failed: {exc}")
            news_refs = {}

    docket_refs: dict[str, list[dict[str, str]]] = {}
    try:
        docket_refs = fetch_docket_refs(
            settlements, client=client, limit_cases=docket_top_cases, out=out
        )
    except Exception as exc:  # noqa: BLE001
        out(f"  docket lookups failed: {exc}")
        docket_refs = {}

    # A fresh lookup wins: carry-over exists only for cases this run did not
    # re-resolve, so it is filtered down to those before it is applied.
    carried = {
        settlement_id: refs
        for settlement_id, refs in carry_forward_dockets(settlements, previous).items()
        if settlement_id not in docket_refs
    }

    enriched = apply(settlements, news_refs, carried, docket_refs)
    out(
        f"xref: {len(news_refs)} news link(s), {len(docket_refs)} docket link(s)"
        + (f", {len(carried)} carried forward" if carried else "")
    )
    return enriched