"""Parse ``openclassactions.com/llms.txt`` - the machine-readable index.

The file is published specifically for machine consumption (its ``robots.txt``
declares an ``LLM-Content`` directive pointing at it) and is regenerated on
every request, so expired claim windows are already filtered out. That makes it
the cheapest possible daily signal: one request, ~260 entries, each already
carrying a deadline and a proof requirement.

Each line looks like::

    - [Kia Window Regulator Settlement — Up to $400 per Repair](https://...):
      Deadline: November 23, 2026 · Documentation required

Everything after the colon is a ``·``-separated field list. **The fields are not
positional**, and that is the trap:

* the trailing field is sometimes the *proof* requirement
  (``Documentation required``)
* sometimes the *geographic scope* (``CA residents``)
* and sometimes both, in either order::

      ... Deadline: October 8, 2026 · ID/code from notice required · CA residents
      ... Deadline: October 5, 2026 · Automatic payment — no claim form · WA residents

Reading "the last field" as the proof requirement would mislabel roughly 55 of
the 260 entries. Fields are therefore classified by content, and the proof
whitelist is matched **first** so that ``ID/code from notice required`` is never
mistaken for Idaho.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from urllib.parse import urlparse

from ..models import GeoScope, ProofLevel
from ..textutil import clean_text, collapse_ws, slugify

__all__ = ["IndexEntry", "IndexDocument", "parse_index", "split_title", "parse_geo"]

MIDDOT = "\u00b7"
EM_DASH = "\u2014"
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"

ENTRY_RE = re.compile(
    r"^\s*[-*]\s*\[(?P<title>.+?)\]\((?P<url>https?://[^\s)]+)\)\s*:\s*(?P<meta>.*)$"
)
SECTION_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$")
LAST_UPDATED_RE = re.compile(r"^Last updated:\s*(?P<value>\d{4}-\d{2}-\d{2})\s*$", re.M)
DEADLINE_PREFIX_RE = re.compile(r"^deadline\s*:\s*", re.I)

SECTION_OPEN = "Open Settlements"
SECTION_UNVERIFIED = "Recent Settlements and Lawsuits"

STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)

STATE_NAMES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

# City/abbreviation aliases that are not two-letter USPS codes.
CITY_ALIASES: dict[str, str] = {"nyc": "NY"}

# Words that make a segment look like an eligibility selector rather than prose.
GEO_SUFFIX_RE = re.compile(
    r"\b(residents?|applicants?|job applicants?|workers?|employees?|purchases?|buyers?|"
    r"sellers?|patients?|borrowers?|policyholders?|homeowners?|clubs?|members?|customers?|"
    r"retirees?|institutions?|detainees?|holders?|businesses?|farms?|parents?|students?|"
    r"record requests?|appointments?|custody|detentions?|claims?|only|statewide)\b",
    re.I,
)

# A field that describes what you must *provide* is never a place selector.
# Without this, "Class Member ID Required" matches the "member" selector word,
# finds the token "ID", and becomes a filter for Idaho.
_PROOFISH_RE = re.compile(
    r"\b(required|requires?|receipts?|documents?|documentation|proof|statement|"
    r"attestation|declaration|pin)\b",
    re.I,
)

PROOF_LABELS: dict[str, str] = {
    "automatic payment - no claim form": ProofLevel.L0.value,
    "no proof required": ProofLevel.L1.value,
    "id/code from notice required": ProofLevel.L2.value,
    "class member id required": ProofLevel.L2.value,
    "notice id required": ProofLevel.L2.value,
    "documentation required": ProofLevel.L3.value,
    "model number required": ProofLevel.L3.value,
    "marked-product photo, model number and batch code required": ProofLevel.L3.value,
}

# These are not proof requirements: they are invitations to join an
# investigation, where no money is claimable yet.
INVESTIGATION_LABELS = (
    "no proof required to request a free case review",
    "free case review",
)

def normalize_label(value: str | None) -> str:
    """Lower-case and canonicalise a field so whitelist matching is reliable."""
    text = clean_text(value).lower()
    for dash in _DASHES:
        text = text.replace(dash, "-")
    text = text.replace("\u00a0", " ")
    return collapse_ws(text).rstrip(".")


_TITLE_SPLIT_RE = re.compile(rf"\s+(?:[{_DASHES}]|-)\s+")


def split_title(title: str) -> tuple[str, str | None]:
    """Split ``"<Case name> — <payout phrase>"`` into its two halves.

    Roughly a quarter of entries carry no payout phrase at all, in which case
    the phrase is ``None`` and the payout has to come from the detail page.
    ASCII hyphens only split when surrounded by spaces, so
    ``"Above-Ground Pool"`` is left intact.
    """
    text = collapse_ws(title)
    match = _TITLE_SPLIT_RE.search(text)
    if not match:
        return text, None
    name = text[: match.start()].strip()
    phrase = text[match.end() :].strip()
    if not name or not phrase:
        return text, None
    return name, phrase


def guess_proof_level(value: str | None) -> str | None:
    """Best-effort proof level for labels outside the published whitelist.

    Used for the handful of long free-text fields upstream sometimes emits
    (for example a payment-election description that mentions a Notice ID and
    PIN). Returns ``None`` rather than a default, because inventing a proof
    requirement is worse than admitting we do not know one.
    """
    low = normalize_label(value)
    if not low:
        return None
    if any(token in low for token in ("notice id", "id/code", "id from notice",
                                      "loginid", "claim id", "member id",
                                      " and pin", "pin from", "pin ")):
        return ProofLevel.L2.value
    if any(token in low for token in ("document", "receipt", "proof of purchase",
                                      "batch code", "records required")):
        return ProofLevel.L3.value
    if low.startswith("no proof") or "no documentation" in low:
        return ProofLevel.L1.value
    if "automatic" in low:
        return ProofLevel.L0.value
    return None


_CONNECTOR_WORDS = frozenset({"and", "or"})


def _is_pure_code_list(text: str) -> bool:
    """True for a bare selector such as ``"CA & CO"``, false for prose."""
    tokens = re.findall(r"[A-Za-z]+", text)
    if not tokens:
        return False
    return all(
        token.upper() in STATE_CODES or token.lower() in _CONNECTOR_WORDS
        for token in tokens
    )


def parse_geo(value: str | None) -> GeoScope | None:
    """Interpret a field as a geographic restriction, or ``None`` if it is not one.

    The hard part is that several USPS codes are ordinary English words. ``ID``
    is Idaho *and* the word "ID", so ``Class Member ID Required`` and
    ``ID/code from notice required`` both contain a two-letter token that looks
    exactly like a state.

    A segment therefore only counts as geography when it carries a recognisable
    selector word (``"... residents"``, ``"... purchases only"``), names a state
    in full, or is a bare list of codes - never merely because a two-letter token
    appears somewhere inside it.
    """
    text = collapse_ws(value)
    if not text:
        return None
    low = text.lower()

    if "canada" in low or "canadian" in low:
        return GeoScope(scope_type="country", country="CA", label=text)

    if _PROOFISH_RE.search(text):
        return None

    named = {
        code
        for name, code in STATE_NAMES.items()
        if re.search(rf"\b{re.escape(name)}\b", low)
    }
    for alias, code in CITY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", low):
            named.add(code)

    codes = {code for code in re.findall(r"\b([A-Z]{2})\b", text) if code in STATE_CODES}
    found = sorted(named | codes)
    if not found:
        return None

    selector_word = bool(GEO_SUFFIX_RE.search(text))
    if not (selector_word or named or _is_pure_code_list(text)):
        # A two-letter token inside a label is an acronym, not a state.
        return None

    if not selector_word and len(text) > 70:
        # Prose that happens to mention a state is not a precise enough filter,
        # so the original wording is kept instead of a guessed state list.
        return GeoScope(scope_type="other", label=text)

    return GeoScope(scope_type="states", states=found, label=text)


def slug_from_url(url: str) -> str:
    """Stable identifier taken from the URL path, e.g. ``kia-window-regulator-...``."""
    path = urlparse(url).path.rstrip("/")
    name = path.split("/")[-1] if path else ""
    name = re.sub(r"\.(php|html?|aspx)$", "", name, flags=re.I)
    return name or slugify(url)


@dataclass
class IndexEntry:
    """One raw line from the published index, before enrichment."""

    url: str
    title: str
    section: str  # "open" | "unverified"
    case_name: str = ""
    payout_phrase: str | None = None
    deadline_raw: str = ""
    proof_label_raw: str | None = None
    proof_level: str | None = None
    geo: GeoScope = dc_field(default_factory=GeoScope)
    notes: list[str] = dc_field(default_factory=list)
    is_investigation: bool = False

    @property
    def slug(self) -> str:
        return slug_from_url(self.url)

    @property
    def is_pending(self) -> bool:
        return self.section == "unverified"


@dataclass
class IndexDocument:
    entries: list[IndexEntry] = dc_field(default_factory=list)
    last_updated: str | None = None
    source_url: str = ""

    def __len__(self) -> int:
        return len(self.entries)

    def open_entries(self) -> list[IndexEntry]:
        return [entry for entry in self.entries if entry.section == "open"]

    def unverified_entries(self) -> list[IndexEntry]:
        return [entry for entry in self.entries if entry.section == "unverified"]


def classify_segment(segment: str) -> tuple[str, object]:
    """Decide what one ``·``-separated field actually is.

    Proof requirements are matched **before** geography so that
    ``ID/code from notice required`` is never read as Idaho, and ``No proof
    required to request a free case review`` is never read as a claimable
    no-proof settlement.
    """
    label = normalize_label(segment)
    if not label:
        return "skip", None

    for marker in INVESTIGATION_LABELS:
        if marker in label:
            return "investigation", collapse_ws(segment)

    level = PROOF_LABELS.get(label)
    if level is not None:
        return "proof", (collapse_ws(segment), level)

    guessed = guess_proof_level(segment)
    if guessed is not None:
        return "inferred_proof", (collapse_ws(segment), guessed)

    geo = parse_geo(segment)
    if geo is not None:
        return "geo", geo

    return "note", collapse_ws(segment)


def _section_key(heading: str) -> str | None:
    if heading.startswith(SECTION_OPEN):
        return "open"
    if heading.startswith(SECTION_UNVERIFIED):
        return "unverified"
    return None


def parse_index(text: str, *, source_url: str = "") -> IndexDocument:
    """Parse the whole ``llms.txt`` payload into an :class:`IndexDocument`."""
    document = IndexDocument(source_url=source_url)
    section: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        heading = SECTION_RE.match(line)
        if heading:
            section = _section_key(heading.group("name").strip())
            continue
        if section is None:
            continue

        if section == "open" and document.last_updated is None:
            stamp = LAST_UPDATED_RE.search(line)
            if stamp:
                document.last_updated = stamp.group("value")
                continue

        entry = ENTRY_RE.match(line)
        if not entry:
            continue
        document.entries.append(
            _parse_entry(
                title=entry.group("title"),
                url=entry.group("url"),
                meta=entry.group("meta"),
                section=section,
            )
        )

    return document


def _parse_entry(*, title: str, url: str, meta: str, section: str) -> IndexEntry:
    case_name, payout_phrase = split_title(title)
    entry = IndexEntry(
        url=url,
        title=clean_text(title),
        section=section,
        case_name=clean_text(case_name),
        payout_phrase=clean_text(payout_phrase) if payout_phrase else None,
    )

    segments = [part for part in (chunk.strip() for chunk in meta.split(MIDDOT)) if part]

    for segment in segments:
        deadline_match = DEADLINE_PREFIX_RE.match(segment)
        if deadline_match:
            entry.deadline_raw = collapse_ws(segment[deadline_match.end():])
            continue

        kind, payload = classify_segment(segment)
        if kind == "skip":
            continue
        if kind == "investigation":
            entry.is_investigation = True
            entry.notes.append(str(payload))
        elif kind == "proof":
            label, level = payload  # type: ignore[misc]
            entry.proof_label_raw = label
            entry.proof_level = level
        elif kind == "inferred_proof":
            label, level = payload  # type: ignore[misc]
            entry.proof_label_raw = label
            entry.proof_level = level
            entry.notes.append("proof requirement inferred from free text")
        elif kind == "geo":
            entry.geo = payload  # type: ignore[assignment]
        else:
            entry.notes.append(str(payload))

    if not entry.deadline_raw:
        joined = " ".join(segments).lower()
        if "not yet verified" in joined or "pending final approval" in joined:
            entry.deadline_raw = "Not yet verified"

    return entry