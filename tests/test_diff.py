"""Diff engine and severity rules (no network).

Each test writes small hand-made snapshots (builders below) into a temp
snapshot folder and checks the findings document.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from seo_reviewer import diff as D
from seo_reviewer import snapshot as snap
from seo_reviewer.config import ConfigError, validate_config

BASE = "https://example.test"
HOME = f"{BASE}/"


# ---- builders -----------------------------------------------------------------

def page(path="/post-a", title="default", desc=None, h1=("Post A",),
         canonical="self", http=200, noindex=False, status="ok", redirect_to=None, canonical_count=1):
    url = BASE + path
    final = redirect_to or url
    if canonical == "self":
        canonical = final
    if title == "default":
        title = f"A good title for the blog post at {path}"
    if desc is None:
        desc = f"A meta description for {path} that is long enough to be inside the configured range."
    seo = None
    if status == "ok" and http == 200:
        seo = {"title": title, "title_count": 1, "meta_description": desc, "meta_description_count": 1,
               "h1": list(h1), "canonical": canonical, "canonical_raw": canonical,
               "canonical_count": canonical_count if canonical else 0, "meta_robots": [], "lang": "en",
               "links_total": 1, "internal_links": [], "internal_links_truncated": False}
    return {"url": url, "reason": "sitemap", "lastmod": None, "status": status,
            "error": None if status == "ok" else "boom", "http_status": http if status == "ok" else None,
            "final_url": final if status == "ok" else None,
            "redirect_chain": [{"url": url, "status": 301, "location": final}] if redirect_to else [],
            "content_type": "text/html", "x_robots_tag": [], "noindex": noindex,
            "noindex_sources": ["meta-robots"] if noindex else [], "seo": seo}


def crawl(pages, status="ok", reasons=()):
    return {"source": "crawl", "status": status, "error": "; ".join(reasons) or None,
            "partial_reasons": list(reasons), "pages": pages}


def sitemap(urls, status="ok"):
    return {"source": "sitemap", "status": status, "error": None if status == "ok" else "child failed",
            "partial_reasons": [] if status == "ok" else ["child failed"],
            "sitemaps": [{"url": f"{BASE}/sitemap.xml", "status": "ok", "urls": dict(urls)}] if status != "failed" else []}


def robots(allowed=None, mode="parsed", status="ok"):
    return {"source": "robots", "status": status, "error": None if status == "ok" else "timeout", "mode": mode,
            "url": f"{BASE}/robots.txt", "sitemaps_declared": [f"{BASE}/sitemap.xml"],
            "allowed": allowed if allowed is not None else {HOME: True, f"{BASE}/sitemap.xml": True}}


def link(path, http=200, status="ok", source="check", redirect=False):
    url = BASE + path
    return {"url": url, "status": status, "source": source, "http_status": http if status == "ok" else None,
            "final_url": url + "/" if redirect else url,
            "redirect_chain": [{"url": url, "status": 301, "location": url + "/"}] if redirect else [],
            "method": "HEAD", "error": None if status == "ok" else "timeout", "notes": [],
            "linked_from": [HOME], "linked_from_count": 3}


def links(items=(), status="ok"):
    return {"source": "links", "status": status, "error": None, "partial_reasons": [], "links": list(items)}


def crux(lcp="FAST", inp="FAST", cls="FAST"):
    def m(cat, val):
        return {"percentile": val, "category": cat} if cat else None
    return {"overall_category": "FAST", "metrics": {"lcp_ms": m(lcp, 1000), "inp_ms": m(inp, 100), "cls": m(cls, 0.01)}}


def run(strategy="mobile", score=96, lcp=2700, cls=0.0, tbt=0, status="ok", url_field="default", origin_field="default"):
    return {"url": HOME, "strategy": strategy, "status": status, "error": None if status == "ok" else "HTTP 500",
            "lab": {"performance_score": score, "lcp_ms": lcp, "cls": cls, "tbt_ms": tbt} if status == "ok" else None,
            "field": {"url": crux() if url_field == "default" else url_field,
                      "origin": crux() if origin_field == "default" else origin_field, "notes": []}
            if status == "ok" else None}


def psi(runs=None, status="ok", error=None):
    return {"status": status, "error": error, "partial_reasons": [], "runs": runs if runs is not None else [run()]}


def day_records(**over):
    rec = {
        "robots": robots(),
        "sitemap": sitemap({f"{BASE}/post-a": "2026-11-01", f"{BASE}/post-b": "2026-11-02"}),
        "crawl": crawl([page("/", title="Example Blog"), page("/post-a"), page("/post-b")]),
        "links": links([link("/tag/x"), link("/about")]),
        "psi": psi(),
    }
    rec.update(over)
    return rec


@pytest.fixture
def root(tmp_path):
    return tmp_path / "snapshots"


def write(cfg, root, day, records):
    meta = snap.build_meta(cfg, day, records, datetime(2026, 11, 1, tzinfo=timezone.utc))
    snap.write_snapshot(root, day, records, meta)


def findings_for(cfg, root, day, records, **kw):
    write(cfg, root, day, records)
    return D.build_findings(cfg, root, day, **kw)


def rules_of(doc, change=None):
    return [(f["rule"], f["change"]) for f in doc["findings"] if change is None or f["change"] == change]


def one(doc, rule, change=None):
    hits = [f for f in doc["findings"] if f["rule"] == rule and (change is None or f["change"] == change)]
    assert len(hits) == 1, (rule, change, rules_of(doc))
    return hits[0]


# ---- baseline and stable ------------------------------------------------------------

def test_first_run_is_baseline_with_current_issues(cfg, root):
    doc = findings_for(cfg, root, "2026-11-01", day_records())
    assert doc["status"] == "baseline" and not doc["all_stable"]
    assert doc["headline"].startswith("Baseline run")
    assert all(i["baseline"] for i in doc["sources"].values())
    f = one(doc, "title_length", "baseline")  # "Example Blog" = 12 chars
    assert f["url"] == HOME and f["new_value"] == 12 and f["evidence"] == "now: 12"
    assert f["suggested_next_step"]  # written by the rules
    assert "baseline: 2 sitemap URLs recorded" in doc["sources"]["sitemap"]["notes"]
    assert not [f for f in doc["findings"] if f["source"] == "sitemap"]
    assert [f["id"] for f in doc["findings"]] == [f"F{i:03d}" for i in range(1, len(doc["findings"]) + 1)]


def test_identical_day_is_all_stable_and_known_issues_are_ongoing(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    doc = findings_for(cfg, root, "2026-11-02", day_records())
    assert doc["status"] == "stable" and doc["all_stable"]
    assert doc["findings"] == []
    assert doc["headline"] == "All stable: no critical or warning changes since 2026-11-01 (0 info)"
    assert {o["rule"]: o["count"] for o in doc["ongoing"]} == {"title_length": 1}
    assert doc["sources"]["crawl"]["compared_to"] == "2026-11-01"


# ---- source failures ----------------------------------------------------------------

def test_failed_source_is_one_finding_and_no_per_url_findings(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    doc = findings_for(cfg, root, "2026-11-02", day_records(psi=psi(runs=[], status="failed", error="HTTP 403: blocked")))
    f = one(doc, "source_failed")
    assert f["severity"] == "warning" and f["message"] == "PSI failed: HTTP 403: blocked"
    assert [x for x in doc["findings"] if x["source"] == "psi"] == [f]
    assert doc["status"] == "issues"


def test_sitemap_failure_is_critical(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    rec = day_records(sitemap=sitemap({}, status="failed"))
    rec["sitemap"]["error"] = "https://example.test/sitemap.xml: HTTP 503"
    doc = findings_for(cfg, root, "2026-11-02", rec)
    f = one(doc, "source_failed_critical")
    assert f["severity"] == "critical" and f["message"].startswith("SITEMAP failed: ")
    assert doc["findings"][0] is f  # critical first


def test_dependent_failures_are_not_triple_reported(cfg, root):
    rec = day_records(robots=robots(status="failed"), crawl=crawl([], status="failed"),
                      links=links(status="failed"))
    doc = findings_for(cfg, root, "2026-11-01", rec)
    assert one(doc, "source_failed_critical")["source"] == "robots"
    skipped = [f for f in doc["findings"] if f["rule"] == "source_skipped_upstream"]
    assert {f["source"] for f in skipped} == {"crawl", "links"}
    assert all(f["severity"] == "info" for f in skipped)


def test_failed_day_is_skipped_as_baseline_and_recovery_is_reported(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    write(cfg, root, "2026-11-02", day_records(crawl=crawl([], status="failed")))
    doc = findings_for(cfg, root, "2026-11-03", day_records())
    assert doc["sources"]["crawl"]["compared_to"] == "2026-11-01"
    f = one(doc, "source_recovered")
    assert f["severity"] == "info" and f["source"] == "crawl"


def test_partial_source_gets_info_finding_and_is_still_compared(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    rec = day_records(crawl=crawl([page("/", title="Example Blog"), page("/post-a", http=404)], status="partial",
                                  reasons=["runtime.budget_seconds reached: 1 page(s) not fetched"]))
    doc = findings_for(cfg, root, "2026-11-02", rec)
    assert one(doc, "source_partial")["severity"] == "info"
    assert one(doc, "page_http_error", "appeared")["severity"] == "critical"


# ---- crawl rules --------------------------------------------------------------------

def test_page_regressions_severity_and_order(cfg, root):
    write(cfg, root, "2026-11-01", day_records(crawl=crawl([
        page("/", title="Example Blog"), page("/a"), page("/b"), page("/c"), page("/d", title=None)])))
    doc = findings_for(cfg, root, "2026-11-02", day_records(crawl=crawl([
        page("/", title="Example Blog"),
        page("/a", http=404),
        page("/b", noindex=True),
        page("/c", canonical=None, title="A changed title for this blog post"),
        page("/d", title="A brand new title for page d, fixed")])))
    http = one(doc, "page_http_error")
    assert (http["severity"], http["old_value"], http["new_value"]) == ("critical", 200, 404)
    assert http["evidence"] == "200 -> 404"
    assert one(doc, "page_noindex")["severity"] == "critical"
    canon = one(doc, "canonical_missing", "appeared")
    assert canon["severity"] == "critical" and canon["old_value"] == f"{BASE}/c"
    assert one(doc, "title_changed")["severity"] == "info"
    fixed = one(doc, "title_missing", "resolved")
    assert fixed["severity"] == "info" and fixed["suggested_next_step"] == "No action needed."
    sev = [f["severity"] for f in doc["findings"]]
    assert sev == sorted(sev, key=["critical", "warning", "info"].index)
    assert doc["status"] == "issues"


def test_new_page_in_window_reports_its_issues(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    rec = day_records(crawl=crawl([page("/", title="Example Blog"), page("/post-a"), page("/post-b"),
                                   page("/post-new", desc="")]))
    doc = findings_for(cfg, root, "2026-11-02", rec)
    f = one(doc, "meta_description_missing", "new_page")
    assert f["severity"] == "warning" and f["url"] == f"{BASE}/post-new" and f["regression"]
    assert any("not in the 2026-11-01 crawl" in n for n in doc["sources"]["crawl"]["notes"])


def test_trailing_slash_canonical_is_one_grouped_info(cfg, root):
    pages = [page(f"/p{i}", canonical=f"{BASE}/p{i}/") for i in range(3)]
    doc = findings_for(cfg, root, "2026-11-01", day_records(crawl=crawl(pages)))
    assert "canonical_other" not in [r for r, _ in rules_of(doc)]
    f = one(doc, "canonical_slash_mismatch", "baseline")
    assert f["severity"] == "info" and f["details"]["count"] == 3
    # next day, same state: ongoing, not repeated
    doc2 = findings_for(cfg, root, "2026-11-02", day_records(crawl=crawl(pages)))
    assert doc2["findings"] == []
    assert {o["rule"]: o["count"] for o in doc2["ongoing"]}["canonical_slash_mismatch"] == 3


def test_trailing_slash_strict_mode_flags_canonical_other(cfg, root):
    cfg["rules"]["trailing_slash_equivalent"] = False
    doc = findings_for(cfg, root, "2026-11-01", day_records(crawl=crawl([page("/p", canonical=f"{BASE}/p/")])))
    assert one(doc, "canonical_other")["severity"] == "warning"


def test_url_key():
    assert D.url_key("HTTPS://Example.test/a/#x", True) == "https://example.test/a"
    assert D.url_key("https://example.test/", True) == "https://example.test/"
    assert D.url_key("https://example.test/a/", False) == "https://example.test/a/"
    assert D.url_key(None, True) is None


def test_canonical_target_broken_uses_link_results(cfg, root):
    rec = day_records(crawl=crawl([page("/a", canonical=f"{BASE}/gone/")]), links=links([link("/gone/", http=404)]))
    doc = findings_for(cfg, root, "2026-11-01", rec)
    assert one(doc, "canonical_target_broken")["severity"] == "warning"


def test_duplicates_and_multiple_h1(cfg, root):
    rec = day_records(crawl=crawl([page("/a", title="Same title used on two pages ok"),
                                   page("/b", title="Same title used on two pages ok", h1=("x", "y"))]))
    doc = findings_for(cfg, root, "2026-11-01", rec)
    assert len([f for f in doc["findings"] if f["rule"] == "title_duplicate"]) == 2
    assert one(doc, "h1_multiple")["new_value"] == 2


def test_mass_change_is_grouped(cfg, root):
    old = [page(f"/p{i}", title=f"Old title number {i} for the blog post") for i in range(7)]
    new = [page(f"/p{i}", title=f"New title number {i} for the blog post") for i in range(7)]
    write(cfg, root, "2026-11-01", day_records(crawl=crawl(old)))
    doc = findings_for(cfg, root, "2026-11-02", day_records(crawl=crawl(new)))
    f = one(doc, "title_changed")
    assert f["count"] == 7 and len(f["items"]) == 7 and f["url"] is None
    assert f["message"] == "title changed: 7 URLs"


def test_redirect_and_fetch_failure(cfg, root):
    write(cfg, root, "2026-11-01", day_records(crawl=crawl([page("/a"), page("/b")])))
    doc = findings_for(cfg, root, "2026-11-02", day_records(crawl=crawl([
        page("/a", redirect_to=f"{BASE}/a-new"), page("/b", status="failed")], status="partial", reasons=["1 failed"])))
    assert one(doc, "page_redirects")["severity"] == "warning"
    assert one(doc, "page_fetch_failed")["severity"] == "warning"


# ---- sitemap rules ------------------------------------------------------------------

def test_sitemap_added_removed_lastmod(cfg, root):
    old = {f"{BASE}/p{i}": "2026-11-01" for i in range(20)}
    write(cfg, root, "2026-11-01", day_records(sitemap=sitemap(old)))
    new = dict(old)
    new.pop(f"{BASE}/p0")
    new[f"{BASE}/p1"] = "2026-11-02"
    new[f"{BASE}/fresh"] = "2026-11-02"
    doc = findings_for(cfg, root, "2026-11-02", day_records(sitemap=sitemap(new)))
    added = one(doc, "sitemap_urls_added")
    assert added["details"]["urls"] == [f"{BASE}/fresh"] and added["severity"] == "info"
    removed = one(doc, "sitemap_urls_removed")
    assert removed["details"]["urls"] == [f"{BASE}/p0"] and removed["severity"] == "info"
    assert one(doc, "sitemap_lastmod_changed")["details"]["count"] == 1
    assert doc["status"] == "stable"


def test_sitemap_big_drop_is_warning_and_partial_skips_removals(cfg, root):
    old = {f"{BASE}/p{i}": None for i in range(20)}
    write(cfg, root, "2026-11-01", day_records(sitemap=sitemap(old)))
    half = {f"{BASE}/p{i}": None for i in range(10)}
    doc = findings_for(cfg, root, "2026-11-02", day_records(sitemap=sitemap(half)))
    f = one(doc, "sitemap_urls_dropped")
    assert f["severity"] == "warning" and f["details"]["percent"] == 50.0
    doc = findings_for(cfg, root, "2026-11-03", day_records(sitemap=sitemap(half, status="partial")))
    assert doc["sources"]["sitemap"]["compared_to"] == "2026-11-02"  # last ok day
    assert not [x for x in doc["findings"] if x["rule"].startswith("sitemap_urls_d") or x["rule"] == "sitemap_urls_removed"]
    assert any("removed URLs not computed" in n for n in doc["sources"]["sitemap"]["notes"])


# ---- robots rules --------------------------------------------------------------------

def test_robots_now_blocks_homepage_is_critical(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    doc = findings_for(cfg, root, "2026-11-02", day_records(robots=robots({HOME: False, f"{BASE}/sitemap.xml": True})))
    f = one(doc, "robots_blocks_url")
    assert f["severity"] == "critical" and f["url"] == HOME and f["evidence"] == "'allowed' -> 'disallowed'"


def test_robots_mode_disallow_all_is_critical(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    doc = findings_for(cfg, root, "2026-11-02", day_records(robots=robots(mode="disallow_all")))
    assert one(doc, "robots_mode_changed")["severity"] == "critical"


# ---- links rules ---------------------------------------------------------------------

def test_new_broken_link_ongoing_and_fixed(cfg, root):
    write(cfg, root, "2026-11-01", day_records(links=links([link("/a"), link("/b", http=404), link("/c")])))
    doc = findings_for(cfg, root, "2026-11-02", day_records(links=links([
        link("/a", http=500), link("/b", http=404), link("/c", redirect=True),
        link("/post-a", http=404, source="crawl")])))
    new = one(doc, "link_broken", "appeared")
    assert new["url"] == f"{BASE}/a" and new["severity"] == "warning" and new["evidence"] == "200 -> 500"
    assert new["details"]["linked_from_count"] == 3
    assert one(doc, "link_redirects")["severity"] == "info"
    assert {o["rule"]: o["count"] for o in doc["ongoing"]}["link_broken"] == 1
    assert not [f for f in doc["findings"] if f["url"] == f"{BASE}/post-a"]  # crawl reports crawled pages
    doc = findings_for(cfg, root, "2026-11-03", day_records(links=links([link("/a"), link("/b"), link("/c")])))
    assert {f["url"] for f in doc["findings"] if f["rule"] == "link_broken" and f["change"] == "resolved"} == {
        f"{BASE}/a", f"{BASE}/b"}


def test_link_unchecked_yesterday_counts_as_new(cfg, root):
    write(cfg, root, "2026-11-01", day_records(links=links([link("/a")])))
    doc = findings_for(cfg, root, "2026-11-02", day_records(links=links([link("/a"), link("/z", status="failed")])))
    f = one(doc, "link_broken", "appeared")
    assert f["old_value"] == "not checked" and f["new_value"] == "timeout"


# ---- PSI lab confirmation -------------------------------------------------------------

def psi_day(cfg, root, day, **kw):
    return findings_for(cfg, root, day, day_records(psi=psi([run(**kw)])))


def test_lab_regression_needs_two_runs(cfg, root):
    psi_day(cfg, root, "2026-11-01", score=96)
    doc = psi_day(cfg, root, "2026-11-02", score=80)
    f = one(doc, "psi_performance_score", "pending")
    assert f["severity"] == "info"
    assert f["message"].startswith("possible regression, awaiting confirmation (1 of 2)")
    assert doc["status"] == "stable"
    doc = psi_day(cfg, root, "2026-11-03", score=81)
    f = one(doc, "psi_performance_score", "confirmed")
    assert f["severity"] == "warning" and (f["old_value"], f["new_value"]) == (96, 81)
    assert f["compared_to"] == "2026-11-01"
    doc = psi_day(cfg, root, "2026-11-04", score=80)  # new normal: not repeated
    assert not [x for x in doc["findings"] if x["rule"] == "psi_performance_score"]


def test_lab_recovery_clears_pending(cfg, root):
    psi_day(cfg, root, "2026-11-01", lcp=2700)
    one(psi_day(cfg, root, "2026-11-02", lcp=3500), "psi_lcp", "pending")
    doc = psi_day(cfg, root, "2026-11-03", lcp=2750)
    f = one(doc, "psi_lcp", "cleared")
    assert f["severity"] == "info"
    assert not [x for x in psi_day(cfg, root, "2026-11-04", lcp=3500)["findings"] if x["change"] == "confirmed"]


def test_failed_psi_day_neither_advances_nor_resets(cfg, root):
    psi_day(cfg, root, "2026-11-01", tbt=0)
    one(psi_day(cfg, root, "2026-11-02", tbt=500), "psi_tbt", "pending")
    findings_for(cfg, root, "2026-11-03", day_records(psi=psi([run(status="failed")], status="failed", error="HTTP 500")))
    doc = psi_day(cfg, root, "2026-11-04", tbt=480)
    f = one(doc, "psi_tbt", "confirmed")
    assert f["details"]["streak"] == 2


def test_noise_below_tolerance_is_ignored(cfg, root):
    psi_day(cfg, root, "2026-11-01", score=96, lcp=2700, cls=0.0, tbt=0)
    doc = psi_day(cfg, root, "2026-11-02", score=90, lcp=2900, cls=0.01, tbt=90)
    assert doc["findings"] == []


def test_one_confirmation_reports_immediately(cfg, root):
    cfg["psi"]["regression_confirmations"] = 1
    psi_day(cfg, root, "2026-11-01", cls=0.0)
    assert one(psi_day(cfg, root, "2026-11-02", cls=0.2), "psi_cls", "confirmed")["severity"] == "warning"


def test_strategies_are_tracked_separately(cfg, root):
    findings_for(cfg, root, "2026-11-01", day_records(psi=psi([run("mobile", score=96), run("desktop", score=100)])))
    doc = findings_for(cfg, root, "2026-11-02", day_records(psi=psi([run("mobile", score=96), run("desktop", score=70)])))
    f = one(doc, "psi_performance_score", "pending")
    assert f["details"]["strategy"] == "desktop"


# ---- CrUX ----------------------------------------------------------------------------

def test_crux_good_to_poor_is_critical_and_compared_directly(cfg, root):
    psi_day(cfg, root, "2026-11-01")
    doc = psi_day(cfg, root, "2026-11-02", url_field=crux(lcp="SLOW", inp="AVERAGE"),
                  origin_field=crux(cls="AVERAGE"))
    poor = one(doc, "crux_poor")
    assert poor["severity"] == "critical" and poor["details"]["scope"] == "url"
    assert poor["evidence"] == "'1000 ms FAST' -> '1000 ms SLOW'"
    worse = [f for f in doc["findings"] if f["rule"] == "crux_worse"]
    assert {(f["details"]["scope"], f["message"].split(" (")[0]) for f in worse} == {
        ("url", "CrUX INP"), ("origin", "CrUX CLS")}
    doc = psi_day(cfg, root, "2026-11-03")
    assert {f["rule"] for f in doc["findings"]} == {"crux_better"}


def test_crux_data_disappearing_and_origin_once_per_strategy(cfg, root):
    findings_for(cfg, root, "2026-11-01", day_records(psi=psi([run()])))
    doc = findings_for(cfg, root, "2026-11-02", day_records(psi=psi([run(url_field=None, origin_field=crux(lcp="AVERAGE"))])))
    f = one(doc, "crux_data_changed")
    assert f["message"] == "CrUX URL data (mobile) is no longer available"
    assert one(doc, "crux_worse")["url"] == BASE


def test_crux_baseline_lists_poor_metrics(cfg, root):
    doc = psi_day(cfg, root, "2026-11-01", url_field=crux(lcp="SLOW"))
    assert one(doc, "crux_slow", "baseline")["severity"] == "warning"


# ---- compare-to, overrides, config ------------------------------------------------------

def test_compare_to_uses_that_day(cfg, root):
    write(cfg, root, "2026-11-01", day_records(crawl=crawl([page("/a", http=404)])))
    write(cfg, root, "2026-11-02", day_records(crawl=crawl([page("/a")])))
    doc = findings_for(cfg, root, "2026-11-03", day_records(crawl=crawl([page("/a")])), compare_to="2026-11-01")
    assert doc["compare_to"] == "2026-11-01"
    assert one(doc, "page_http_error", "resolved")["compared_to"] == "2026-11-01"
    with pytest.raises(snap.SnapshotError):
        D.build_findings(cfg, root, "2026-11-03", compare_to="2026-11-05")


def test_severity_override(cfg, root):
    cfg["rules"]["severity_overrides"] = {"title_length": "warning"}
    doc = findings_for(cfg, root, "2026-11-01", day_records())
    assert one(doc, "title_length")["severity"] == "warning"


def test_config_rejects_bad_overrides(cfg):
    bad = copy.deepcopy(cfg)
    bad["rules"]["severity_overrides"] = {"nope": "info", "title_length": "urgent"}
    with pytest.raises(ConfigError) as e:
        validate_config(bad)
    assert "unknown rule 'nope'" in str(e.value) and "title_length: must be one of" in str(e.value)
    ok = copy.deepcopy(cfg)
    ok["rules"]["severity_overrides"] = None
    assert validate_config(ok)["rules"]["severity_overrides"] == {}


def test_findings_file_roundtrip(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    doc = D.build_findings(cfg, root, "2026-11-01")
    path = D.write_findings(root, "2026-11-01", doc)
    assert path.name == "findings.json"
    assert snap.read_source(root, "2026-11-01", "findings")["status"] == "baseline"
    lines = "\n".join(D.format_findings(doc))
    assert "Baseline run" in lines and "title_length" in lines
    with pytest.raises(snap.SnapshotError):
        D.write_findings(root, "2026-11-09", doc)


def test_every_rule_has_severity_check_and_next_step():
    from seo_reviewer.rules import RULES, SEVERITIES
    for rid, r in RULES.items():
        assert r["severity"] in SEVERITIES and r["check"] and r["next_step"], rid
