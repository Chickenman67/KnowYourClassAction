"""A deliberately polite HTTP client.

Design goals, in priority order:

1. **Never get us blocked.** An identifying ``User-Agent``, serialized requests
   with a crawl delay per host, ``robots.txt`` honoured, and exponential backoff
   with jitter on transient failures. A tracker that gets IP-banned is worthless.
2. **Never lose data.** Every successful response is cached on disk. If the
   network fails on a later run, the stale cache is served instead of the run
   collapsing - a daily job should degrade, not break.
3. **Be testable.** ``sleep``, ``monotonic`` and ``now`` are injectable so the
   test suite never actually waits or touches the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

__all__ = ["PoliteClient", "FetchResult", "FetchError", "RobotsDisallowed"]


class FetchError(RuntimeError):
    """A request failed in a way the caller should know about.

    Both halves are kept when both are known. Reporting only the status threw
    away the useful half in the cases that need it most: a 200 whose body would
    not parse read as a bare ``HTTP 200``, and a 404 lost its status entirely
    and surfaced as "request failed with no response" - which is untrue, and the
    only thing an operator had to go on.
    """

    def __init__(self, url: str, status_code: Optional[int] = None, message: str = ""):
        self.url = url
        self.status_code = status_code
        parts = [f"HTTP {status_code}"] if status_code else []
        if message:
            parts.append(message)
        super().__init__(f"{url}: {': '.join(parts)}".strip().rstrip(":"))


class RobotsDisallowed(FetchError):
    """robots.txt forbids fetching this URL."""


@dataclass
class FetchResult:
    url: str
    status_code: int
    text: str
    from_cache: bool = False
    stale: bool = False
    fetched_at: float = 0.0

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        origin = "cache" if self.from_cache else "net"
        if self.stale:
            origin += "/stale"
        return f"<FetchResult {self.status_code} {origin} {len(self.text)}b {self.url}>"


class PoliteClient:
    """Serialized, cached, robots-aware HTTP client."""

    RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
    DEFAULT_ACCEPT = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "text/plain;q=0.8,application/json;q=0.8,*/*;q=0.5"
    )
    ROBOTS_TTL_SECONDS = 86400

    def __init__(
        self,
        user_agent: str,
        *,
        timeout: float = 25.0,
        min_delay: float = 1.0,
        max_retries: int = 3,
        cache_dir: Path | str | None = None,
        cache_ttl: int = 21600,
        respect_robots: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.min_delay = min_delay
        self.max_retries = max_retries
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl = cache_ttl
        self.respect_robots = respect_robots

        self._sleep = sleep
        self._monotonic = monotonic
        self._now = now
        self._rng = rng or random.Random()

        self._last_request_at: dict[str, float] = {}
        self._robots: dict[str, Optional[RobotFileParser]] = {}
        self.request_count = 0

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": self.DEFAULT_ACCEPT,
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            }
        )

        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        cache_ttl: Optional[int] = None,
        use_cache: bool = True,
        check_robots: Optional[bool] = None,
    ) -> FetchResult:
        """Fetch ``url`` politely, returning a :class:`FetchResult`.

        Raises :class:`FetchError` (or :class:`RobotsDisallowed`) when the URL
        cannot be fetched and no cached copy exists.
        """
        ttl = self.cache_ttl if cache_ttl is None else cache_ttl

        if use_cache:
            cached = self._read_cache(url, ttl=ttl)
            if cached is not None:
                return cached

        host = urlparse(url).netloc
        if self.respect_robots and check_robots is not False and not self._robots_allows(url):
            raise RobotsDisallowed(url, message="disallowed by robots.txt")

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                # exponential backoff with jitter - capped so a daily job cannot stall
                delay = min(30.0, 1.5 * (2 ** (attempt - 1))) + self._rng.uniform(0.0, 0.5)
                self._sleep(delay)
            self._throttle(host)
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:  # network-level failure
                last_error = exc
                continue

            self._last_request_at[host] = self._monotonic()
            self.request_count += 1

            if response.status_code in self.RETRY_STATUSES and attempt < self.max_retries:
                last_error = FetchError(url, response.status_code, response.reason or "")
                continue

            result = FetchResult(
                url=url,
                status_code=response.status_code,
                text=response.text,
                fetched_at=self._now(),
            )
            if result.ok:
                self._write_cache(result)
                return result
            # A final response that is not ok still has a reason worth keeping:
            # without this it fell through as "no response", losing the status.
            last_error = FetchError(url, response.status_code, response.reason or "")
            break

        # Everything failed. Serve a stale cache rather than losing the run.
        stale = self._read_cache(url, ttl=None)
        if stale is not None:
            return replace(stale, stale=True)

        if isinstance(last_error, FetchError):
            raise last_error
        raise FetchError(
            url,
            message=str(last_error) if last_error else "request failed with no response",
        )

    def get_json(self, url: str, **kwargs: Any) -> Any:
        """GET a URL and decode the body as JSON."""
        result = self.get(url, **kwargs)
        try:
            return json.loads(result.text)
        except ValueError as exc:
            raise FetchError(url, result.status_code, f"invalid JSON: {exc}") from exc

    # ------------------------------------------------------------------
    # throttling
    # ------------------------------------------------------------------
    def _throttle(self, host: str) -> None:
        if self.min_delay <= 0:
            return
        last = self._last_request_at.get(host)
        if last is None:
            return
        wait = self.min_delay - (self._monotonic() - last)
        if wait > 0:
            self._sleep(wait)

    # ------------------------------------------------------------------
    # robots.txt
    # ------------------------------------------------------------------
    def _robots_allows(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.netloc
        if host not in self._robots:
            self._robots[host] = self._load_robots(parsed.scheme, host)
        parser = self._robots[host]
        if parser is None:
            return True  # fail open: an unreachable robots.txt must not halt us
        return parser.can_fetch(self.user_agent, url)

    def _load_robots(self, scheme: str, host: str) -> Optional[RobotFileParser]:
        """The host's parsed robots.txt, or ``None`` to fail open.

        ``get`` either returns an ok result or raises, so a status check here
        would be unreachable: an unreachable or refused robots.txt arrives as
        the exception below, and no rules means "allowed".
        """
        robots_url = f"{scheme}://{host}/robots.txt"
        try:
            # check_robots=False prevents infinite recursion
            result = self.get(robots_url, cache_ttl=self.ROBOTS_TTL_SECONDS, check_robots=False)
        except FetchError:
            return None
        parser = RobotFileParser()
        parser.parse(result.text.splitlines())
        return parser

    # ------------------------------------------------------------------
    # disk cache
    # ------------------------------------------------------------------
    def _cache_path(self, url: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, url: str, ttl: Optional[int]) -> Optional[FetchResult]:
        path = self._cache_path(url)
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if payload.get("url") != url:
            return None
        fetched_at = float(payload.get("fetched_at") or 0.0)
        if ttl is not None and (self._now() - fetched_at) > ttl:
            return None
        return FetchResult(
            url=url,
            status_code=int(payload.get("status_code") or 200),
            text=payload.get("text") or "",
            from_cache=True,
            fetched_at=fetched_at,
        )

    def _write_cache(self, result: FetchResult) -> None:
        path = self._cache_path(result.url)
        if path is None:
            return
        payload = {
            "url": result.url,
            "status_code": result.status_code,
            "fetched_at": result.fetched_at,
            "text": result.text,
        }
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)  # atomic - a killed job never leaves a torn cache entry
        except OSError:
            tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()