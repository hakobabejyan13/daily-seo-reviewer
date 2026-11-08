"""Report builder (no network).

Findings documents are built by the real diff engine from small hand-made
snapshots (builders from test_diff), so the report is tested against the
exact shape diff.py writes.
"""

from __future__ import annotations

import re

import pytest

from seo_reviewer import report as RP
from seo_reviewer import snapshot as snap
from seo_reviewer.snapshot import SnapshotError

from test_diff import (BASE, HOME, crawl, crux, day_records, findings_for, link, links, page, psi, run,
                       sitemap, write)
from seo_reviewer import diff as D


@pytest.fixture
def root(tmp_path):
    return tmp_path / "snapshots"


def report_for(cfg, root, day, records, **kw):
    doc = findings_for(cfg, root, day, records, **kw)
    D.write_findings(root, day, doc)
    inputs = RP.load_inputs(root, day)
    return doc, RP.build_report(inputs["findings"], inputs["meta"], inputs["records"])


def numbers(text):
    return set(re.findall(r"\d+(?:\.\d+)?", text))


# ---- modes ------------------------------------------------------------------------

def test_baseline_report_says_so_and_lists_current_issues(cfg, root):
    doc, md = report_for(cfg, root, "2026-11-01", day_records())
    assert doc["status"] == "baseline"
    assert md.startswith("# SEO review: Example, 2026-11-01\n")
    assert "**Baseline run: no earlier snapshot" in md and "- Status: Baseline run (`baseline`)" in md
    assert "This is a baseline run" in md
    assert "Compared with: nothing yet (first run)" in md
    assert "## Source health" in md and "## Findings" in md
    f = next(x for x in doc["findings"] if x["rule"] == "title_length")
    assert f"#### {f['id']} · title length out of range" in md
    assert "- Evidence: `now: 12`" in md
    assert f["suggested_next_step"].split()[0] in md  # rules text, as written
    assert "baseline (no earlier ok day)" in md
    assert "## Known issues" not in md  # nothing is ongoing on a first run


def test_stable_report_is_short(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    rec = day_records(sitemap=sitemap({f"{BASE}/post-a": "2026-11-01", f"{BASE}/post-b": "2026-11-02",
                                       f"{BASE}/post-c": "2026-11-02"}))
    doc, md = report_for(cfg, root, "2026-11-02", rec)
    assert doc["status"] == "stable"
    assert "**All stable: no critical or warning changes since 2026-11-01 (1 info)**" in md
    assert "## Findings" not in md and "## Source health" not in md and "####" not in md
    assert "Sources: robots.txt ok" in md
    assert "Info (1):" in md and "1 new URL(s) in the sitemap" in md
    assert "Known issues (1, unchanged):" in md and "title length out of range (info, rule `title_length`): 1 item(s)" in md
    assert len(md.splitlines()) < 25


def test_stable_with_no_changes_at_all(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    doc, md = report_for(cfg, root, "2026-11-02", day_records())
    assert doc["findings"] == []
    assert "Info (" not in md
    assert "Compared with: 2026-11-01" in md


def test_issues_report_orders_severity_and_regressions_first(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    rec = day_records(crawl=crawl([page("/", title="Example Blog"), page("/post-a", noindex=True),
                                   page("/post-b", title=None)]),
                      links=links([link("/tag/x", http=404), link("/about")]))
    doc, md = report_for(cfg, root, "2026-11-02", rec)
    assert doc["status"] == "issues"
    crit, warn = md.index("### Critical"), md.index("### Warning")
    assert crit < warn
    ids = re.findall(r"^#### (F\d{3})", md, flags=re.M)
    assert ids == [f["id"] for f in doc["findings"]]  # same order as findings.json
    noindex = next(f for f in doc["findings"] if f["rule"] == "page_noindex")
    block = md[md.index(f"#### {noindex['id']}"):]
    block = block[:block.index("\n####")] if "\n####" in block else block
    assert "**critical** · new · regression" in block
    assert f"- URL: <{BASE}/post-a>" in block
    assert "- Evidence: `'indexable' -> 'meta-robots'` (crawl.json 2026-11-02 vs 2026-11-01)" in block
    assert f"- Next step: {RP.esc(noindex['suggested_next_step'])}" in block
    broken = next(f for f in doc["findings"] if f["rule"] == "link_broken")
    assert "Linked from 3 page(s)" in md and broken["id"] in md


def test_failed_source_is_explicit(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    doc, md = report_for(cfg, root, "2026-11-02",
                         day_records(psi=psi(runs=[], status="failed", error="HTTP 403: blocked")))
    assert "> **PSI failed: HTTP 403: blocked; Core Web Vitals and PageSpeed scores not compared today.**" in md
    assert "| PSI | **failed** | not compared |" in md


def test_upstream_failure_says_not_run(cfg, root):
    rec = day_records(robots={"source": "robots", "status": "failed", "error": "timeout", "mode": None,
                              "allowed": {}, "sitemaps_declared": []},
                      crawl=crawl([], status="failed"), links=links(status="failed"))
    _, md = report_for(cfg, root, "2026-11-01", rec)
    assert "ROBOTS failed: timeout; robots.txt rules not compared today." in md
    assert "CRAWL not run: robots failed" in md and "LINKS not run: crawl failed" in md


def test_partial_source_is_shown_with_reasons(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    rec = day_records(crawl=crawl([page("/", title="Example Blog"), page("/post-a")], status="partial",
                                  reasons=("1 page skipped: runtime budget used up",)))
    doc, md = report_for(cfg, root, "2026-11-02", rec)
    assert doc["status"] == "stable"  # partial alone is info
    assert "CRAWL partial: 1 page skipped: runtime budget used up" in md
    assert "Crawl partial" in md


def test_grouped_finding_lists_urls_collapsed(cfg, root):
    pages = [page("/", title="Example Blog")] + [page(f"/p{i}", title="short") for i in range(8)]
    _, md = report_for(cfg, root, "2026-11-01", day_records(crawl=crawl(pages)))
    assert "- Affected: 9 URL(s)" in md
    assert "<details><summary>9 URLs</summary>" in md and "</details>" in md
    assert f"- <{BASE}/p0>: `now 5`" in md


def test_grouped_list_truncation_is_stated(cfg, root):
    cfg["rules"]["max_urls_per_finding"] = 3
    pages = [page("/", title="Example Blog")] + [page(f"/p{i}", title="short") for i in range(8)]
    doc, md = report_for(cfg, root, "2026-11-01", day_records(crawl=crawl(pages)))
    g = next(f for f in doc["findings"] if f["rule"] == "title_length")
    assert g["count"] == 9 and len(g["items"]) == 3
    assert "- ... and 6 more (see findings.json)" in md
    assert "<details>" not in md  # 3 lines stay open


def test_psi_pending_message_is_shown(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    doc, md = report_for(cfg, root, "2026-11-02", day_records(psi=psi(runs=[run(score=80)])))
    f = next(f for f in doc["findings"] if f["change"] == "pending")
    assert "possible regression, awaiting confirmation (1 of 2)" in md
    assert f["severity"] == "info" and doc["status"] == "stable"
    assert f"- {f['id']} info · possible regression" in md  # one-liner in the short report


def test_every_number_comes_from_the_data(cfg, root):
    """Numbers in the report text must appear in findings.json, meta.json or
    the source records of that day (dates and ids included)."""
    write(cfg, root, "2026-11-01", day_records())
    rec = day_records(crawl=crawl([page("/", title="Example Blog"), page("/post-a", http=404), page("/post-b")]),
                      psi=psi(runs=[run(score=70, lcp=4200)]))
    doc, md = report_for(cfg, root, "2026-11-02", rec)
    data = (root / "2026-11-02" / "findings.json").read_text(encoding="utf-8")
    for name in ("meta", "robots", "sitemap", "crawl", "links", "psi"):
        data += (root / "2026-11-02" / f"{name}.json").read_text(encoding="utf-8")
    data_numbers = numbers(data)
    missing = sorted(n for n in numbers(md) if n not in data_numbers)
    assert not missing, missing


def test_markdown_escaping(cfg, root):
    rec = day_records(crawl=crawl([page("/", title="Example Blog"), page("/post-a", title="<b>x</b> | *y*")]))
    _, md = report_for(cfg, root, "2026-11-01", rec)
    assert "<b>" not in md
    assert RP.esc("a|b <c> *d*") == "a\\|b \\<c> \\*d\\*"
    assert RP.code("a`b") == "`` a`b ``"
    assert RP.link("https://x.test/a b") == "`https://x.test/a b`"


def test_compare_to_line(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    write(cfg, root, "2026-11-02", day_records())
    _, md = report_for(cfg, root, "2026-11-03", day_records(), compare_to="2026-11-01")
    assert "Compared with: 2026-11-01 (chosen with --compare-to)" in md


def test_write_report_path_and_missing_findings(cfg, root, tmp_path):
    write(cfg, root, "2026-11-01", day_records())
    with pytest.raises(SnapshotError):
        RP.load_inputs(root, "2026-11-01")  # no findings.json yet
    path = RP.write_report(cfg, tmp_path, "2026-11-01", "# hi\n\n")
    assert path == tmp_path / "reports" / "2026-11-01.md"
    assert path.read_bytes() == b"# hi\n"


def test_source_health_rows(cfg, root):
    doc = findings_for(cfg, root, "2026-11-01", day_records())
    rows = RP.source_health(doc, day_records())
    assert [r["source"] for r in rows] == ["robots", "sitemap", "crawl", "links", "psi"]
    assert all(r["alert"] is None for r in rows)
    assert RP.coverage("psi", {"status": "ok", "counts": {"ok": 2}, "runs": [{}, {}]}) == "2 of 2 run(s) ok"
    assert RP.coverage("crawl", {"status": "ok", "counts": {"ok": 51}, "pages_selected": 51}) == "51 of 51 pages fetched"
    assert RP.coverage("links", {"status": "ok"}) == ""  # fields missing: no text, no crash


def test_pending_block_label_in_issues_report(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    rec = day_records(psi=psi(runs=[run(score=80)]), crawl=crawl([page("/", title="Example Blog"),
                                                                  page("/post-a", noindex=True), page("/post-b")]))
    doc, md = report_for(cfg, root, "2026-11-02", rec)
    assert doc["status"] == "issues"
    assert "possible regression, not confirmed yet" in md
    assert md.index("| robots.txt |") < md.index("| Sitemap |") < md.index("| PSI |")  # source order, not alphabetical
