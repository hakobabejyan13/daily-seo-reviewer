"""robots.txt collector.

fetch_robots() is used by the HTTP client to enforce robots.txt.
collect_robots() produces the JSON record stored in the daily snapshot.

Status codes follow the common convention (and urllib.robotparser's own
read() behaviour):
  200          -> parse the file
  401 / 403    -> treat as "disallow everything"
  other 4xx    -> no robots.txt, "allow everything"
  5xx / network error -> source failed, "disallow everything" (be safe)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.robotparser import RobotFileParser

if TYPE_CHECKING:  # avoid a runtime import cycle with http_client
    from ..http_client import PoliteClient

MAX_ROBOTS_BYTES = 500 * 1024  # Google reads at most 500 KiB


@dataclass
class RobotsResult:
    url: str
    status: str  # "ok" | "failed"
    http_status: int | None = None
    error: str | None = None
    mode: str = "parsed"  # "parsed" | "allow_all" | "disallow_all"
    text: str = ""
    sitemaps: list[str] = field(default_factory=list)
    parser: RobotFileParser | None = None

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.mode == "allow_all":
            return True
        if self.mode == "disallow_all" or self.parser is None:
            return False
        return self.parser.can_fetch(user_agent, url)


def parse_robots(url: str, text: str, http_status: int = 200) -> RobotsResult:
    """Build a RobotsResult from robots.txt text (no network)."""
    text = text[:MAX_ROBOTS_BYTES]
    parser = RobotFileParser(url)
    parser.parse(text.splitlines())
    parser.modified()  # mark as read, otherwise can_fetch() always says False
    return RobotsResult(
        url=url,
        status="ok",
        http_status=http_status,
        mode="parsed",
        text=text,
        sitemaps=list(parser.site_maps() or []),
        parser=parser,
    )


def fetch_robots(client: "PoliteClient", origin: str) -> RobotsResult:
    """Fetch and interpret robots.txt for one origin (scheme://host)."""
    from ..http_client import FetchError  # local import: no cycle at load time

    url = origin.rstrip("/") + "/robots.txt"
    try:
        resp = client.get(url, check_robots=False)
    except FetchError as e:
        return RobotsResult(url=url, status="failed", error=str(e), mode="disallow_all")
    code = resp.status_code
    if code == 200:
        return parse_robots(url, resp.text, http_status=code)
    if code in (401, 403):
        return RobotsResult(
            url=url, status="ok", http_status=code, mode="disallow_all",
            error=f"HTTP {code}: robots.txt access denied, treated as disallow all",
        )
    if 400 <= code < 500:
        return RobotsResult(
            url=url, status="ok", http_status=code, mode="allow_all",
            error=f"HTTP {code}: no robots.txt, treated as allow all",
        )
    return RobotsResult(
        url=url, status="failed", http_status=code, mode="disallow_all",
        error=f"HTTP {code}: server error, treated as disallow all",
    )


def collect_robots(client: "PoliteClient", base_url: str, check_urls: list[str] | None = None) -> dict[str, Any]:
    """Snapshot record for robots.txt: status, declared sitemaps, and whether
    our User-Agent may fetch each URL in check_urls."""
    rb = client.robots_for(base_url)
    return {
        "source": "robots",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url": rb.url,
        "status": rb.status,
        "http_status": rb.http_status,
        "error": rb.error,
        "mode": rb.mode,
        "user_agent": client.user_agent,
        "sitemaps_declared": rb.sitemaps,
        "size_bytes": len(rb.text.encode("utf-8")),
        "allowed": {u: rb.can_fetch(client.user_agent, u) for u in (check_urls or [])},
    }
