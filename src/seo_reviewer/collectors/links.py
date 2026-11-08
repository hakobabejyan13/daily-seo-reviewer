"""Broken link checker.

Input is today's crawl record: the internal_links of every crawled page that
has SEO data (HTTP 200 HTML). For every unique link URL the record keeps
which crawled pages link to it.

Order and cap:
  Links are ordered most-linked first (number of distinct crawled pages that
  link to them), then alphabetically, so the same site gives the same order
  every day. URLs already fetched by today's crawl reuse that result (no
  second request) and do not count against links.max_links. Of the rest, the
  first links.max_links are checked; the others are counted in
  left_out_by_cap and not listed.

Checking:
  Every request goes through the PoliteClient: robots.txt is obeyed (a
  disallowed link is recorded as disallowed and never requested), rate limit,
  timeout and retries apply. links.method (HEAD by default) is tried first;
  when HEAD gets a network error or any HTTP status >= 400 (405 and 501
  included) the same URL is asked again with GET, so a server that handles
  HEAD badly does not produce a false broken link. GET is sent with
  stream=True and the body is not read. Redirects are followed by hand, one
  hop at a time (max 10, loop detection), so every hop is robots-checked and
  recorded. A redirect to another host is recorded but only followed when
  links.check_external is true.

links.check_external false = same host as site.base_url only. The crawl
record only stores same-host links today, so true has no extra effect until
the crawler also stores external links (noted in the record).

Link status:
  ok         -> a final HTTP response was received (any code; 4xx/5xx are
                data for the diff, see counts.broken)
  failed     -> network error after retries (HEAD and GET), redirect loop,
                too many redirects
  disallowed -> robots.txt does not allow the URL (or a redirect hop)
  skipped    -> runtime.budget_seconds ran out before this link

Record status:
  ok       -> every listed link is ok or disallowed and the crawl was ok
  partial  -> some links failed or were skipped, or the crawl was partial
              (so the set of links is incomplete); partial_reasons says why
  failed   -> the crawl source failed or no link could be checked
  disabled -> links.enabled is false; nothing was requested
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

if TYPE_CHECKING:
    from ..http_client import PoliteClient

MAX_REDIRECTS = 10
MAX_LINKED_FROM = 20                   # source pages stored per link (count is always exact)
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


# ---- input from the crawl record --------------------------------------------

def gather_links(crawl_record: dict[str, Any], host: str, check_external: bool) -> tuple[dict[str, list[str]], list[str]]:
    """{link url: [crawled page urls that link to it]} plus notes."""
    inbound: dict[str, list[str]] = {}
    notes: list[str] = []
    truncated, other_host = [], 0
    for page in crawl_record.get("pages", []):
        seo = page.get("seo")
        if page.get("status") != "ok" or not seo:
            continue
        source = page["url"]
        if seo.get("internal_links_truncated"):
            truncated.append(source)
        for link in seo.get("internal_links") or []:
            if not check_external and _host(link) != host:
                other_host += 1
                continue
            sources = inbound.setdefault(link, [])
            if source not in sources:
                sources.append(source)
    if truncated:
        notes.append(f"{len(truncated)} page(s) had more internal links than the crawler stores per page, "
                     f"extra links not checked: {', '.join(truncated)}")
    if other_host:
        notes.append(f"{other_host} link(s) to another host ignored (links.check_external is false)")
    if check_external:
        notes.append("links.check_external is true, but the crawl stores same-host links only")
    return inbound, notes


def order_links(inbound: dict[str, list[str]]) -> list[str]:
    """Most-linked first, then alphabetical (deterministic)."""
    return sorted(inbound, key=lambda u: (-len(inbound[u]), u))


def crawl_results(crawl_record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Results the crawl already has, by URL: the page URLs themselves, plus
    the final URL of a page reached through same-host redirects."""
    known: dict[str, dict[str, Any]] = {}
    for page in crawl_record.get("pages", []):
        if page.get("status") not in ("ok", "failed", "disallowed"):
            continue  # skipped pages were never fetched
        known.setdefault(page["url"], {
            "status": page["status"], "http_status": page.get("http_status"),
            "final_url": page.get("final_url"), "redirect_chain": list(page.get("redirect_chain") or []),
            "error": page.get("error"),
        })
    for page in crawl_record.get("pages", []):
        final = page.get("final_url")
        if page.get("status") == "ok" and page.get("redirect_chain") and final \
                and page.get("http_status") not in REDIRECT_STATUSES:
            known.setdefault(final, {"status": "ok", "http_status": page["http_status"], "final_url": final,
                                     "redirect_chain": [], "error": None})
    return known


# ---- checking one link -------------------------------------------------------

def _close(resp: Any) -> None:
    close = getattr(resp, "close", None)
    if callable(close):
        close()


def _probe(client: "PoliteClient", url: str, method: str) -> tuple[Any, str, str | None]:
    """One hop: (response, method used, note). Raises RobotsDisallowed or FetchError."""
    from ..http_client import FetchError

    if method == "GET":
        resp = client.get(url, allow_redirects=False, stream=True)
        _close(resp)
        return resp, "GET", None
    try:
        resp = client.head(url, allow_redirects=False)
    except FetchError as e:
        note = f"HEAD failed ({e}), retried with GET"
    else:
        if resp.status_code < 400:
            return resp, "HEAD", None
        note = f"HEAD returned {resp.status_code}, retried with GET"
    resp = client.get(url, allow_redirects=False, stream=True)
    _close(resp)
    return resp, "GET", note


def _empty_link(url: str, sources: list[str]) -> dict[str, Any]:
    return {
        "url": url, "status": "failed", "source": "check", "http_status": None, "final_url": None,
        "redirect_chain": [], "method": None, "error": None, "notes": [],
        "linked_from": sources[:MAX_LINKED_FROM], "linked_from_count": len(sources),
    }


def check_link(client: "PoliteClient", url: str, sources: list[str], method: str,
               check_external: bool) -> dict[str, Any]:
    from ..http_client import FetchError, RobotsDisallowed

    link = _empty_link(url, sources)
    start_host = _host(url)
    visited = {url}
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            resp, used, note = _probe(client, current, method)
        except RobotsDisallowed as e:
            link["status"] = "disallowed"
            link["error"] = str(e)
            return link
        except FetchError as e:
            link["error"] = f"request failed: {e}"
            return link
        link["method"] = used
        if note:
            link["notes"].append(f"{current}: {note}")
        code = resp.status_code
        location = resp.headers.get("Location")
        if code in REDIRECT_STATUSES and location:
            target = urljoin(current, location.strip())
            link["redirect_chain"].append({"url": current, "status": code, "location": target, "method": used})
            if _host(target) != start_host and not check_external:
                link.update(status="ok", final_url=target, http_status=code,
                            error=f"redirects to another host ({_host(target)}), not followed")
                return link
            if target in visited:
                link["error"] = f"redirect loop at {target}"
                return link
            visited.add(target)
            current = target
            continue
        link.update(status="ok", http_status=code, final_url=current)
        return link
    link["error"] = f"more than {MAX_REDIRECTS} redirects"
    return link


def is_broken(link: dict[str, Any]) -> bool:
    """Failed, or the final response is HTTP 4xx/5xx."""
    return link["status"] == "failed" or (link["status"] == "ok" and (link["http_status"] or 0) >= 400)


# ---- collector -------------------------------------------------------------

def collect_links(client: "PoliteClient", cfg: dict[str, Any], crawl_record: dict[str, Any] | None,
                  deadline: float | None = None) -> dict[str, Any]:
    """Snapshot record for the link check.

    deadline: value of client.now() after which no new link is started (the
    same runtime.budget_seconds deadline as the crawl; None = no limit).
    """
    lcfg = cfg["links"]
    method = lcfg["method"].upper()
    check_external = bool(lcfg["check_external"])
    max_links: int = lcfg["max_links"]
    record: dict[str, Any] = {
        "source": "links",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "failed",
        "error": None,
        "partial_reasons": [],
        "notes": [],
        "limits": {"max_links": max_links, "method": method, "check_external": check_external,
                   "max_redirects": MAX_REDIRECTS, "max_linked_from": MAX_LINKED_FROM},
        "unique_links": 0,
        "from_crawl": 0,
        "to_check": 0,
        "left_out_by_cap": 0,
        "counts": _counts([]),
        "links": [],
    }
    if not lcfg["enabled"]:
        record.update(status="disabled", error="links.enabled is false")
        return record

    crawl_status = (crawl_record or {}).get("status")
    if crawl_status not in ("ok", "partial"):
        err = (crawl_record or {}).get("error") or "no crawl record"
        record["error"] = f"crawl source {crawl_status or 'missing'} ({err}), so no links were checked"
        return record

    host = _host(cfg["site"]["base_url"])
    inbound, notes = gather_links(crawl_record, host, check_external)
    record["notes"] = notes
    ordered = order_links(inbound)
    known = crawl_results(crawl_record)
    reuse = [u for u in ordered if u in known]
    pending = [u for u in ordered if u not in known]
    to_check = pending[:max_links]
    record.update(unique_links=len(ordered), from_crawl=len(reuse), to_check=len(to_check),
                  left_out_by_cap=len(pending) - len(to_check))
    if record["left_out_by_cap"]:
        record["notes"].append(f"{record['left_out_by_cap']} link(s) not checked: over links.max_links ({max_links})")

    results: dict[str, dict[str, Any]] = {}
    for url in reuse:
        link = _empty_link(url, inbound[url])
        link.update(known[url], source="crawl", method="GET")
        results[url] = link
    budget_hit = False
    for url in to_check:
        if deadline is not None and client.now() >= deadline:
            budget_hit = True
        if budget_hit:
            link = _empty_link(url, inbound[url])
            link.update(status="skipped", error="runtime.budget_seconds reached before this link")
        else:
            link = check_link(client, url, inbound[url], method, check_external)
        results[url] = link
    record["links"] = [results[u] for u in ordered if u in results]  # most-linked first

    counts = _counts(record["links"])
    record["counts"] = counts
    reasons: list[str] = []
    if budget_hit:
        reasons.append(f"runtime.budget_seconds reached: {counts['skipped']} link(s) not checked")
    checked_failed = [lk for lk in record["links"] if lk["status"] == "failed" and lk["source"] == "check"]
    if checked_failed:
        reasons.append(f"{len(checked_failed)} link(s) failed: " + "; ".join(
            f"{lk['url']}: {lk['error']}" for lk in checked_failed[:10])
            + (" ..." if len(checked_failed) > 10 else ""))
    if crawl_status == "partial":
        reasons.append("crawl source partial: links of pages that were not crawled are missing")
    record["partial_reasons"] = reasons

    if record["links"] and counts["ok"] == 0 and counts["disallowed"] < len(record["links"]):
        record["error"] = "no link could be checked: " + "; ".join(reasons)
        return record
    if reasons:
        record["status"] = "partial"
        record["error"] = "; ".join(reasons)
    else:
        record["status"] = "ok"
    return record


def _counts(links: list[dict[str, Any]]) -> dict[str, int]:
    c = {"ok": 0, "failed": 0, "disallowed": 0, "skipped": 0}
    for lk in links:
        c[lk["status"]] += 1
    c["broken"] = sum(1 for lk in links if is_broken(lk))
    c["http_4xx"] = sum(1 for lk in links if lk["status"] == "ok" and 400 <= (lk["http_status"] or 0) < 500)
    c["http_5xx"] = sum(1 for lk in links if lk["status"] == "ok" and (lk["http_status"] or 0) >= 500)
    c["redirected"] = sum(1 for lk in links if lk["redirect_chain"])
    c["from_crawl"] = sum(1 for lk in links if lk["source"] == "crawl")
    c["checked"] = sum(1 for lk in links if lk["source"] == "check" and lk["status"] != "skipped")
    c["get_fallback"] = sum(1 for lk in links if lk["notes"])
    return c
