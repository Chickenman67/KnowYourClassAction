"""Text hygiene: scraped prose is untrusted input.

One of the sources we read carries injected third-party spam in its content
stream, and another appends referral parameters to every outbound link. These
tests pin the sanitising behaviour we rely on.
"""

from __future__ import annotations

from kya.textutil import clean_text, cleaner_url, collapse_ws, slugify, truncate


def test_collapse_ws_handles_upstream_indentation() -> None:
    # Real shape from the Kia page: the value is indented across two lines.
    raw = "\r\n            Kroll Settlement Administration LLC        "
    assert collapse_ws(raw) == "Kroll Settlement Administration LLC"


def test_collapse_ws_strips_zero_width_characters() -> None:
    assert collapse_ws("Kia\u200b Window\ufeff Regulator") == "Kia Window Regulator"


def test_clean_text_unescapes_entities() -> None:
    assert clean_text("Settlement &amp; Claims Co") == "Settlement & Claims Co"


def test_clean_text_of_none_is_empty() -> None:
    assert clean_text(None) == ""


def test_cleaner_url_removes_tracking_parameters() -> None:
    url = "https://schusterdataincident.com/?utm_source=openclassactions.com"
    assert cleaner_url(url) == "https://schusterdataincident.com/"


def test_cleaner_url_keeps_meaningful_parameters() -> None:
    url = "https://forms.example.com/claim?form-version=1&utm_source=oca&id=42"
    assert cleaner_url(url) == "https://forms.example.com/claim?form-version=1&id=42"


def test_cleaner_url_passthrough_and_empty() -> None:
    assert cleaner_url("https://example.com/x") == "https://example.com/x"
    assert cleaner_url("   ") is None
    assert cleaner_url(None) is None


def test_slugify_is_stable_and_ascii() -> None:
    # The upstream titles use an em dash to separate the case name from the payout.
    title = "Kia Window Regulator Settlement \u2014 Up to $400 per Repair"
    assert slugify(title) == "kia-window-regulator-settlement-up-to-400-per-repair"


def test_slugify_collapses_runs() -> None:
    assert slugify("C$32 for  Former -- CIBC   Fund Holders") == "c-32-for-former-cibc-fund-holders"


def test_truncate_appends_ellipsis_only_when_needed() -> None:
    assert truncate("short") == "short"
    out = truncate("x" * 50, limit=10)
    assert len(out) == 10
    assert out.endswith("\u2026")