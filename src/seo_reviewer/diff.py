"""Diff engine: today's snapshot vs earlier snapshots -> findings.json.

Deterministic. Every finding comes from data in data/snapshots/; the LLM
summary (summarize.py) only summarizes this file.

Per source:
  - today failed      -> exactly one finding "SOURCE failed: ERROR" and no
                         per-URL findings for that source. If the source it
                         depends on failed too (crawl <- robots, links <-
                         crawl), it is an info "not run" finding instead, so
                         one root cause is not reported three times.
  - today partial     -> one info finding with the reasons, then a
                         like-for-like compare of what was collected.
  - today disabled    -> no finding, listed as disabled.
  - today ok/partial  -> compared with the last earlier day where the source
                         was ok (snapshot.last_day_with_status), or with
                         --compare-to when given. No such day = baseline:
                         the current issues are listed as findings with
                         change "baseline", and the file says so.

Page / link / run issues that existed on both days are not repeated as
findings; they are listed under "ongoing" (counts per rule), so a quiet day
stays short ("all stable").

PageSpeed Insights:
  - lab metrics (score, LCP, CLS, TBT) need psi.regression_confirmations
    consecutive regressed runs before they are reported at their normal
    severity. Until then: info "possible regression, awaiting confirmation
    (k of N)". If the next run is fine again, the pending state is cleared
    (info "cleared"). The state is rebuilt from the ok runs of that URL x
    strategy in earlier snapshots, so a failed PSI run neither advances nor
    resets the counter. The reference value is the last run that was not
    regressed (or the run that confirmed a regression).
  - CrUX field data (28-day real-user data) is compared directly with the
    previous ok run of the same URL x strategy, by category
    (FAST / AVERAGE / SLOW). URL data and origin data are kept separate.

URLs: with rules.trailing_slash_equivalent, /x and /x/ are the same URL when
matching canonicals and link results to pages. A canonical that differs from
the page URL only by that slash is one grouped site-level finding
(canonical_slash_mismatch), not a per-page "canonical points elsewhere".
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from . import __version__
from . import rules as R
from .collectors.links import is_broken
from .snapshot import (SnapshotError, _dump, _write_atomic, last_day_with_status, latest_previous_day,
                       list_snapshot_days, parse_day, read_meta, read_snapshot, read_source)

FINDINGS_SCHEMA_VERSION = 1
UPSTREAM = {"crawl": "robots", "links": "crawl"}
CRITICAL_SOURCES = frozenset({"robots", "sitemap", "crawl"})
CRUX_RANK = {"FAST": 0, "AVERAGE": 1, "SLOW": 2}
CRUX_LABEL = {"lcp_ms": ("CrUX LCP", " ms"), "inp_ms": ("CrUX INP", " ms"), "cls": ("CrUX CLS", "")}
LAB_METRICS = {
    # metric: (rule, label, unit, worse-direction sign, threshold getter)
    "performance_score": ("psi_performance_score", "performance score", "", -1,
                          lambda th: th["psi_performance_drop_points"]),
    "lcp_ms": ("psi_lcp", "lab LCP", " ms", 1, lambda th: th["lab_tolerance"]["lcp_ms"]),
    "cls": ("psi_cls", "lab CLS", "", 1, lambda th: th["lab_tolerance"]["cls"]),
    "tbt_ms": ("psi_tbt", "lab TBT", " ms", 1, lambda th: th["lab_tolerance"]["tbt_ms"]),
}


# ---- helpers -------------------------------------------------------------------

def url_key(url: str | None, slash_equivalent: bool) -> str | None:
    """Comparison key for a URL: lower-case scheme/host, no fragment and,
    when slash_equivalent, no trailing slash (except the root "/")."""
    if not url:
        return None
    p = urlsplit(url)
    path = p.path or "/"
    if slash_equivalent and len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, p.query, ""))


def _fmt(v: Any, unit: str = "") -> str:
    if v is None:
        return "none"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        v = round(v, 4)
    if isinstance(v, (list, tuple)):
        return " | ".join(str(x) for x in v) if v else "none"
    if isinstance(v, str):
        return repr(v) if len(v) <= 200 else repr(v[:197] + "...")
    return f"{v}{unit}"


class Ctx:
    def __init__(self, cfg: dict[str, Any], root: Path, day: str, compare_to: str | None):
        self.cfg = cfg
        self.root = Path(root)
        self.day = day
        self.compare_to = compare_to
        rc = cfg["rules"]
        self.overrides: dict[str, str] = rc["severity_overrides"] or {}
        self.slash_eq: bool = rc["trailing_slash_equivalent"]
        self.group_min: int = rc["group_min"]
        self.max_urls: int = rc["max_urls_per_finding"]
        self.th = cfg["thresholds"]
        self.findings: list[dict[str, Any]] = []
        self.ongoing: dict[tuple[str, str], list[str]] = {}

    def key(self, url: str | None) -> str | None:
        return url_key(url, self.slash_eq)

    def add(self, source: str, rule: str, change: str, *, url: str | None = None, old: Any = None,
            new: Any = None, message: str | None = None, evidence: str | None = None,
            compared_to: str | None = None, details: dict[str, Any] | None = None,
            unit: str = "") -> dict[str, Any]:
        if evidence is None:
            if change == "baseline":
                evidence = f"now: {_fmt(new, unit)}"
            elif old is not None or new is not None:
                evidence = f"{_fmt(old, unit)} -> {_fmt(new, unit)}"
            else:
                evidence = ""
        f = {
            "id": None,
            "severity": R.severity_for(rule, change, self.overrides),
            "source": source,
            "rule": rule,
            "check": R.check_for(rule),
            "change": change,
            "regression": change in R.REGRESSION_CHANGES,
            "url": url,
            "old_value": old,
            "new_value": new,
            "evidence": evidence,
            "evidence_source": f"{source}.json {self.day}" + (f" vs {compared_to}" if compared_to else ""),
            "compared_to": compared_to,
            "message": message or (f"{R.check_for(rule)}: {url}" if url else R.check_for(rule)),
            "suggested_next_step": R.next_step_for(rule, change),
            "count": 1,
        }
        if details:
            f["details"] = details
        self.findings.append(f)
        return f

    def ongoing_issue(self, rule: str, url: str | None) -> None:
        self.ongoing.setdefault((rule, R.severity_for(rule, "appeared", self.overrides)), []).append(url or "")


def _group(ctx: Ctx, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """More than rules.group_min findings with the same source, rule and change
    become one finding with count and a capped list of items."""
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for f in items:
        buckets.setdefault((f["source"], f["rule"], f["change"]), []).append(f)
    out: list[dict[str, Any]] = []
    for (source, rule, change), fs in buckets.items():
        if len(fs) <= ctx.group_min or source == "psi":
            out.extend(fs)
            continue
        first = fs[0]
        g = dict(first)
        g.pop("details", None)
        g.update(
            url=None, old_value=None, new_value=None, count=len(fs),
            message=f"{R.check_for(rule)}: {len(fs)} URLs" + (" (baseline)" if change == "baseline" else ""),
            evidence=f"e.g. {first['url']}: {first['evidence']}",
            items=[{"url": f["url"], "old_value": f["old_value"], "new_value": f["new_value"]}
                   for f in fs[:ctx.max_urls]],
        )
        if len(fs) > ctx.max_urls:
            g["items_truncated"] = len(fs) - ctx.max_urls
        out.append(g)
    return out


# ---- baseline lookup -------------------------------------------------------------

def _baseline(ctx: Ctx, source: str) -> tuple[str, dict[str, Any]] | None | str:
    """(day, record) of the comparison day, None for a baseline run, or an
    error text when --compare-to names a day where the source was not ok."""
    if ctx.compare_to:
        try:
            status = read_meta(ctx.root, ctx.compare_to).get("sources", {}).get(source, {}).get("status")
        except SnapshotError as e:
            return f"compare-to day {ctx.compare_to} unreadable: {e}"
        rec = read_source(ctx.root, ctx.compare_to, source) if status == "ok" else None
        if rec is None:
            return f"{source} was {status or 'missing'} on compare-to day {ctx.compare_to}, so it was not compared"
        return ctx.compare_to, rec
    return last_day_with_status(ctx.root, source, before=ctx.day)


# ---- robots ------------------------------------------------------------------------

def diff_robots(ctx: Ctx, today: dict[str, Any], prev: dict[str, Any] | None, pday: str | None) -> None:
    allowed = today.get("allowed") or {}
    if prev is None:
        for url, ok in allowed.items():
            if not ok:
                ctx.add("robots", "robots_blocks_url", "baseline", url=url, new="disallowed")
        if today.get("mode") == "disallow_all":
            ctx.add("robots", "robots_mode_changed", "baseline", url=today.get("url"), new=today.get("mode"),
                    message=f"robots.txt is disallow_all: {today.get('error') or ''}".strip())
        return
    old_allowed = prev.get("allowed") or {}
    for url, ok in allowed.items():
        was = old_allowed.get(url)
        if was is True and ok is False:
            ctx.add("robots", "robots_blocks_url", "appeared", url=url, old="allowed", new="disallowed", compared_to=pday)
        elif was is False and ok is True:
            ctx.add("robots", "robots_blocks_url", "resolved", url=url, old="disallowed", new="allowed", compared_to=pday)
        elif ok is False:
            ctx.ongoing_issue("robots_blocks_url", url)
    if today.get("mode") != prev.get("mode"):
        if today.get("mode") == "disallow_all":
            rule, change = "robots_mode_changed", "appeared"
        elif prev.get("mode") == "disallow_all":
            rule, change = "robots_mode_changed", "resolved"
        else:  # parsed <-> allow_all (robots.txt appeared or went 404): not an alarm
            rule, change = "robots_mode_info", "changed"
        ctx.add("robots", rule, change, url=today.get("url"), old=prev.get("mode"), new=today.get("mode"),
                compared_to=pday)
    if sorted(today.get("sitemaps_declared") or []) != sorted(prev.get("sitemaps_declared") or []):
        ctx.add("robots", "robots_sitemaps_changed", "changed", url=today.get("url"),
                old=sorted(prev.get("sitemaps_declared") or []), new=sorted(today.get("sitemaps_declared") or []),
                compared_to=pday)


# ---- sitemap ----------------------------------------------------------------------

def _sitemap_urls(rec: dict[str, Any]) -> dict[str, str | None]:
    urls: dict[str, str | None] = {}
    for s in rec.get("sitemaps") or []:
        urls.update(s.get("urls") or {})
    return urls


def diff_sitemap(ctx: Ctx, today: dict[str, Any], prev: dict[str, Any] | None, pday: str | None,
                 notes: list[str]) -> None:
    now = _sitemap_urls(today)
    if prev is None:
        notes.append(f"baseline: {len(now)} sitemap URLs recorded")
        return
    old = _sitemap_urls(prev)
    added = sorted(set(now) - set(old))
    if added:
        ctx.add("sitemap", "sitemap_urls_added", "changed", old=len(old), new=len(now), compared_to=pday,
                message=f"{len(added)} new URL(s) in the sitemap",
                evidence=f"{len(old)} -> {len(now)} URLs; +{len(added)}",
                details={"urls": added[:ctx.max_urls], "count": len(added)})
    if today["status"] != "ok":
        notes.append("sitemap partial today: removed URLs not computed (missing files would look like removals)")
    else:
        removed = sorted(set(old) - set(now))
        if removed:
            share = 100.0 * len(removed) / max(1, len(old))
            rule = "sitemap_urls_dropped" if share > ctx.th["sitemap_drop_percent"] else "sitemap_urls_removed"
            ctx.add("sitemap", rule, "removed", old=len(old), new=len(now), compared_to=pday,
                    message=f"{len(removed)} URL(s) removed from the sitemap ({share:.1f}% of {len(old)})",
                    evidence=f"{len(old)} -> {len(now)} URLs; -{len(removed)}",
                    details={"urls": removed[:ctx.max_urls], "count": len(removed), "percent": round(share, 1)})
    changed = sorted(u for u in set(now) & set(old) if now[u] != old[u])
    if changed:
        ctx.add("sitemap", "sitemap_lastmod_changed", "changed", compared_to=pday,
                message=f"{len(changed)} sitemap URL(s) have a new lastmod",
                evidence=f"e.g. {changed[0]}: {old[changed[0]]} -> {now[changed[0]]}",
                details={"urls": changed[:ctx.max_urls], "count": len(changed)})


# ---- crawl ---------------------------------------------------------------------------

def _norm_text(s: str | None) -> str | None:
    return " ".join(s.split()).casefold() if s else None


def _dup_counts(pages: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in pages:
        seo = p.get("seo")
        if p.get("status") == "ok" and seo:
            k = _norm_text(seo.get(field))
            if k:
                counts[k] = counts.get(k, 0) + 1
    return counts


def _status_map(ctx: Ctx, crawl: dict[str, Any] | None, links: dict[str, Any] | None) -> dict[str, Any]:
    """URL key -> final HTTP status (int) or 'failed', from crawl pages and link results."""
    out: dict[str, Any] = {}
    for lk in (links or {}).get("links") or []:
        if lk.get("status") == "failed":
            out[ctx.key(lk["url"])] = "failed"
        elif lk.get("status") == "ok":
            out[ctx.key(lk["url"])] = lk.get("http_status")
    for p in (crawl or {}).get("pages") or []:
        if p.get("status") == "ok":
            out[ctx.key(p["url"])] = p.get("http_status")
            if p.get("final_url"):
                out[ctx.key(p["final_url"])] = p.get("http_status")
    return out


def _is_bad(status: Any) -> bool:
    return status == "failed" or (isinstance(status, int) and status >= 400)


def page_issues(ctx: Ctx, p: dict[str, Any], dups: dict[str, dict[str, int]],
                statuses: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Issues on one page: {rule: value}, plus whether its canonical differs
    from the page URL only by a trailing slash."""
    out: dict[str, Any] = {}
    st = p.get("status")
    if st == "failed":
        return {"page_fetch_failed": p.get("error")}, False
    if st == "disallowed":
        return {"page_disallowed": "disallowed by robots.txt"}, False
    if st != "ok":
        return {}, False
    code = p.get("http_status")
    if isinstance(code, int) and code >= 400:
        out["page_http_error"] = code
    if p.get("redirect_chain"):
        out["page_redirects"] = p.get("final_url")
    if p.get("noindex"):
        out["page_noindex"] = ", ".join(p.get("noindex_sources") or []) or "noindex"
    seo = p.get("seo")
    if not seo:
        return out, False
    th = ctx.th
    title = seo.get("title")
    if not title:
        out["title_missing"] = title
    else:
        if not th["title_length"]["min"] <= len(title) <= th["title_length"]["max"]:
            out["title_length"] = len(title)
        n = dups["title"].get(_norm_text(title) or "", 0)
        if n > 1:
            out["title_duplicate"] = n
    desc = seo.get("meta_description")
    if not desc:
        out["meta_description_missing"] = desc
    else:
        rng = th["meta_description_length"]
        if not rng["min"] <= len(desc) <= rng["max"]:
            out["meta_description_length"] = len(desc)
        n = dups["meta_description"].get(_norm_text(desc) or "", 0)
        if n > 1:
            out["meta_description_duplicate"] = n
    h1 = seo.get("h1") or []
    if not [h for h in h1 if h]:
        out["h1_missing"] = len(h1)
    elif len(h1) > 1:
        out["h1_multiple"] = len(h1)
    canon = seo.get("canonical")
    slash_only = False
    page_url = p.get("final_url") or p["url"]
    if not canon:
        out["canonical_missing"] = None
    else:
        if (seo.get("canonical_count") or 0) > 1:
            out["canonical_multiple"] = seo.get("canonical_count")
        if canon != page_url:
            if ctx.key(canon) == ctx.key(page_url):
                slash_only = True
            else:
                out["canonical_other"] = canon
        target = statuses.get(ctx.key(canon))
        if _is_bad(target):
            out["canonical_target_broken"] = f"{canon} ({target})"
    return out, slash_only


# value shown as old/new for an issue, read from either day's page
def _page_value(rule: str, p: dict[str, Any] | None) -> Any:
    if p is None:
        return None
    seo = p.get("seo") or {}
    if rule in ("page_fetch_failed", "page_disallowed"):
        return p.get("status")
    if rule == "page_http_error":
        return p.get("http_status")
    if rule == "page_redirects":
        return p.get("final_url")
    if rule == "page_noindex":
        return ", ".join(p.get("noindex_sources") or []) or "indexable"
    if rule in ("title_missing", "title_duplicate"):
        return seo.get("title")
    if rule == "title_length":
        return len(seo["title"]) if seo.get("title") else None
    if rule in ("meta_description_missing", "meta_description_duplicate"):
        return seo.get("meta_description")
    if rule == "meta_description_length":
        return len(seo["meta_description"]) if seo.get("meta_description") else None
    if rule in ("h1_missing", "h1_multiple"):
        return len(seo.get("h1") or [])
    if rule in ("canonical_missing", "canonical_other", "canonical_target_broken"):
        return seo.get("canonical")
    if rule == "canonical_multiple":
        return seo.get("canonical_count")
    return None


def _crawl_day(ctx: Ctx, rec: dict[str, Any], links: dict[str, Any] | None):
    pages = rec.get("pages") or []
    dups = {"title": _dup_counts(pages, "title"), "meta_description": _dup_counts(pages, "meta_description")}
    statuses = _status_map(ctx, rec, links)
    by_key: dict[str, dict[str, Any]] = {}
    issues: dict[str, dict[str, Any]] = {}
    slash: list[str] = []
    for p in pages:
        k = ctx.key(p["url"])
        by_key[k] = p
        iss, slash_only = page_issues(ctx, p, dups, statuses)
        issues[k] = iss
        if slash_only:
            slash.append(p["url"])
    return by_key, issues, slash


def diff_crawl(ctx: Ctx, today: dict[str, Any], prev: dict[str, Any] | None, pday: str | None,
               today_links: dict[str, Any] | None, notes: list[str]) -> None:
    now_pages, now_issues, now_slash = _crawl_day(ctx, today, today_links)
    found: list[dict[str, Any]] = []

    def add(rule: str, change: str, url: str, old: Any, new: Any) -> None:
        before = len(ctx.findings)
        ctx.add("crawl", rule, change, url=url, old=old, new=new, compared_to=pday)
        found.extend(ctx.findings[before:])
        del ctx.findings[before:]

    if prev is None:
        for k, iss in now_issues.items():
            for rule, val in iss.items():
                add(rule, "baseline", now_pages[k]["url"], None, _page_value(rule, now_pages[k]) if val is None else val)
        if now_slash:
            ctx.add("crawl", "canonical_slash_mismatch", "baseline", new=len(now_slash),
                    message=f"{len(now_slash)} crawled page(s): canonical differs from the page URL only by a trailing slash",
                    evidence=f"now: {len(now_slash)} page(s), e.g. {now_slash[0]}",
                    details={"urls": now_slash[:ctx.max_urls], "count": len(now_slash)})
        ctx.findings.extend(_group(ctx, found))
        notes.append(f"baseline: {len(now_pages)} crawled page(s) recorded")
        return

    prev_links = read_source(ctx.root, pday, "links") if pday else None
    old_pages, old_issues, old_slash = _crawl_day(ctx, prev, prev_links)
    new_count = 0
    for k, p in now_pages.items():
        iss = now_issues[k]
        q = old_pages.get(k)
        q_comparable = q is not None and q.get("status") in ("ok", "failed", "disallowed")
        if p.get("status") == "skipped":
            continue
        if not q_comparable:
            new_count += 1
            for rule, val in iss.items():
                add(rule, "new_page", p["url"], None, _page_value(rule, p) if val is None else val)
            continue
        old_iss = old_issues.get(k, {})
        for rule, val in iss.items():
            if rule in old_iss:
                ctx.ongoing_issue(rule, p["url"])
            else:
                add(rule, "appeared", p["url"], _page_value(rule, q), _page_value(rule, p) if val is None else val)
        for rule in old_iss:
            if rule not in iss:
                add(rule, "resolved", p["url"], _page_value(rule, q), _page_value(rule, p))
        so, sn = q.get("seo"), p.get("seo")
        if so and sn and p.get("status") == "ok" and q.get("status") == "ok":
            for field, rule in (("title", "title_changed"), ("meta_description", "meta_description_changed")):
                if so.get(field) and sn.get(field) and so[field] != sn[field]:
                    add(rule, "changed", p["url"], so[field], sn[field])
            if so.get("h1") and sn.get("h1") and so["h1"] != sn["h1"]:
                add("h1_changed", "changed", p["url"], so["h1"], sn["h1"])
            if so.get("canonical") and sn.get("canonical") and ctx.key(so["canonical"]) != ctx.key(sn["canonical"]):
                add("canonical_changed", "changed", p["url"], so["canonical"], sn["canonical"])
    if new_count:
        notes.append(f"{new_count} page(s) were not in the {pday} crawl (new in the newest-N window): "
                     "their issues are reported as change new_page")

    if now_slash and not old_slash:
        ctx.add("crawl", "canonical_slash_mismatch", "appeared", old=0, new=len(now_slash), compared_to=pday,
                message=f"{len(now_slash)} crawled page(s): canonical differs from the page URL only by a trailing slash",
                details={"urls": now_slash[:ctx.max_urls], "count": len(now_slash)})
    elif old_slash and not now_slash:
        ctx.add("crawl", "canonical_slash_mismatch", "resolved", old=len(old_slash), new=0, compared_to=pday,
                message="no crawled page has a trailing-slash-only canonical mismatch any more")
    elif now_slash:
        ctx.ongoing.setdefault(("canonical_slash_mismatch", R.severity_for("canonical_slash_mismatch", "appeared",
                                                                          ctx.overrides)), []).extend(now_slash)
    ctx.findings.extend(_group(ctx, found))


# ---- links -----------------------------------------------------------------------------

def _link_value(lk: dict[str, Any] | None) -> Any:
    if lk is None:
        return None
    if lk.get("status") == "failed":
        return lk.get("error") or "failed"
    return lk.get("http_status")


def diff_links(ctx: Ctx, today: dict[str, Any], prev: dict[str, Any] | None, pday: str | None) -> None:
    found: list[dict[str, Any]] = []

    def add(rule: str, change: str, lk: dict[str, Any], old: Any, new: Any) -> None:
        before = len(ctx.findings)
        ctx.add("links", rule, change, url=lk["url"], old=old, new=new, compared_to=pday,
                details={"linked_from": (lk.get("linked_from") or [])[:5],
                         "linked_from_count": lk.get("linked_from_count"),
                         "final_url": lk.get("final_url")})
        found.extend(ctx.findings[before:])
        del ctx.findings[before:]

    old = {lk["url"]: lk for lk in (prev or {}).get("links") or []}
    for lk in today.get("links") or []:
        if lk.get("source") == "crawl":  # crawled pages: reported by the crawl source
            continue
        if lk.get("status") not in ("ok", "failed"):
            continue
        q = old.get(lk["url"])
        q_known = q is not None and q.get("status") in ("ok", "failed")
        broken = is_broken(lk)
        if prev is None:
            if broken:
                add("link_broken", "baseline", lk, None, _link_value(lk))
            continue
        if broken:
            if q_known and is_broken(q):
                ctx.ongoing_issue("link_broken", lk["url"])
            else:
                add("link_broken", "appeared", lk, _link_value(q) if q_known else "not checked", _link_value(lk))
        elif q_known and is_broken(q):
            add("link_broken", "resolved", lk, _link_value(q), _link_value(lk))
        elif lk.get("redirect_chain") and q_known and not q.get("redirect_chain"):
            add("link_redirects", "appeared", lk, q.get("final_url"), lk.get("final_url"))
    ctx.findings.extend(_group(ctx, found))


# ---- PSI ---------------------------------------------------------------------------------

def _psi_history(ctx: Ctx, today: dict[str, Any]) -> dict[tuple[str, str], list[tuple[str, dict[str, Any]]]]:
    """(url, strategy) -> [(day, run)] of ok runs, oldest first, today last."""
    hist: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = {}
    days = [d for d in list_snapshot_days(ctx.root) if d < ctx.day and (not ctx.compare_to or d >= ctx.compare_to)]
    for d in days:
        try:
            rec = read_source(ctx.root, d, "psi")
        except SnapshotError:
            continue
        for r in (rec or {}).get("runs") or []:
            if r.get("status") == "ok":
                hist.setdefault((r["url"], r["strategy"]), []).append((d, r))
    for r in today.get("runs") or []:
        if r.get("status") == "ok":
            hist.setdefault((r["url"], r["strategy"]), []).append((ctx.day, r))
    return hist


def lab_state(seq: list[tuple[str, dict[str, Any]]], metric: str, threshold: float, sign: int,
              needed: int) -> dict[str, Any] | None:
    """Replay the ok runs and return the event of the last run (today):
    None, or {event: pending|confirmed|cleared, ref_day, ref, value, streak}."""
    ref: tuple[str, float] | None = None
    streak = 0
    event: dict[str, Any] | None = None
    for day, run in seq:
        v = (run.get("lab") or {}).get(metric)
        event = None
        if v is None:
            continue
        if ref is None:
            ref = (day, v)
            continue
        if (v - ref[1]) * sign > threshold:
            streak += 1
            if streak >= needed:
                event = {"event": "confirmed", "ref_day": ref[0], "ref": ref[1], "value": v, "streak": streak}
                ref, streak = (day, v), 0
            else:
                event = {"event": "pending", "ref_day": ref[0], "ref": ref[1], "value": v, "streak": streak}
        else:
            if streak:
                event = {"event": "cleared", "ref_day": ref[0], "ref": ref[1], "value": v, "streak": streak}
            ref, streak = (day, v), 0
    return event


def _crux_compare(ctx: Ctx, scope: str, url: str, strategy: str, old: dict[str, Any] | None,
                  new: dict[str, Any] | None, pday: str | None, baseline: bool) -> None:
    label_scope = "URL" if scope == "url" else "origin"
    det = {"strategy": strategy, "scope": scope}
    if baseline:
        if not new:
            return
        for m, (label, unit) in CRUX_LABEL.items():
            cur = (new.get("metrics") or {}).get(m)
            if cur and cur.get("category") in ("SLOW", "AVERAGE"):
                rule = "crux_slow" if cur["category"] == "SLOW" else "crux_average"
                ctx.add("psi", rule, "baseline", url=url, new=f"{cur['percentile']}{unit} {cur['category']}",
                        message=f"{label} ({label_scope}, {strategy}) is {cur['category']}", details=det)
        return
    if bool(old) != bool(new):
        ctx.add("psi", "crux_data_changed", "changed", url=url, compared_to=pday,
                old="field data" if old else "no field data", new="field data" if new else "no field data",
                message=f"CrUX {label_scope} data ({strategy}) " + ("is no longer available" if old else "is available now"),
                details=det)
        return
    if not old or not new:
        return
    for m, (label, unit) in CRUX_LABEL.items():
        a, b = (old.get("metrics") or {}).get(m), (new.get("metrics") or {}).get(m)
        if not a or not b or a.get("category") not in CRUX_RANK or b.get("category") not in CRUX_RANK:
            continue
        ra, rb = CRUX_RANK[a["category"]], CRUX_RANK[b["category"]]
        o, n = f"{a['percentile']}{unit} {a['category']}", f"{b['percentile']}{unit} {b['category']}"
        msg = f"{label} ({label_scope}, {strategy})"
        if rb > ra:
            rule = "crux_poor" if (a["category"] == "FAST" and b["category"] == "SLOW") else "crux_worse"
            ctx.add("psi", rule, "worsened", url=url, old=o, new=n, compared_to=pday,
                    message=f"{msg} got worse: {a['category']} -> {b['category']}", details=det)
        elif rb < ra:
            ctx.add("psi", "crux_better", "improved", url=url, old=o, new=n, compared_to=pday,
                    message=f"{msg} improved: {a['category']} -> {b['category']}", details=det)
        elif b["category"] == "SLOW":
            ctx.ongoing_issue("crux_slow", f"{url} ({strategy}, {label_scope})")


def diff_psi(ctx: Ctx, today: dict[str, Any], prev: dict[str, Any] | None, pday: str | None,
             notes: list[str]) -> None:
    needed = ctx.cfg["psi"]["regression_confirmations"]
    hist = _psi_history(ctx, today)
    origin_done: set[str] = set()
    base = ctx.cfg["site"]["base_url"]
    origin_url = urlunsplit(urlsplit(base)[:2] + ("", "", ""))
    for r in today.get("runs") or []:
        if r.get("status") != "ok":
            continue
        key = (r["url"], r["strategy"])
        seq = hist.get(key, [])
        earlier = seq[:-1]
        strategy = r["strategy"]
        if not earlier:
            notes.append(f"baseline for {r['url']} ({strategy}): first ok PSI run")
        # lab metrics (confirmation)
        for metric, (rule, label, unit, sign, thr) in LAB_METRICS.items():
            ev = lab_state(seq, metric, thr(ctx.th), sign, needed)
            if ev is None or ev["event"] is None:
                continue
            det = {"strategy": strategy, "metric": metric, "reference_day": ev["ref_day"],
                   "streak": ev["streak"], "confirmations_needed": needed}
            where = f"{r['url']} ({strategy})"
            if ev["event"] == "pending":
                ctx.add("psi", rule, "pending", url=r["url"], old=ev["ref"], new=ev["value"], unit=unit,
                        compared_to=ev["ref_day"],
                        message=f"possible regression, awaiting confirmation ({ev['streak']} of {needed}): "
                                f"{label} {_fmt(ev['ref'], unit)} -> {_fmt(ev['value'], unit)} on {where}",
                        details=det)
                ctx.findings[-1]["suggested_next_step"] = (f"No action yet: reported as a regression only if the next "
                                            f"{needed - ev['streak']} run(s) are also worse.")
            elif ev["event"] == "confirmed":
                ctx.add("psi", rule, "confirmed", url=r["url"], old=ev["ref"], new=ev["value"], unit=unit,
                        compared_to=ev["ref_day"],
                        message=f"{label} regression confirmed over {ev['streak']} run(s): "
                                f"{_fmt(ev['ref'], unit)} -> {_fmt(ev['value'], unit)} on {where}",
                        details=det)
            else:
                ctx.add("psi", rule, "cleared", url=r["url"], old=ev["ref"], new=ev["value"], unit=unit,
                        compared_to=ev["ref_day"],
                        message=f"possible {label} regression did not repeat (cleared): now "
                                f"{_fmt(ev['value'], unit)} vs {_fmt(ev['ref'], unit)} on {where}",
                        details=det)
        # CrUX field data (direct compare with the previous ok run)
        prev_run = earlier[-1][1] if earlier else None
        prev_day = earlier[-1][0] if earlier else None
        field = r.get("field") or {}
        pfield = (prev_run or {}).get("field") or {}
        _crux_compare(ctx, "url", r["url"], strategy, pfield.get("url"), field.get("url"), prev_day, prev_run is None)
        if strategy not in origin_done:
            origin_done.add(strategy)
            _crux_compare(ctx, "origin", origin_url, strategy, pfield.get("origin"), field.get("origin"),
                          prev_day, prev_run is None)


# ---- orchestration -----------------------------------------------------------------------

def _sort_key(f: dict[str, Any]) -> tuple:
    src = R.SOURCES.index(f["source"]) if f["source"] in R.SOURCES else len(R.SOURCES)
    return (R.SEVERITY_RANK[f["severity"]], 0 if f["regression"] else 1, src, f["rule"], f["url"] or "")


def build_findings(cfg: dict[str, Any], root: str | Path, day: str, records: dict[str, Any] | None = None,
                   compare_to: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Findings document for `day`. records = today's source records (read
    from the snapshot when None)."""
    if parse_day(day) is None:
        raise SnapshotError(f"invalid date {day!r}")
    if compare_to is not None and (parse_day(compare_to) is None or compare_to >= day):
        raise SnapshotError(f"--compare-to must be a YYYY-MM-DD day before {day}, got {compare_to!r}")
    root = Path(root)
    if records is None:
        records = {k: v for k, v in read_snapshot(root, day).items() if k not in ("meta", "findings", "summary", "delivery")}
    ctx = Ctx(cfg, root, day, compare_to)
    prev_day_any = latest_previous_day(root, day)
    prev_meta = {}
    if prev_day_any:
        try:
            prev_meta = read_meta(root, prev_day_any).get("sources", {})
        except SnapshotError:
            prev_meta = {}

    sources: dict[str, dict[str, Any]] = {}
    failed: set[str] = set()
    for src in R.SOURCES:
        rec = records.get(src)
        info: dict[str, Any] = {"status": (rec or {}).get("status") or "missing", "error": (rec or {}).get("error"),
                                "compared_to": None, "baseline": False, "notes": []}
        sources[src] = info
        status = info["status"]
        if status == "disabled":
            info["notes"].append("disabled in config.yaml, not compared")
            continue
        if status not in ("ok", "partial"):
            failed.add(src)
            err = info["error"] or (f"no {src}.json in today's snapshot" if rec is None else "unknown error")
            up = UPSTREAM.get(src)
            if up in failed:
                ctx.add(src, "source_skipped_upstream", "failed", message=f"{src.upper()} not run: {up} failed ({err})",
                        new=status)
            else:
                rule = "source_failed_critical" if src in CRITICAL_SOURCES else "source_failed"
                ctx.add(src, rule, "failed", message=f"{src.upper()} failed: {err}", new=status,
                        evidence=f"{prev_meta.get(src, {}).get('status') or 'none'} -> {status}")
            continue
        if status == "partial":
            reasons = rec.get("partial_reasons") or ([rec["error"]] if rec.get("error") else [])
            ctx.add(src, "source_partial", "partial", new="partial",
                    message=f"{src.upper()} partial: " + "; ".join(reasons)[:500],
                    details={"reasons": reasons[:20]})
        if prev_meta.get(src, {}).get("status") == "failed":
            ctx.add(src, "source_recovered", "recovered", old="failed", new=status, compared_to=prev_day_any,
                    message=f"{src.upper()} works again (failed on {prev_day_any})")
        base = _baseline(ctx, src)
        if isinstance(base, str):
            info["notes"].append(base)
            continue
        pday, prev = base if base else (None, None)
        info["compared_to"] = pday
        info["baseline"] = prev is None
        if src == "robots":
            diff_robots(ctx, rec, prev, pday)
        elif src == "sitemap":
            diff_sitemap(ctx, rec, prev, pday, info["notes"])
        elif src == "crawl":
            diff_crawl(ctx, rec, prev, pday, records.get("links"), info["notes"])
        elif src == "links":
            diff_links(ctx, rec, prev, pday)
        elif src == "psi":
            diff_psi(ctx, rec, prev, pday, info["notes"])

    findings = sorted(ctx.findings, key=_sort_key)
    for i, f in enumerate(findings, 1):
        f["id"] = f"F{i:03d}"
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in R.SEVERITIES}
    compared = [s for s, i in sources.items() if i["status"] in ("ok", "partial")]
    baseline = bool(compared) and all(sources[s]["baseline"] for s in compared)
    if baseline:
        status = "baseline"
    elif counts["critical"] or counts["warning"]:
        status = "issues"
    else:
        status = "stable"
    ongoing = [{"rule": rule, "severity": sev, "check": R.check_for(rule), "count": len(urls),
                "urls": urls[:ctx.max_urls]}
               for (rule, sev), urls in sorted(ctx.ongoing.items(), key=lambda kv: (R.SEVERITY_RANK[kv[0][1]], kv[0][0]))]
    return {
        "schema_version": FINDINGS_SCHEMA_VERSION,
        "tool_version": __version__,
        "date": day,
        "generated_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds"),
        "site": {k: cfg["site"][k] for k in ("name", "base_url")},
        "compare_to": compare_to,
        "status": status,
        "all_stable": status == "stable",
        "headline": _headline(status, counts, sources, failed),
        "counts": counts,
        "sources": sources,
        "findings": findings,
        "ongoing": ongoing,
    }


def _headline(status: str, counts: dict[str, int], sources: dict[str, Any], failed: set[str]) -> str:
    fail = f"; failed sources: {', '.join(sorted(failed))}" if failed else ""
    if status == "baseline":
        return (f"Baseline run: no earlier snapshot to compare with; current issues listed "
                f"({counts['critical']} critical, {counts['warning']} warning, {counts['info']} info){fail}")
    if status == "stable":
        days = sorted({i["compared_to"] for i in sources.values() if i["compared_to"]})
        since = f" since {days[-1]}" if days else ""
        return f"All stable: no critical or warning changes{since} ({counts['info']} info){fail}"
    return f"{counts['critical']} critical, {counts['warning']} warning, {counts['info']} info{fail}"


def write_findings(root: str | Path, day: str, doc: dict[str, Any]) -> Path:
    folder = Path(root) / day
    if not (folder / "meta.json").is_file():
        raise SnapshotError(f"no snapshot for {day} in {root}")
    path = folder / "findings.json"
    _write_atomic(path, _dump(doc))
    return path


def format_findings(doc: dict[str, Any], limit: int = 40) -> list[str]:
    lines = [f"  findings: {doc['headline']}"]
    for src, i in doc["sources"].items():
        cmp = f"vs {i['compared_to']}" if i["compared_to"] else ("baseline" if i["baseline"] else "not compared")
        lines.append(f"    {src}: {i['status']}, {cmp}" + "".join(f"; {n}" for n in i["notes"]))
    for f in doc["findings"][:limit]:
        where = f" | {f['url']}" if f["url"] and f["url"] not in f["message"] else ""
        lines.append(f"    {f['id']} {f['severity'].upper()} {f['source']}/{f['rule']} [{f['change']}] "
                     f"{f['message']}{where} | {f['evidence']}")
    if len(doc["findings"]) > limit:
        lines.append(f"    ... {len(doc['findings']) - limit} more in findings.json")
    for o in doc["ongoing"]:
        lines.append(f"    ongoing: {o['rule']} ({o['severity']}) on {o['count']} item(s)")
    return lines
