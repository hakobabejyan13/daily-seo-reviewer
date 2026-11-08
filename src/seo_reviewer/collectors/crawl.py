"""On-page crawler.

Pages crawled (in this order, duplicates removed):
  1. the homepage (site.base_url + "/")
  2. crawler.extra_urls (absolute, or relative to base_url)
  3. the newest crawler.max_pages URLs of today's sitemap record, by
     <lastmod> (same host as base_url only; URLs without a usable lastmod
     come after the dated ones, in sitemap order). crawler.sitemap_include /
     crawler.sitemap_exclude are regular expressions matched (re.search)
     against the URL path: with includes, a URL must match at least one;
     a URL matching any exclude is dropped. Empty lists = no filter.
If the sitemap source failed, only 1 and 2 are crawled and the record says so.

Every request goes through the PoliteClient (robots.txt, rate limit,
retries). Redirects are followed by hand, one hop at a time, so every hop is
robots-checked and rate-limited and the full chain is recorded. Redirects to
another host are recorded but not followed.

Per page: HTTP status, redirect chain, final URL, X-Robots-Tag, <title>,
meta description, H1s, canonical (absolute), meta robots, noindex, and the
internal links found (input for the link checker). Server HTML only, no JS.

Page status:
  ok         -> a final HTTP response was received (any status code; SEO
                fields are parsed only for HTTP 200 HTML)
  failed     -> network error, too many redirects or a redirect loop
  disallowed -> robots.txt does not allow the URL (or a redirect hop); it
                was not fetched
  skipped    -> not fetched because runtime.budget_seconds ran out

Record status:
  ok      -> every selected page has status ok or disallowed
  partial -> some pages failed or were skipped, or the sitemap source was
             not ok (so the page selection is incomplete); partial_reasons
             says why
  failed  -> no page could be fetched (e.g. robots.txt unreachable)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .sitemap import parse_lastmod

if TYPE_CHECKING:
    from ..http_client import PoliteClient

MAX_REDIRECTS = 10
MAX_HTML_BYTES = 5 * 1024 * 1024       # parse at most 5 MB of one page
MAX_LINKS_PER_PAGE = 1000              # keeps crawl.json bounded
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_WS = re.compile(r"\s+")
_CHARSET = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.I)


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    return _WS.sub(" ", text).strip()


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def normalize_link(href: str, page_url: str) -> str | None:
    """Absolute http(s) URL without fragment, or None for mailto:, js:, etc."""
    href = (href or "").strip()
    if not href or href.startswith("#"):
        return None
    try:
        parts = urlsplit(urljoin(page_url, href))
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, parts.query, ""))


# ---- page selection --------------------------------------------------------

def select_pages(cfg: dict[str, Any], sitemap_record: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Pages to crawl as [{url, reason, lastmod}] plus notes on the selection."""
    base = cfg["site"]["base_url"]
    host = _host(base)
    max_pages: int = cfg["crawler"]["max_pages"]
    notes: list[str] = []
    pages: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(url: str, reason: str, lastmod: str | None = None) -> None:
        if url not in seen:
            seen.add(url)
            pages.append({"url": url, "reason": reason, "lastmod": lastmod})

    add(base + "/", "homepage")
    for raw in cfg["crawler"]["extra_urls"]:
        url = normalize_link(raw, base + "/")
        if url is None or _host(url) != host:
            notes.append(f"extra_url {raw!r} ignored: not an http(s) URL on {host}")
            continue
        add(url, "extra_url")

    status = (sitemap_record or {}).get("status")
    if status not in ("ok", "partial"):
        err = (sitemap_record or {}).get("error") or "no sitemap record"
        notes.append(f"sitemap source {status or 'missing'} ({err}): crawled homepage and extra_urls only")
        return pages, notes
    if status == "partial":
        notes.append("sitemap source partial: newest pages picked from the sitemaps that were read")

    # Unique URLs across all sitemap files, first lastmod seen wins.
    candidates: dict[str, str | None] = {}
    for sm in sitemap_record.get("sitemaps", []):
        for loc, lastmod in (sm.get("urls") or {}).items():
            candidates.setdefault(loc, lastmod)
    includes = [re.compile(p) for p in cfg["crawler"].get("sitemap_include") or []]
    excludes = [re.compile(p) for p in cfg["crawler"].get("sitemap_exclude") or []]
    dated, undated = [], []
    off_host = filtered = 0
    for order, (loc, lastmod) in enumerate(candidates.items()):
        url = normalize_link(loc, base + "/")
        if url is None or _host(url) != host:
            off_host += 1
            continue
        path = urlsplit(url).path
        if (includes and not any(p.search(path) for p in includes)) or any(p.search(path) for p in excludes):
            filtered += 1
            continue
        dt = parse_lastmod(lastmod)
        (dated if dt else undated).append((-(dt.timestamp()) if dt else 0.0, order, url, lastmod))
    if off_host:
        notes.append(f"{off_host} sitemap URL(s) ignored: not on {host}")
    if filtered:
        notes.append(f"{filtered} sitemap URL(s) left out by crawler.sitemap_include/sitemap_exclude")
    picked = 0
    for _, _, url, lastmod in sorted(dated) + undated:
        if picked >= max_pages:
            break
        if url in seen:
            continue
        add(url, "sitemap", lastmod)
        picked += 1
    if picked < max_pages:
        notes.append(f"sitemap gave {picked} page(s) for max_pages {max_pages}")
    return pages, notes


# ---- HTML parsing (no network) ---------------------------------------------

def _charset(content_type: str | None) -> str | None:
    m = _CHARSET.search(content_type or "")
    return m.group(1) if m else None


def _rel_values(tag: Any) -> list[str]:
    rel = tag.get("rel") or []
    if isinstance(rel, str):
        rel = rel.split()
    return [r.lower() for r in rel]


def _directives(value: str) -> list[str]:
    return [d.strip().lower() for d in value.split(",") if d.strip()]


def parse_robots_directives(values: list[str], header: bool) -> list[str]:
    """Directives that apply to all crawlers or to Googlebot.

    X-Robots-Tag values may be prefixed by a user agent ("googlebot: noindex");
    directives for other named agents are ignored.
    """
    out: list[str] = []
    for value in values:
        if header:
            m = re.match(r"^\s*([A-Za-z][\w-]*)\s*:\s*(.*)$", value)
            if m and m.group(1).lower() not in ("noindex", "nofollow", "none", "unavailable_after",
                                                "max-snippet", "max-image-preview", "max-video-preview"):
                if m.group(1).lower() != "googlebot":
                    continue
                value = m.group(2)
        out.extend(_directives(value))
    return out


def parse_html(content: bytes, page_url: str, content_type: str | None = None) -> dict[str, Any]:
    """SEO fields and internal links of one HTML page."""
    soup = BeautifulSoup(content[:MAX_HTML_BYTES], "lxml", from_encoding=_charset(content_type))
    head = soup.head or soup

    titles = head.find_all("title") or soup.find_all("title", limit=1)
    title = _clean(titles[0].get_text()) if titles else None

    def metas(name: str) -> list[str]:
        return [m.get("content") or "" for m in soup.find_all("meta")
                if (m.get("name") or "").strip().lower() == name]

    descriptions = metas("description")
    meta_robots = metas("robots") + metas("googlebot")

    base_href = None
    base_tag = soup.find("base", href=True)
    if base_tag is not None:
        base_href = urljoin(page_url, base_tag["href"].strip())
    link_base = base_href or page_url

    canonicals = [t.get("href") or "" for t in soup.find_all("link") if "canonical" in _rel_values(t)]
    canonical_raw = canonicals[0].strip() if canonicals else None
    canonical = urljoin(link_base, canonical_raw) if canonical_raw else None

    h1s = [_clean(h.get_text(" ")) or "" for h in soup.find_all("h1")]

    host = _host(page_url)
    internal: list[str] = []
    seen: set[str] = set()
    anchors = 0
    truncated = False
    for a in soup.find_all("a", href=True):
        url = normalize_link(a["href"], link_base)
        if url is None:
            continue
        anchors += 1
        if _host(url) != host or url in seen:
            continue
        if len(internal) >= MAX_LINKS_PER_PAGE:
            truncated = True
            continue
        seen.add(url)
        internal.append(url)

    html_lang = soup.html.get("lang") if soup.html else None
    return {
        "title": title,
        "title_count": len(titles),
        "meta_description": _clean(descriptions[0]) if descriptions else None,
        "meta_description_count": len(descriptions),
        "h1": h1s,
        "canonical": canonical,
        "canonical_raw": canonical_raw,
        "canonical_count": len(canonicals),
        "meta_robots": [_clean(v) for v in meta_robots],
        "lang": html_lang,
        "links_total": anchors,
        "internal_links": internal,
        "internal_links_truncated": truncated,
    }


# ---- fetching --------------------------------------------------------------

def _empty_page(sel: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": sel["url"], "reason": sel["reason"], "lastmod": sel["lastmod"],
        "status": "failed", "error": None, "http_status": None, "final_url": None,
        "redirect_chain": [], "content_type": None, "x_robots_tag": [], "noindex": None,
        "noindex_sources": [], "seo": None,
    }


def fetch_page(client: "PoliteClient", sel: dict[str, Any]) -> dict[str, Any]:
    """Fetch one page, following same-host redirects by hand."""
    from ..http_client import FetchError, RobotsDisallowed

    page = _empty_page(sel)
    url = sel["url"]
    start_host = _host(url)
    visited = {url}
    for _ in range(MAX_REDIRECTS + 1):
        try:
            resp = client.get(url, allow_redirects=False)
        except RobotsDisallowed as e:
            page["status"] = "disallowed"
            page["error"] = str(e)
            return page
        except FetchError as e:
            page["error"] = f"request failed: {e}"
            return page
        code = resp.status_code
        location = resp.headers.get("Location")
        if code in REDIRECT_STATUSES and location:
            target = urljoin(url, location.strip())
            page["redirect_chain"].append({"url": url, "status": code, "location": target})
            if _host(target) != start_host:
                page.update(status="ok", final_url=target, http_status=code,
                            error=f"redirects to another host ({_host(target)}), not followed")
                return page
            if target in visited:
                page["error"] = f"redirect loop at {target}"
                return page
            visited.add(target)
            url = target
            continue
        return _finish(page, url, resp)
    page["error"] = f"more than {MAX_REDIRECTS} redirects"
    return page


def _finish(page: dict[str, Any], url: str, resp: Any) -> dict[str, Any]:
    ctype = resp.headers.get("Content-Type")
    xrt = [v.strip() for v in _header_values(resp, "X-Robots-Tag") if v.strip()]
    page.update(status="ok", http_status=resp.status_code, final_url=url,
                content_type=ctype, x_robots_tag=xrt)
    sources: list[str] = []
    if any(d in ("noindex", "none") for d in parse_robots_directives(xrt, header=True)):
        sources.append("x-robots-tag")
    if resp.status_code == 200:
        if "html" not in (ctype or "").lower():
            page["error"] = f"not HTML ({ctype or 'no Content-Type'}), SEO fields not parsed"
        else:
            seo = parse_html(resp.content or b"", url, ctype)
            page["seo"] = seo
            if any(d in ("noindex", "none") for d in parse_robots_directives(seo["meta_robots"], header=False)):
                sources.append("meta-robots")
    page["noindex_sources"] = sources
    page["noindex"] = bool(sources)
    return page


def _header_values(resp: Any, name: str) -> list[str]:
    """All values of a header; several X-Robots-Tag headers are allowed."""
    raw = getattr(resp, "raw", None)
    headers = getattr(raw, "headers", None)
    if headers is not None and hasattr(headers, "getlist"):
        vals = headers.getlist(name)
        if vals:
            return list(vals)
    value = resp.headers.get(name)
    return [value] if value else []


# ---- collector -------------------------------------------------------------

def collect_crawl(client: "PoliteClient", cfg: dict[str, Any], sitemap_record: dict[str, Any] | None,
                  deadline: float | None = None) -> dict[str, Any]:
    """Snapshot record for the on-page crawl.

    deadline: value of client.now() after which no new request is started
    (from runtime.budget_seconds; None = no limit).
    """
    started = datetime.now(timezone.utc)
    selection, notes = select_pages(cfg, sitemap_record)
    record: dict[str, Any] = {
        "source": "crawl",
        "fetched_at": started.isoformat(timespec="seconds"),
        "status": "failed",
        "error": None,
        "partial_reasons": [],
        "selection_notes": notes,
        "limits": {"max_pages": cfg["crawler"]["max_pages"], "max_redirects": MAX_REDIRECTS,
                   "max_links_per_page": MAX_LINKS_PER_PAGE},
        "pages_selected": len(selection),
        "counts": {},
        "pages": [],
    }

    reasons: list[str] = []
    robots = client.robots_for(cfg["site"]["base_url"]) if client.respect_robots else None
    if robots is not None and robots.status == "failed":
        record["error"] = f"robots.txt could not be fetched ({robots.error}), so no page was crawled"
        for sel in selection:
            page = _empty_page(sel)
            page.update(status="disallowed", error=record["error"])
            record["pages"].append(page)
        record["counts"] = _counts(record["pages"])
        return record

    budget_hit = False
    for sel in selection:
        if deadline is not None and client.now() >= deadline:
            budget_hit = True
        if budget_hit:
            page = _empty_page(sel)
            page.update(status="skipped", error="runtime.budget_seconds reached before this page")
            record["pages"].append(page)
            continue
        record["pages"].append(fetch_page(client, sel))

    counts = _counts(record["pages"])
    record["counts"] = counts
    if budget_hit:
        reasons.append(f"runtime.budget_seconds reached: {counts['skipped']} page(s) not fetched")
    if counts["failed"]:
        reasons.append(f"{counts['failed']} page(s) failed: " + "; ".join(
            f"{p['url']}: {p['error']}" for p in record["pages"] if p["status"] == "failed"))
    if any(n.startswith("sitemap source") for n in notes):
        reasons.extend(n for n in notes if n.startswith("sitemap source"))

    record["partial_reasons"] = reasons
    if counts["ok"] == 0 and counts["disallowed"] < len(selection):
        record["error"] = "no page could be fetched: " + "; ".join(reasons)
        return record
    if reasons:
        record["status"] = "partial"
        record["error"] = "; ".join(reasons)
    else:
        record["status"] = "ok"
    return record


def _counts(pages: list[dict[str, Any]]) -> dict[str, int]:
    c = {"ok": 0, "failed": 0, "disallowed": 0, "skipped": 0}
    for p in pages:
        c[p["status"]] += 1
    c["http_200"] = sum(1 for p in pages if p["http_status"] == 200)
    c["redirected"] = sum(1 for p in pages if p["redirect_chain"])
    c["noindex"] = sum(1 for p in pages if p["noindex"])
    return c
