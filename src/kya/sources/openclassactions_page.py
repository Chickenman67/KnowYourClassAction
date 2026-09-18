"""Parse an ``openclassactions.com`` case page.

Every case page carries **two structured blocks**, which is what makes reliable
extraction possible without regex-guessing at prose:

``<section class="settlement-facts">``
    Labelled quick facts - ``Status``, ``Claim Deadline``, ``Estimated Payout``,
    ``Proof Required``. The label set **varies by page**: a pending case shows
    ``Settlement Fund`` and omits ``Estimated Payout`` entirely, and a recall
    shows ``Recall Remedy`` / ``Units Recalled`` / ``Can I Claim?``. So the block
    is read label-agnostically; nothing is assumed to exist.

``<section class="settlement-case-details">``
    ``Case Title``, ``Case Number``, ``Court``, ``Administrator``,
    ``Official Website``, and page-specific labels such as ``Program`` or
    ``Official Recall Notice``. Values may contain links.

Plus schema.org JSON-LD, which yields the category via ``BreadcrumbList``, the
headline and ``dateModified`` via ``Article``, and eligibility nuance via
``FAQPage`` - all without scraping layout.

The planner is a stdlib ``html.parser`` (no lxml), so the parser has no exotic
dependencies.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dc_field
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..textutil import clean_text, cleaner_url, collapse_ws

__all__ = ["Fact", "CaseDetails", "PageData", "parse_page"]

FACTS_SELECTOR = "section.settlement-facts"
DETAILS_SELECTOR = "section.settlement-case-details"
CLAIM_LINK_SELECTORS = ("a.file-claim", "a[href][class*=claim]", ".file-claim")

# Hosts that publish court records and reporting, and can never accept a claim.
# The "Official Website" detail block sometimes holds one of these instead of a
# settlement site: NZXT's page reads "CourtListener Docket - Burns v. Fragile",
# and the docket URL then became the row's *claim portal* link - sending
# readers somewhere that cannot take their claim.
_RECORD_HOSTS = ("courtlistener.com", "law.justia.com", "casetext.com", "unicourt.com")
_RECORD_PATH_HINTS = ("/docket/", "/opinion/")


def is_court_record_link(url: str | None) -> bool:
    """True for a link to a court record rather than a settlement's own site."""
    if not url:
        return False
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if any(host == name or host.endswith("." + name) for name in _RECORD_HOSTS):
        return True
    path = parts.path.lower()
    return any(hint in path for hint in _RECORD_PATH_HINTS)


@dataclass
class Fact:
    """One labelled quick fact, plus its optional sub-detail line."""

    label: str
    value: str = ""
    sub: str = ""

    def is_known(self) -> bool:
        """``False`` for placeholders such as "Not yet verified"."""
        return bool(self.value) and "not yet" not in self.value.lower()


@dataclass
class CaseDetails:
    """The bottom case-details block."""

    values: dict[str, str] = dc_field(default_factory=dict)
    subs: dict[str, str] = dc_field(default_factory=dict)
    links: dict[str, str] = dc_field(default_factory=dict)

    def get(self, label: str) -> str | None:
        return self.values.get(label)


@dataclass
class PageData:
    url: str
    title: str | None = None
    headline: str | None = None
    description: str | None = None
    category: str | None = None
    breadcrumb: list[str] = dc_field(default_factory=list)
    date_published: str | None = None
    date_modified: str | None = None
    facts: dict[str, Fact] = dc_field(default_factory=dict)
    details: CaseDetails = dc_field(default_factory=CaseDetails)
    claim_url: str | None = None
    faq: list[tuple[str, str]] = dc_field(default_factory=list)
    warnings: list[str] = dc_field(default_factory=list)

    def fact(self, label: str) -> Fact | None:
        return self.facts.get(label)

    def fact_value(self, label: str) -> str | None:
        fact = self.facts.get(label)
        if fact is None or not fact.value:
            return None
        return fact.value

    def can_i_claim(self) -> str | None:
        """The recall/disclaimer answer, when a page provides one."""
        return self.fact_value("Can I Claim?")


def _parse_facts(soup: BeautifulSoup) -> dict[str, Fact]:
    facts: dict[str, Fact] = {}
    section = soup.select_one(FACTS_SELECTOR)
    if section is None:
        return facts
    for block in section.select(".fact"):
        label_el = block.select_one(".fact-label")
        if label_el is None:
            continue
        label = clean_text(label_el.get_text())
        if not label:
            continue
        value_el = block.select_one(".fact-value")
        sub_el = block.select_one(".fact-sub")
        facts[label] = Fact(
            label=label,
            value=clean_text(value_el.get_text()) if value_el else "",
            sub=clean_text(sub_el.get_text()) if sub_el else "",
        )
    return facts


def _parse_details(soup: BeautifulSoup) -> CaseDetails:
    details = CaseDetails()
    section = soup.select_one(DETAILS_SELECTOR)
    if section is None:
        return details
    for block in section.select(".detail"):
        label_el = block.select_one(".detail-label")
        if label_el is None:
            continue
        label = clean_text(label_el.get_text())
        if not label:
            continue
        value_el = block.select_one(".detail-value")
        sub_el = block.select_one(".detail-sub")
        if value_el is not None:
            details.values[label] = clean_text(value_el.get_text())
            link = value_el.find("a", href=True)
            if link is not None:
                cleaned = cleaner_url(link["href"])
                if cleaned:
                    details.links[label] = cleaned
        if sub_el is not None:
            details.subs[label] = clean_text(sub_el.get_text())
    return details


def _iter_jsonld(soup: BeautifulSoup):
    """Yield every JSON-LD node in the page, flattening ``@graph`` blocks."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        queue = [payload]
        while queue:
            node = queue.pop(0)
            if isinstance(node, list):
                queue.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            if "@graph" in node and isinstance(node["@graph"], list):
                queue.extend(node["@graph"])
            yield node


def _jsonld_type(node: dict) -> str:
    kind = node.get("@type")
    if isinstance(kind, list):
        return str(kind[0]) if kind else ""
    return str(kind or "")


def _first_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list) and value:
        return _first_str(value[0])
    return None


def _parse_jsonld(soup: BeautifulSoup) -> dict[str, object]:
    """Pull category, headline, dates and FAQ out of the structured data."""
    result: dict[str, object] = {
        "breadcrumb": [],
        "headline": None,
        "description": None,
        "date_published": None,
        "date_modified": None,
        "faq": [],
    }

    for node in _iter_jsonld(soup):
        kind = _jsonld_type(node)

        if kind == "BreadcrumbList":
            names: list[str] = []
            for item in node.get("itemListElement") or []:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if isinstance(name, str) and name.strip():
                    names.append(clean_text(name))
            if names:
                result["breadcrumb"] = names

        elif kind == "Article":
            result["headline"] = clean_text(node.get("headline")) or None
            result["description"] = clean_text(node.get("description")) or None
            result["date_published"] = _first_str(node.get("datePublished"))
            result["date_modified"] = _first_str(node.get("dateModified"))

        elif kind == "FAQPage":
            pairs: list[tuple[str, str]] = []
            for entry in node.get("mainEntity") or []:
                if not isinstance(entry, dict):
                    continue
                question = clean_text(entry.get("name"))
                answer_node = entry.get("acceptedAnswer") or {}
                answer = ""
                if isinstance(answer_node, dict):
                    answer = clean_text(answer_node.get("text"))
                if question and answer:
                    pairs.append((question, answer))
            if pairs:
                result["faq"] = pairs

    return result


_GENERIC_CRUMBS = frozenset(
    {"settlements", "lawsuits", "home", "class actions", "news", "blog"}
)


def derive_category(breadcrumb: list[str]) -> str | None:
    """The category is the breadcrumb's second-to-last crumb.

    ``Home › Settlements › Data Breaches › Schuster...`` gives ``Data Breaches``
    and ``Home › Class Action Investigations › Ryobi...`` gives
    ``Class Action Investigations``.

    A generic crumb (``Home › Settlements › Kia...``) is not a category, so
    ``None`` is returned rather than showing readers something meaningless.
    """
    if len(breadcrumb) < 3:
        return None
    candidate = breadcrumb[-2].strip()
    if not candidate or candidate.lower() in _GENERIC_CRUMBS:
        return None
    return candidate


def _parse_claim_url(soup: BeautifulSoup, details: CaseDetails) -> str | None:
    """The outbound claim action.

    A claimable page exposes an ``a.file-claim`` pointing at the administrator's
    portal. When it is absent, the official settlement website is a better
    fallback than nothing at all - unless it is not a settlement website. The
    "Official Website" block sometimes carries a court record instead (NZXT:
    "CourtListener Docket - Burns v. Fragile"), and a docket cannot accept a
    claim, so it is refused here rather than presented as the way in.
    """
    for selector in CLAIM_LINK_SELECTORS:
        for link in soup.select(selector):
            href = link.get("href")
            cleaned = cleaner_url(href if isinstance(href, str) else None)
            if cleaned and cleaned.startswith("http"):
                return cleaned
    fallback = details.links.get("Official Website")
    if fallback and not is_court_record_link(fallback):
        return fallback
    return None


def _parse_title(soup: BeautifulSoup) -> str | None:
    header = soup.select_one("h1")
    if header is not None:
        text = clean_text(header.get_text())
        if text:
            return text
    if soup.title is not None:
        text = clean_text(soup.title.get_text())
        if text:
            # The <title> carries the site name after the case name.
            return re.split(r"\s+OpenClassActions", text)[0].strip() or None
    return None


def parse_page(html: str, url: str = "") -> PageData:
    """Parse one case page into structured data.

    Never raises on unexpected markup: a missing block becomes a warning on the
    record rather than an exception, because one changed page must not be able
    to take down a whole daily run.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    page = PageData(url=url)

    page.title = _parse_title(soup)
    page.facts = _parse_facts(soup)
    page.details = _parse_details(soup)

    structured = _parse_jsonld(soup)
    breadcrumb = structured.get("breadcrumb") or []
    page.breadcrumb = [str(name) for name in breadcrumb] if isinstance(breadcrumb, list) else []
    page.headline = _as_optional_str(structured.get("headline"))
    page.description = _as_optional_str(structured.get("description"))
    page.date_published = _as_optional_str(structured.get("date_published"))
    page.date_modified = _as_optional_str(structured.get("date_modified"))
    faq = structured.get("faq") or []
    faq_pairs: list[tuple[str, str]] = []
    if isinstance(faq, list):
        for entry in faq:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                faq_pairs.append((str(entry[0]), str(entry[1])))
    page.faq = faq_pairs
    page.category = derive_category(page.breadcrumb)

    page.claim_url = _parse_claim_url(soup, page.details)

    if not page.facts:
        page.warnings.append("no quick-facts block found")
    if not page.title:
        page.warnings.append("no title found")

    return page


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def find_faq_answer(page: PageData, *keywords: str) -> str | None:
    """First FAQ answer whose question mentions every keyword."""
    lowered = [keyword.lower() for keyword in keywords]
    for question, answer in page.faq:
        haystack = question.lower()
        if all(keyword in haystack for keyword in lowered):
            return answer
    return None