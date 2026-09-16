"""Shared pytest fixtures.

The fixtures in ``tests/fixtures/`` are real captured payloads, not hand-written
samples. That is deliberate: parsers built against invented HTML pass tests and
fail in production. ``tests/fixtures/MANIFEST.json`` records the URL, timestamp
and digest of every capture.
"""

from __future__ import annotations

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
def ryobi_html() -> str:
    return read_fixture("page_ryobi_mower_recall.html")