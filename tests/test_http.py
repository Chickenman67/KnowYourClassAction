"""The polite client's failure behaviour, which is the part nothing exercised.

``http.py`` is the module every network call in this project passes through, and
it was the only one with no test file: 29% line coverage against 87% for the
package, with the untested 71% being exactly its interesting logic - the retry
loop, the backoff, the per-host throttle, ``robots.txt``, and the disk cache.

That matters because the module's docstring makes two load-bearing promises and
neither had any regression protection:

1. *"Never get us blocked"* - an identifying User-Agent, a crawl delay per host,
   and ``robots.txt`` honoured.
2. *"Never lose data"* - a failed run serves the stale cache instead of
   collapsing, because "a daily job should degrade, not break".

Both are failure paths, so the happy path (which CI exercises daily against the
real site) proves nothing about them. These tests hold them with no network
access at all: the client already injects ``sleep``, ``monotonic``, ``now`` and
``rng`` for precisely this, so the suite never waits and never connects.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
import requests

from kya.http import FetchError, FetchResult, PoliteClient, RobotsDisallowed


class FakeResponse:
    def __init__(self, status_code: int, text: str = "", reason: str = ""):
        self.status_code = status_code
        self.text = text
        self.reason = reason


class FakeSession:
    """Replays scripted outcomes in order and records what was asked for.

    A script entry is either a :class:`FakeResponse` or an exception instance to
    raise. Running past the end of the script returns an empty 200, so a test
    that under-scripts fails on its assertions rather than on an IndexError.
    """

    def __init__(self, *script):
        self.headers: dict[str, str] = {}
        self.closed = False
        self.calls: list[str] = []
        self._script = list(script)

    def get(self, url: str, timeout=None) -> FakeResponse:
        self.calls.append(url)
        item = self._script.pop(0) if self._script else FakeResponse(200, "")
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class Harness:
    """A client wired to a fake session and a hand-cranked clock."""

    def __init__(self, *, script=(), cache_dir=None, respect_robots=False, **kwargs):
        self.slept: list[float] = []
        self.clock = 1000.0
        self.session = FakeSession(*script)
        self.client = PoliteClient(
            "kya-test/0.1",
            sleep=self.slept.append,
            monotonic=lambda: self.clock,
            now=lambda: self.clock,
            rng=kwargs.pop("rng", None) or random.Random(0),
            cache_dir=cache_dir,
            respect_robots=respect_robots,
            **kwargs,
        )
        self.client.session = self.session

    def advance(self, seconds: float) -> "Harness":
        self.clock += seconds
        return self

    def rescript(self, *script) -> None:
        """Swap the remaining responses, e.g. to make a later call fail."""
        self.session._script = list(script)


PAGE = "https://example.com/settlements/a.php"
ROBOTS = "https://example.com/robots.txt"


# --- retry and backoff -----------------------------------------------------------


def test_a_transient_status_is_retried_and_then_succeeds() -> None:
    """The commonest real failure: a 503 that works on the next try."""
    h = Harness(script=[FakeResponse(503, "unavailable"), FakeResponse(200, "body")])
    result = h.client.get(PAGE)

    assert result.ok and result.text == "body"
    assert len(h.session.calls) == 2
    # Two distinct waits, both intended: a backoff for the failure, plus the
    # crawl delay, because the retry is still another request to that host.
    assert h.slept[0] > 1.0, f"no backoff before the retry: {h.slept}"
    assert h.slept[1] == pytest.approx(1.0), f"the crawl delay was skipped: {h.slept}"


def test_backoff_grows_and_is_capped_so_a_daily_job_cannot_stall() -> None:
    """Exponential with jitter, bounded - an unbounded wait outlives the run.

    The cap is the point: with 8 retries the unclamped curve would reach 192s,
    which is longer than the workflow is willing to spend on one URL.
    min_delay=0 isolates the backoff from the crawl delay.
    """
    h = Harness(script=[FakeResponse(503, "no")] * 9, max_retries=8, min_delay=0)
    with pytest.raises(FetchError):
        h.client.get(PAGE)

    assert len(h.slept) == 8, f"expected one backoff per retry, got {h.slept}"
    assert h.slept[0] < h.slept[1] < h.slept[2], f"backoff is not growing: {h.slept}"
    assert all(delay <= 30.6 for delay in h.slept), f"backoff exceeded its cap: {h.slept}"
    assert h.slept[-1] >= 30.0, "the cap should actually be reached by then"


def test_the_delay_is_jittered_so_runs_do_not_retry_in_lockstep() -> None:
    """Same request, different seeds, different delays."""
    delays = []
    for seed in (1, 2, 3):
        h = Harness(script=[FakeResponse(503, "no"), FakeResponse(200, "ok")], rng=random.Random(seed))
        h.client.get(PAGE)
        delays.append(h.slept[0])
    assert len(set(delays)) > 1, f"no jitter between seeds: {delays}"


def test_a_connection_error_is_retried_rather_than_raised() -> None:
    """Network-level failures are the ones a daily job must ride out."""
    h = Harness(script=[requests.ConnectionError("reset by peer"), FakeResponse(200, "body")])
    result = h.client.get(PAGE)

    assert result.ok and result.text == "body"
    assert len(h.session.calls) == 2


def test_retries_are_exhausted_then_the_failure_is_reported() -> None:
    """Four attempts for max_retries=3, and the status survives in the error."""
    h = Harness(script=[FakeResponse(503, "no")] * 4, max_retries=3, min_delay=0)
    with pytest.raises(FetchError) as excinfo:
        h.client.get(PAGE)

    assert excinfo.value.status_code == 503
    assert len(h.session.calls) == 4
    assert len(h.slept) == 3, "three waits between four attempts"


def test_a_non_retryable_status_fails_immediately() -> None:
    """A 404 will not become a 200, so retrying it only wastes the crawl delay."""
    h = Harness(script=[FakeResponse(404, "gone", "Not Found")], max_retries=3)
    with pytest.raises(FetchError) as excinfo:
        h.client.get(PAGE)

    assert len(h.session.calls) == 1, "a 404 must not be retried"
    # It did respond, so the error has to say so - the status and the reason
    # both survive, rather than the old "request failed with no response".
    assert excinfo.value.status_code == 404
    assert "HTTP 404" in str(excinfo.value)
    assert "Not Found" in str(excinfo.value)


def test_every_request_carries_the_identifying_user_agent() -> None:
    """Promise 1: an anonymous scraper is a scraper that gets blocked."""
    client = PoliteClient("kya-test/0.1 (+https://example.com)")
    try:
        assert client.session.headers["User-Agent"] == "kya-test/0.1 (+https://example.com)"
        assert client.session.headers["Accept"].startswith("text/html")
    finally:
        client.close()


# --- the stale-cache guarantee ---------------------------------------------------


def test_a_failed_run_serves_the_stale_cache_instead_of_collapsing(tmp_path) -> None:
    """Promise 2, and the reason the cache exists at all.

    Today's run finds yesterday's copy expired, the network is dead, and the job
    still produces something - marked stale so a caller can tell the difference.
    """
    h = Harness(script=[FakeResponse(200, "yesterday")], cache_dir=tmp_path, max_retries=0)
    assert h.client.get(PAGE).text == "yesterday"

    h.advance(h.client.cache_ttl + 1)  # the fresh copy is now too old to serve
    h.rescript(FakeResponse(503, "no"))  # and the network has failed
    stale = h.client.get(PAGE)

    assert stale.stale is True
    assert stale.from_cache is True
    assert stale.text == "yesterday", "the stale body must survive, not be emptied"


def test_the_stale_fallback_beats_raising(tmp_path) -> None:
    """A dead network costs freshness, never the run."""
    h = Harness(script=[FakeResponse(200, "cached")], cache_dir=tmp_path, max_retries=0)
    h.client.get(PAGE)
    h.advance(h.client.cache_ttl + 1)
    h.rescript(requests.ConnectionError("down"))

    result = h.client.get(PAGE)

    assert result.ok and result.text == "cached" and result.stale


def test_without_a_cache_a_dead_network_raises(tmp_path) -> None:
    """The other half of the guarantee: there is nothing to fall back to.

    Worth pinning because the callers branch on this - ``run.py`` turns it into
    a failed index fetch, and ``fetch_pages`` into a downgraded entry.
    """
    h = Harness(script=[requests.ConnectionError("down")], cache_dir=tmp_path, max_retries=0)
    with pytest.raises(FetchError):
        h.client.get(PAGE)


# --- the disk cache ---------------------------------------------------------------


def test_a_fresh_entry_is_served_without_touching_the_network(tmp_path) -> None:
    """The 6h TTL is what makes a rebuild cheap and a re-scrape unnecessary."""
    h = Harness(script=[FakeResponse(200, "body")], cache_dir=tmp_path)
    assert h.client.get(PAGE).from_cache is False

    h.rescript(FakeResponse(500, "must not be needed"))
    second = h.client.get(PAGE)

    assert second.from_cache is True and second.text == "body"
    assert len(h.session.calls) == 1, "a fresh entry must short-circuit the request"


def test_an_expired_entry_goes_back_to_the_network(tmp_path) -> None:
    h = Harness(script=[FakeResponse(200, "old")], cache_dir=tmp_path)
    h.client.get(PAGE)

    h.advance(h.client.cache_ttl + 1)
    h.rescript(FakeResponse(200, "new"))
    result = h.client.get(PAGE)

    assert result.text == "new" and result.from_cache is False


def test_use_cache_false_bypasses_a_perfectly_good_entry(tmp_path) -> None:
    """Callers need a way to force a re-read; the robots fetch relies on it."""
    h = Harness(script=[FakeResponse(200, "old")], cache_dir=tmp_path)
    h.client.get(PAGE)

    h.rescript(FakeResponse(200, "fresh"))
    assert h.client.get(PAGE, use_cache=False).text == "fresh"


def test_a_failed_response_is_never_cached(tmp_path) -> None:
    """Caching a 404 would turn a transient miss into a permanent one."""
    h = Harness(script=[FakeResponse(404, "gone")], cache_dir=tmp_path, max_retries=0)
    with pytest.raises(FetchError):
        h.client.get(PAGE)

    assert list(tmp_path.glob("*.json")) == [], "an error body was cached"


def test_an_entry_claims_its_own_url_and_is_ignored_if_it_does_not(tmp_path) -> None:
    """The filename is a hash, so the payload has to prove its identity.

    Without this check a collision - or a cache copied between URLs - would
    serve one case's page as another's, silently.
    """
    h = Harness(cache_dir=tmp_path)
    h.client._cache_path(PAGE).write_text(
        json.dumps({"url": "https://elsewhere.example/x", "status_code": 200, "text": "wrong"}),
        encoding="utf-8",
    )

    assert h.client._read_cache(PAGE, ttl=None) is None


def test_a_torn_cache_file_is_ignored_rather_than_raising(tmp_path) -> None:
    """A half-written file must cost a re-fetch, not the run."""
    h = Harness(cache_dir=tmp_path)
    h.client._cache_path(PAGE).write_text('{"url": "https://exa', encoding="utf-8")

    assert h.client._read_cache(PAGE, ttl=None) is None


def test_the_cache_is_written_atomically_and_leaves_no_temp_file(tmp_path) -> None:
    """A killed job must never leave a torn entry that later reads as valid."""
    h = Harness(script=[FakeResponse(200, "body")], cache_dir=tmp_path)
    h.client.get(PAGE)

    assert h.client._cache_path(PAGE).is_file()
    assert list(tmp_path.glob("*.tmp")) == [], "a temp file was left behind"


def test_a_cache_write_failure_is_swallowed(tmp_path, monkeypatch) -> None:
    """Losing a cache write must not fail a fetch that already succeeded."""
    h = Harness(cache_dir=tmp_path)

    def boom(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "write_text", boom)
    h.client._write_cache(FetchResult(url=PAGE, status_code=200, text="x", fetched_at=1.0))


def test_no_cache_dir_disables_caching_entirely() -> None:
    h = Harness(script=[FakeResponse(200, "body")], cache_dir=None)
    h.client.get(PAGE)

    assert h.client._cache_path(PAGE) is None
    assert h.client._read_cache(PAGE, ttl=None) is None


# --- robots.txt -------------------------------------------------------------------


ROBOTS_ALLOW = "User-agent: *\nDisallow: /private/\n"
ROBOTS_DENY = "User-agent: *\nDisallow: /\n"


def test_a_disallowed_path_is_refused_before_it_is_fetched() -> None:
    """Promise 1 in its most literal form: the page is never requested."""
    h = Harness(
        script=[FakeResponse(200, ROBOTS_DENY), FakeResponse(200, "page")],
        respect_robots=True,
    )

    with pytest.raises(RobotsDisallowed):
        h.client.get(PAGE)

    assert h.session.calls == [ROBOTS], "the disallowed page was fetched anyway"


def test_an_allowed_path_goes_through() -> None:
    h = Harness(
        script=[FakeResponse(200, ROBOTS_ALLOW), FakeResponse(200, "page")],
        respect_robots=True,
    )

    assert h.client.get(PAGE).text == "page"


def test_an_unreachable_robots_txt_fails_open() -> None:
    """Deliberate, and the opposite of what feels safe.

    Halting because a 404'd robots.txt cannot be read would take the tracker
    down for a file that does not exist, so the client proceeds instead.
    """
    h = Harness(
        script=[FakeResponse(404, ""), FakeResponse(200, "page")],
        respect_robots=True,
    )

    assert h.client.get(PAGE).text == "page"


def test_robots_is_read_once_per_host_and_then_reused() -> None:
    """Every case page shares a host; re-reading robots.txt per page is 273 extra requests."""
    other = "https://example.com/settlements/b.php"
    h = Harness(
        script=[FakeResponse(200, ROBOTS_ALLOW), FakeResponse(200, "a"), FakeResponse(200, "b")],
        respect_robots=True,
    )

    h.client.get(PAGE)
    h.client.get(other)

    assert h.session.calls.count(ROBOTS) == 1


def test_respect_robots_false_never_reads_robots_txt() -> None:
    h = Harness(script=[FakeResponse(200, "page")], respect_robots=False)
    h.client.get(PAGE)

    assert ROBOTS not in h.session.calls


# --- the crawl delay --------------------------------------------------------------


OTHER = "https://example.com/settlements/b.php"


def test_a_second_request_to_the_same_host_waits_out_the_crawl_delay() -> None:
    h = Harness(script=[FakeResponse(200, "a"), FakeResponse(200, "b")], min_delay=1.0)
    h.client.get(PAGE)

    h.client.get(OTHER)  # the clock has not moved

    assert h.slept == [1.0], f"expected exactly one full delay, got {h.slept}"


def test_no_wait_is_needed_once_the_delay_has_already_elapsed() -> None:
    """Waiting after the real work already took longer is just dead time."""
    h = Harness(script=[FakeResponse(200, "a"), FakeResponse(200, "b")], min_delay=1.0)
    h.client.get(PAGE)

    h.advance(5)
    h.client.get(OTHER)

    assert h.slept == []


def test_a_different_host_is_not_delayed_by_the_previous_one() -> None:
    """The delay is per host, so a second source does not inherit the first's."""
    h = Harness(script=[FakeResponse(200, "a"), FakeResponse(200, "b")], min_delay=1.0)
    h.client.get(PAGE)

    h.client.get("https://courtlistener.com/api/rest/v4/search/")

    assert h.slept == []


def test_a_zero_delay_disables_throttling() -> None:
    h = Harness(script=[FakeResponse(200, "a"), FakeResponse(200, "b")], min_delay=0)
    h.client.get(PAGE)
    h.client.get(OTHER)

    assert h.slept == []


# --- json, lifecycle, and the result type -----------------------------------------


def test_get_json_decodes_a_valid_body() -> None:
    h = Harness(script=[FakeResponse(200, '{"ok": true, "n": 2}')])

    assert h.client.get_json(PAGE) == {"ok": True, "n": 2}


def test_get_json_reports_invalid_json_as_a_fetch_error() -> None:
    """Callers catch FetchError; a raw ValueError would escape their handling."""
    h = Harness(script=[FakeResponse(200, "not json at all")])

    with pytest.raises(FetchError) as excinfo:
        h.client.get_json(PAGE)

    assert excinfo.value.status_code == 200
    assert "invalid JSON" in str(excinfo.value)


def test_the_context_manager_closes_the_session() -> None:
    h = Harness()

    with h.client as same:
        assert same is h.client

    assert h.session.closed is True


@pytest.mark.parametrize(
    ("status", "expected"),
    [(199, False), (200, True), (204, True), (299, True), (300, False), (404, False), (503, False)],
)
def test_ok_covers_exactly_the_success_range(status: int, expected: bool) -> None:
    """``ok`` gates whether a body is cached and published, so its edges matter."""
    assert FetchResult(url=PAGE, status_code=status, text="").ok is expected


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        (404, "Not Found", "https://x/a: HTTP 404: Not Found"),
        (503, "", "https://x/a: HTTP 503"),
        (None, "connection reset", "https://x/a: connection reset"),
        (200, "invalid JSON: Expecting value", "https://x/a: HTTP 200: invalid JSON: Expecting value"),
    ],
)
def test_a_fetch_error_keeps_both_the_status_and_the_reason(
    status, message, expected
) -> None:
    """Regression: the reason used to be discarded whenever a status was known.

    That made the two hardest failures to diagnose read as ``HTTP 200`` (a
    response that arrived but would not parse) and as "request failed with no
    response" (a 404 that plainly did respond).
    """
    assert str(FetchError("https://x/a", status, message)) == expected


def test_a_fetch_error_with_nothing_to_say_still_names_the_url() -> None:
    assert str(FetchError("https://x/a")) == "https://x/a"
