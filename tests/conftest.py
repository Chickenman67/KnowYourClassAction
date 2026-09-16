"""Shared pytest fixtures.

The fixtures in ``tests/fixtures/`` are real captured payloads, not hand-written
samples. That is deliberate: parsers built against invented HTML pass tests and
fail in production. ``tests/fixtures/MANIFEST.json`` records the URL, timestamp
and digest of every capture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> str:
    """Read a captured fixture as text, failing loudly if it is missing."""
    path = FIXTURE_DIR / name
    if not path.is_file():
        pytest.skip(f"fixture {name} missing - run: python tools/capture_fixtures.py")
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def index_text() -> str:
    return read_fixture("openclassactions_llms.txt")


@pytest.fixture(scope="session")
def index_document(index_text: str):
    from kya.sources.openclassactions_index import parse_index

    return parse_index(index_text, source_url="https://openclassactions.com/llms.txt")


@pytest.fixture(scope="session")
def by_slug(index_document):
    """Look up a captured index entry by a fragment of its URL."""

    def lookup(fragment: str):
        for entry in index_document.entries:
            if fragment in entry.url:
                return entry
        pytest.fail(f"no index entry matching {fragment!r}")

    return lookup


@pytest.fixture(scope="session")
def kia_html() -> str:
    return read_fixture("page_kia_window_regulator.html")


@pytest.fixture(scope="session")
def schuster_html() -> str:
    return read_fixture("page_schuster_data_breach.html")


@pytest.fixture(scope="session")
def cvs_html() -> str:
    return read_fixture("page_cvs_digital_privacy.html")


@pytest.fixture(scope="session")
def equifax_html() -> str:
    return read_fixture("page_equifax_credit_score.html")


@pytest.fixture(scope="session")
def pages() -> dict:
    """Every captured case page, parsed, keyed by slug.

    Session-scoped so site and pipeline tests share one parse of the real
    captures instead of walking the manifest twice.
    """
    from kya.sources.openclassactions_page import parse_page

    manifest = json.loads((FIXTURE_DIR / "MANIFEST.json").read_text(encoding="utf-8"))
    out: dict = {}
    for name, meta in manifest.items():
        if not name.startswith("page_"):
            continue
        result = parse_page(
            (FIXTURE_DIR / f"{name}.html").read_text(encoding="utf-8"), meta["url"]
        )
        page = result[0] if isinstance(result, tuple) else result
        slug = meta["url"].rsplit("/", 1)[-1].replace(".php", "")
        out[slug] = page
    return out


@pytest.fixture(scope="session")
def settlements(index_document, pages):
    """The full dataset built from captured fixtures - index plus pages."""
    from kya.build import build_all

    return build_all(index_document, pages)


@pytest.fixture(scope="session")
def ryobi_html() -> str:
    return read_fixture("page_ryobi_mower_recall.html")