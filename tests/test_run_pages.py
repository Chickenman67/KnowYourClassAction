"""The page-fetch loop must degrade per entry, never abort the run.

The first scheduled CI run died at page ~250/269 because one connection-level
failure raised out of the loop; these tests hold the line.
"""

from __future__ import annotations

from kya.http import FetchError, FetchResult
from kya.run import fetch_pages


class Entry:
    def __init__(self, slug: str, url: str):
        self.slug = slug
        self.url = url


class FlakyClient:
    """Succeeds everywhere except one page, where retries were exhausted."""

    def __init__(self, bad_url: str):
        self.bad_url = bad_url
        self.fetched: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.fetched.append(url)
        if url == self.bad_url:
            raise FetchError(url, message="request failed with no response")
        return FetchResult(url=url, status_code=200, text="<html>stub page</html>")


def test_one_dead_page_costs_one_entry_never_the_run() -> None:
    entries = [Entry(f"s{i}", f"https://example.com/{i}.php") for i in range(5)]
    client = FlakyClient("https://example.com/3.php")
    lines: list[str] = []
    pages = fetch_pages(client, entries, out=lines.append)
    assert len(client.fetched) == 5, "a raised FetchError must not abort the loop"
    assert set(pages) == {"s0", "s1", "s2", "s4"}
    assert any("s3" in line for line in lines)


def test_an_http_error_page_is_skipped_with_a_warning() -> None:
    class ServerError:
        def get(self, url: str) -> FetchResult:
            return FetchResult(url=url, status_code=503, text="")

    lines: list[str] = []
    pages = fetch_pages(ServerError(), [Entry("a", "https://x/a.php")], out=lines.append)
    assert pages == {}
    assert any("503" in line for line in lines)


def test_progress_still_reports_every_25_pages() -> None:
    entries = [Entry(f"s{i:02d}", f"https://example.com/{i}.php") for i in range(30)]
    lines: list[str] = []
    fetch_pages(FlakyClient("https://example.com/404.php"), entries, out=lines.append)
    assert any("25/30" in line for line in lines)
