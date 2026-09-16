"""Small text helpers shared by the parsers.

Upstream text arrives from the web, which means it can contain anything -
including the injected third-party spam we found in one source's content
stream. Nothing from the network is ever trusted as markup.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_WS_RE = re.compile(r"\s+")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
# tracking parameters that upstream helpfully appends to its outbound links
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_EXACT = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source"}


def collapse_ws(value: str | None) -> str:
    """Collapse all whitespace runs to single spaces and trim.

    Upstream HTML indents its values across several lines, so
    ``"\\n            Kroll Settlement Administration LLC        "`` becomes
    ``"Kroll Settlement Administration LLC"``.
    """
    if not value:
        return ""
    return _WS_RE.sub(" ", _ZERO_WIDTH_RE.sub("", value)).strip()


def clean_text(value: str | None) -> str:
    """Collapse whitespace and unescape HTML entities."""
    return collapse_ws(html.unescape(value or ""))


def cleaner_url(url: str | None) -> str | None:
    """Strip tracking parameters (``?utm_source=...``) from an outbound link.

    We link people to the official claim portal, not to a referral-tagged copy
    of it.
    """
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_EXACT
        and not key.lower().startswith(_TRACKING_PREFIXES)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def slugify(value: str) -> str:
    """A stable, URL-safe identifier."""
    value = _ZERO_WIDTH_RE.sub("", value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return re.sub(r"-{2,}", "-", value).strip("-")


def truncate(value: str | None, limit: int = 400) -> str:
    text = collapse_ws(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"