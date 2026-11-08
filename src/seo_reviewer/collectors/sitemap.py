"""Sitemap collector.

Fetches site.sitemap_url through the PoliteClient (so robots.txt is
enforced), handles <sitemapindex> and <urlset> (plain or gzipped), follows
child sitemaps up to sitemap.max_child_sitemaps and stops collecting URLs at
sitemap.max_urls.

Child sitemaps are fetched freshest first (by the <lastmod> the index gives
them), so when a cap is hit, the sitemaps holding the newest content are the
ones that were read.

Record status:
  ok      -> every sitemap fetched and parsed, no cap hit
  partial -> some URLs collected, but a child sitemap failed or a cap was hit
             (partial_reasons says which; per-sitemap entries say exactly
             which sitemaps are complete)
  failed  -> the root sitemap could not be fetched/parsed, or no child of an
             index could be read

URLs are stored per sitemap ({loc: lastmod}) so a later diff can compare
sitemaps that were complete on both days even when the record is partial.
lastmod is stored exactly as the site wrote it (or null); parse_lastmod()
turns it into a UTC datetime when needed.
"""

from __future__ import annotations

import gzip
import io
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from lxml import etree

if TYPE_CHECKING:
    from ..http_client import PoliteClient

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
# The sitemap protocol limits one file to 50 MB uncompressed; a little slack.
MAX_SITEMAP_BYTES = 52 * 1024 * 1024


# ---- parsing (no network) ------------------------------------------------

def _decompress_capped(content: bytes, limit: int | None = None) -> bytes:
    limit = MAX_SITEMAP_BYTES if limit is None else limit
    with gzip.GzipFile(fileobj=io.BytesIO(content)) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"gzipped sitemap is larger than {limit} bytes uncompressed")
    return data


def _child_text(el: Any, name: str) -> str | None:
    node = el.find(f"{{{SITEMAP_NS}}}{name}")
    if node is None:
        node = el.find(name)
    if node is None or node.text is None:
        return None
    text = node.text.strip()
    return text or None


def parse_sitemap(content: bytes) -> dict[str, Any]:
    """Parse sitemap XML (optionally gzipped). Raises ValueError if not a sitemap.

    Returns kind ("sitemapindex" | "urlset"), entries (number of <sitemap>/<url>
    elements), locs, with_lastmod, and items: [(loc, lastmod or None)] for
    every entry that has a <loc>.
    """
    if content[:2] == b"\x1f\x8b":
        content = _decompress_capped(content)
    elif len(content) > MAX_SITEMAP_BYTES:
        raise ValueError(f"sitemap is larger than {MAX_SITEMAP_BYTES} bytes")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    try:
        root = etree.fromstring(content, parser=parser)
    except etree.XMLSyntaxError as e:
        raise ValueError(f"not valid XML: {e}") from None
    if root is None:
        raise ValueError("empty document")
    kind = etree.QName(root).localname
    if kind not in ("sitemapindex", "urlset"):
        raise ValueError(f"root element is <{kind}>, expected <sitemapindex> or <urlset>")
    child_tag = "sitemap" if kind == "sitemapindex" else "url"
    entries = [el for el in root if isinstance(el.tag, str) and etree.QName(el).localname == child_tag]
    items: list[tuple[str, str | None]] = []
    with_lastmod = 0
    for el in entries:
        loc = _child_text(el, "loc")
        lastmod = _child_text(el, "lastmod")
        if lastmod is not None:
            with_lastmod += 1
        if loc is not None:
            items.append((loc, lastmod))
    return {
        "kind": kind,
        "entries": len(entries),
        "locs": [loc for loc, _ in items],
        "with_lastmod": with_lastmod,
        "items": items,
    }


_W3C_DATETIME = re.compile(
    r"^(\d{4})(?:-(\d{2})(?:-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?"
    r"\s*(Z|[+-]\d{2}:?\d{2})?)?)?)?$"
)


def parse_lastmod(value: str | None) -> datetime | None:
    """Parse a sitemap <lastmod> (W3C Datetime, e.g. 2026-11-06 or
    2026-10-29T13:16:41.361Z) into an aware UTC datetime. None if missing or
    not parseable. A time without a timezone is read as UTC."""
    if not value:
        return None
    m = _W3C_DATETIME.match(value.strip())
    if not m:
        return None
    year, month, day, hh, mm, ss, frac, tz = m.groups()
    try:
        micro = int((frac or "0")[:6].ljust(6, "0"))
        dt = datetime(int(year), int(month or 1), int(day or 1),
                      int(hh or 0), int(mm or 0), int(ss or 0), micro)
    except ValueError:
        return None
    offset = timedelta(0)
    if tz and tz != "Z":
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        offset = sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return (dt - offset).replace(tzinfo=timezone.utc)


def _freshest_first(items: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    """Sort index entries by lastmod, newest first; entries without a usable
    lastmod keep their original order after the dated ones."""
    def key(pair: tuple[int, tuple[str, str | None]]) -> tuple[int, float, int]:
        idx, (_, lastmod) = pair
        dt = parse_lastmod(lastmod)
        return (0, -dt.timestamp(), idx) if dt else (1, 0.0, idx)

    return [item for _, item in sorted(enumerate(items), key=key)]


# ---- fetching -------------------------------------------------------------

def _fetch(client: "PoliteClient", url: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Fetch and parse one sitemap file. Returns (parsed or None, info)."""
    from ..http_client import FetchError, RobotsDisallowed

    info: dict[str, Any] = {"url": url, "kind": None, "status": "failed", "http_status": None, "error": None}
    try:
        resp = client.get(url)
    except RobotsDisallowed as e:
        info["error"] = str(e)
        return None, info
    except FetchError as e:
        info["error"] = f"request failed: {e}"
        return None, info
    info["http_status"] = resp.status_code
    if resp.status_code != 200:
        info["error"] = f"HTTP {resp.status_code}"
        return None, info
    try:
        parsed = parse_sitemap(resp.content or b"")
    except (ValueError, OSError, EOFError) as e:
        info["error"] = f"not parseable: {e}"
        return None, info
    info["kind"] = parsed["kind"]
    info["status"] = "ok"
    return parsed, info


def collect_sitemap(client: "PoliteClient", cfg: dict[str, Any]) -> dict[str, Any]:
    """Snapshot record for the site's sitemap(s)."""
    root_url: str = cfg["site"]["sitemap_url"]
    max_children: int = cfg["sitemap"]["max_child_sitemaps"]
    max_urls: int = cfg["sitemap"]["max_urls"]
    started = datetime.now(timezone.utc)

    record: dict[str, Any] = {
        "source": "sitemap",
        "fetched_at": started.isoformat(timespec="seconds"),
        "root_url": root_url,
        "root_kind": None,
        "status": "failed",
        "error": None,
        "partial_reasons": [],
        "limits": {"max_child_sitemaps": max_children, "max_urls": max_urls},
        "url_count": 0,
        "urls_with_lastmod": 0,
        "sitemaps": [],
        "skipped_sitemaps": [],
    }

    parsed, info = _fetch(client, root_url)
    if parsed is None:
        record["error"] = f"{root_url}: {info['error']}"
        record["sitemaps"].append({**info, "url_count": 0, "truncated": False, "urls": {}})
        return record
    record["root_kind"] = parsed["kind"]

    seen_urls: dict[str, str | None] = {}
    reasons: list[str] = []
    budget = {"left": max_urls}

    def add_urlset(info: dict[str, Any], items: list[tuple[str, str | None]]) -> None:
        urls: dict[str, str | None] = {}
        truncated = False
        for loc, lastmod in items:
            if loc in urls:
                continue
            if loc not in seen_urls:
                if budget["left"] <= 0:
                    truncated = True
                    break
                budget["left"] -= 1
                seen_urls[loc] = lastmod
            urls[loc] = lastmod
        info.update(url_count=len(urls), truncated=truncated, urls=urls)
        if truncated:
            reasons.append(f"max_urls ({max_urls}) reached in {info['url']}: {len(urls)} of {len(items)} URLs kept")
        record["sitemaps"].append(info)

    if parsed["kind"] == "urlset":
        add_urlset({**info, "entries": parsed["entries"]}, parsed["items"])
    else:
        record["sitemaps"].append({**info, "entries": parsed["entries"], "url_count": 0, "truncated": False, "urls": {}})
        visited = {root_url}
        queue: deque[str] = deque()
        for loc, _ in _freshest_first(parsed["items"]):
            if loc not in visited:
                visited.add(loc)
                queue.append(loc)
        fetched_children = 0
        ok_children = 0
        while queue:
            child_url = queue.popleft()
            if fetched_children >= max_children:
                record["skipped_sitemaps"] = [child_url, *queue]
                reasons.append(f"max_child_sitemaps ({max_children}) reached: {len(record['skipped_sitemaps'])} sitemap(s) not fetched")
                break
            if budget["left"] <= 0:
                record["skipped_sitemaps"] = [child_url, *queue]
                reasons.append(f"max_urls ({max_urls}) reached: {len(record['skipped_sitemaps'])} sitemap(s) not fetched")
                break
            fetched_children += 1
            child, cinfo = _fetch(client, child_url)
            if child is None:
                reasons.append(f"{child_url}: {cinfo['error']}")
                record["sitemaps"].append({**cinfo, "url_count": 0, "truncated": False, "urls": {}})
                continue
            ok_children += 1
            if child["kind"] == "sitemapindex":
                # Not allowed by the protocol, but seen in the wild: follow it.
                record["sitemaps"].append({**cinfo, "entries": child["entries"], "url_count": 0, "truncated": False, "urls": {}})
                for loc, _ in _freshest_first(child["items"]):
                    if loc not in visited:
                        visited.add(loc)
                        queue.append(loc)
            else:
                add_urlset({**cinfo, "entries": child["entries"]}, child["items"])
        if parsed["items"] and ok_children == 0:
            record["error"] = "no child sitemap could be read: " + "; ".join(reasons)
            record["partial_reasons"] = reasons
            return record

    record["url_count"] = len(seen_urls)
    record["urls_with_lastmod"] = sum(1 for v in seen_urls.values() if v)
    record["partial_reasons"] = reasons
    if reasons:
        record["status"] = "partial"
        record["error"] = "; ".join(reasons)
    else:
        record["status"] = "ok"
    return record
