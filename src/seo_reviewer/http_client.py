"""Polite HTTP client used by every collector that talks to the target site.

- Identifiable User-Agent from config (crawler.user_agent).
- Rate limit: at most crawler.requests_per_second requests, across all hosts.
- Timeout on every request (crawler.timeout_seconds).
- Retries (crawler.max_retries, default 1) on connection errors, timeouts,
  HTTP 429 and 5xx, with a short backoff.
- robots.txt enforcement via urllib.robotparser: before fetching a URL the
  client loads robots.txt for that origin once and refuses disallowed URLs
  by raising RobotsDisallowed.

Clock and sleep are injectable so tests never wait or touch the network.
"""

from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from .collectors import robots as robots_mod

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class FetchError(Exception):
    """The request failed after all retries (network error or timeout)."""


class RobotsDisallowed(Exception):
    """robots.txt does not allow our User-Agent to fetch this URL."""

    def __init__(self, url: str, robots_url: str, reason: str | None = None):
        msg = f"robots.txt ({robots_url}) disallows {url}"
        if reason:
            msg += f" ({reason})"
        super().__init__(msg)
        self.url = url
        self.robots_url = robots_url
        self.reason = reason


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class PoliteClient:
    def __init__(
        self,
        crawler_cfg: dict[str, Any],
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        retry_backoff_seconds: float = 2.0,
    ):
        self.user_agent: str = crawler_cfg["user_agent"]
        self.timeout: float = float(crawler_cfg["timeout_seconds"])
        self.max_retries: int = int(crawler_cfg["max_retries"])
        self.respect_robots: bool = bool(crawler_cfg["respect_robots_txt"])
        self.min_interval: float = 1.0 / float(crawler_cfg["requests_per_second"])
        self.retry_backoff = retry_backoff_seconds
        self._clock = clock
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._robots: dict[str, robots_mod.RobotsResult] = {}
        self.request_count = 0
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent})

    # ---- rate limiting -------------------------------------------------
    def _wait_for_slot(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            wait = self.min_interval - (now - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()

    # ---- robots.txt ----------------------------------------------------
    def robots_for(self, url: str) -> robots_mod.RobotsResult:
        """robots.txt result for the URL's origin, fetched once per client."""
        origin = origin_of(url)
        if origin not in self._robots:
            self._robots[origin] = robots_mod.fetch_robots(self, origin)
        return self._robots[origin]

    def is_allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        return self.robots_for(url).can_fetch(self.user_agent, url)

    # ---- requests ------------------------------------------------------
    def request(self, method: str, url: str, *, check_robots: bool = True, **kwargs: Any) -> requests.Response:
        """Send one request (plus retries). Raises RobotsDisallowed or FetchError.

        HTTP error statuses (4xx/5xx) are returned, not raised: callers record them.
        """
        if check_robots and self.respect_robots:
            rb = self.robots_for(url)
            if not rb.can_fetch(self.user_agent, url):
                reason = "robots.txt could not be fetched, so nothing is crawled" if rb.status == "failed" else None
                raise RobotsDisallowed(url, rb.url, reason)
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("allow_redirects", True)
        last_exc: Exception | None = None
        resp: requests.Response | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                self._sleep(self.retry_backoff * attempt)
            self._wait_for_slot()
            self.request_count += 1
            try:
                resp = self.session.request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                resp = None
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                continue
            return resp
        if resp is not None:
            return resp
        raise FetchError(f"{method} {url} failed after {self.max_retries + 1} attempt(s): {last_exc}")

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("HEAD", url, **kwargs)
