from datetime import datetime, timezone

import yaml

from seo_reviewer import main as main_mod
from seo_reviewer import snapshot as snap
from seo_reviewer.http_client import PoliteClient

from conftest import FakeClock, FakeSession, make_response
from test_crawl import page_routes
from test_psi import HOME as PSI_HOME, KEY as PSI_KEY, PsiSession, resp as psi_resp
from test_sitemap import BASE, site_routes


def link_routes():
    """page_routes plus the two links found on the crawled pages that were not crawled."""
    return page_routes(**{
        f"{BASE}/tag/one/?page=2": make_response(f"{BASE}/tag/one/?page=2", 200, ""),
        f"{BASE}/blog/relative/": make_response(f"{BASE}/blog/relative/", 404, ""),
    })


def client_for(cfg, routes):
    clock = FakeClock()
    return PoliteClient(cfg["crawler"], session=FakeSession(routes), clock=clock, sleep=clock.sleep)


def psi_kwargs(routes=None, env=None):
    clock = FakeClock()
    routes = routes if routes is not None else {(PSI_HOME, "mobile"): psi_resp("mobile_ok"),
                                                (PSI_HOME, "desktop"): psi_resp("desktop_origin_fallback")}
    return {"session": PsiSession(routes, clock), "clock": clock, "sleep": clock.sleep,
            "env": {"PSI_API_KEY": PSI_KEY} if env is None else env}


def test_collect_only_writes_snapshot_and_applies_retention(cfg, tmp_path):
    root = snap.snapshot_root(cfg, tmp_path)
    (root / "2026-01-01").mkdir(parents=True)  # older than 90 days
    now = datetime(2026, 11, 8, 5, 3, tzinfo=timezone.utc)
    cfg["crawler"]["max_pages"] = 2
    result = main_mod.run_collect_only(cfg, tmp_path, client=client_for(cfg, link_routes()), now=now,
                                       psi_kwargs=psi_kwargs())
    assert result["day"] == "2026-11-08"
    assert result["deleted"] == ["2026-01-01"]
    data = snap.read_snapshot(root, "2026-11-08")
    assert set(data) == {"meta", "robots", "sitemap", "crawl", "links", "psi"}
    assert data["meta"]["sources"]["psi"] == {"status": "ok", "error": None}
    assert data["psi"]["counts"]["ok"] == 2
    assert PSI_KEY not in (root / "2026-11-08" / "psi.json").read_text(encoding="utf-8")
    assert data["meta"]["sources"]["robots"]["status"] == "ok"
    assert data["meta"]["sources"]["sitemap"]["status"] == "ok"
    assert data["meta"]["sources"]["crawl"]["status"] == "ok"
    assert data["meta"]["sources"]["links"]["status"] == "ok"
    # robots + index + 3 children + 3 pages + links: tag HEAD, relative HEAD 404 + GET
    assert data["meta"]["requests_made"] == 11
    assert data["links"]["counts"]["broken"] == 1
    assert [p["url"] for p in data["crawl"]["pages"]] == [f"{BASE}/", f"{BASE}/post-d/", f"{BASE}/post-e/"]
    assert data["robots"]["allowed"] == {f"{BASE}/": True, f"{BASE}/sitemap.xml": True}
    assert data["sitemap"]["url_count"] == 7
    text = main_mod.format_summary(result)
    assert "sitemap: ok, 7 URLs from 4 of 4 sitemap file(s)" in text
    assert "crawl:   ok, 3 page(s) selected: 3 fetched (3 HTTP 200, 0 redirected, 1 noindex)" in text
    assert "ok | 200 | 12 | 60 | 1 | self | no | 3 | https://example.test/" in text
    assert ("links:   ok, 4 unique internal link(s): 2 from the crawl, 2 to check, 0 over the cap; "
            "2 checked (1 needed GET after HEAD), 1 broken (1 4xx, 0 5xx, 0 failed)") in text
    assert "ok | 404 | GET | 1 | https://example.test/blog/relative/" in text
    assert "psi:     ok, 2 run(s) (1 URL(s) x mobile, desktop): 2 ok, 0 failed, 0 skipped; 2 API request(s)" in text
    assert "ok | mobile | 62 | 4124 ms | 0.0412 | 356 ms | 0.0 s | https://example.test/" in text
    assert ("field (CrUX, 28 days) URL: AVERAGE (LCP 2710 ms AVERAGE, INP 180 ms FAST, CLS 0.05 FAST); "
            "origin: FAST (LCP 2450 ms FAST, INP 160 ms FAST, CLS 0.03 FAST)") in text
    assert "field (CrUX, 28 days) URL: none; origin: FAST" in text
    assert "requests made: 11 to the site, 2 to the PSI API" in text
    assert PSI_KEY not in text


def test_psi_failure_is_recorded_not_fatal(cfg, tmp_path):
    cfg["crawler"]["max_pages"] = 2
    result = main_mod.run_collect_only(cfg, tmp_path, day="2026-11-08", client=client_for(cfg, link_routes()),
                                       psi_kwargs=psi_kwargs(env={}))
    meta = snap.read_meta(snap.snapshot_root(cfg, tmp_path), "2026-11-08")
    assert meta["sources"]["psi"]["status"] == "failed"
    assert meta["sources"]["crawl"]["status"] == "ok"
    text = main_mod.format_summary(result)
    assert "PSI failed: no API key: environment variable PSI_API_KEY is not set" in text


def test_psi_crash_does_not_lose_other_sources(cfg, tmp_path, monkeypatch):
    cfg["crawler"]["max_pages"] = 2

    def boom(*a, **kw):
        raise RuntimeError("bug")

    monkeypatch.setattr(main_mod, "collect_psi", boom)
    result = main_mod.run_collect_only(cfg, tmp_path, day="2026-11-08", client=client_for(cfg, link_routes()),
                                       psi_kwargs=psi_kwargs())
    assert result["records"]["psi"]["status"] == "failed"
    assert result["records"]["psi"]["error"] == "PSI collector crashed: RuntimeError: bug"
    assert result["records"]["links"]["status"] == "ok"
    assert "PSI failed: PSI collector crashed" in main_mod.format_summary(result)


def test_collect_only_records_failed_sources(cfg, tmp_path):
    routes = site_routes(**{f"{BASE}/robots.txt": make_response(f"{BASE}/robots.txt", 503, "")})
    result = main_mod.run_collect_only(cfg, tmp_path, day="2026-11-08", client=client_for(cfg, routes),
                                       psi_kwargs=psi_kwargs())
    meta = snap.read_meta(snap.snapshot_root(cfg, tmp_path), "2026-11-08")
    assert meta["sources"]["robots"]["status"] == "failed"
    assert meta["sources"]["sitemap"]["status"] == "failed"
    assert "robots.txt could not be fetched" in meta["sources"]["sitemap"]["error"]
    assert meta["sources"]["crawl"]["status"] == "failed"
    assert meta["sources"]["links"]["status"] == "failed"
    assert "crawl source failed" in meta["sources"]["links"]["error"]
    assert "sitemap: failed" in main_mod.format_summary(result)


def test_cli_argument_errors(tmp_path, capsys):
    assert main_mod.main(["--config", str(tmp_path / "missing.yaml")]) == 2
    assert main_mod.main(["--collect-only", "--date", "08-11-2026"]) == 2
    assert main_mod.main(["--collect-only", "--config", str(tmp_path / "missing.yaml")]) == 2
    assert "CONFIG ERROR" in capsys.readouterr().err


def test_cli_end_to_end_with_fake_network(cfg, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PSI_API_KEY", raising=False)  # PSI records "no API key", no network
    cfg["crawler"]["max_pages"] = 2
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    real_init = PoliteClient.__init__

    def fake_init(self, crawler_cfg, **kw):
        clock = FakeClock()
        real_init(self, crawler_cfg, session=FakeSession(link_routes()), clock=clock, sleep=clock.sleep)

    monkeypatch.setattr(PoliteClient, "__init__", fake_init)
    assert main_mod.main(["--collect-only", "--config", str(cfg_path), "--date", "2026-11-08"]) == 0
    assert (tmp_path / "data" / "snapshots" / "2026-11-08" / "meta.json").is_file()  # relative to config.yaml
    out = capsys.readouterr().out
    assert "Snapshot 2026-11-08 written" in out
    assert "PSI failed: no API key" in out


def test_run_daily_writes_findings_from_real_collector_records(cfg, tmp_path):
    cfg["crawler"]["max_pages"] = 2
    root = snap.snapshot_root(cfg, tmp_path)
    first = main_mod.run_daily(cfg, tmp_path, day="2026-11-07", client=client_for(cfg, link_routes()),
                               psi_kwargs=psi_kwargs())
    assert first["findings"]["status"] == "baseline"
    assert (root / "2026-11-07" / "findings.json").is_file()
    assert any(f["rule"] == "link_broken" and f["change"] == "baseline" for f in first["findings"]["findings"])
    second = main_mod.run_daily(cfg, tmp_path, day="2026-11-08", client=client_for(cfg, link_routes()),
                                psi_kwargs=psi_kwargs())
    doc = snap.read_source(root, "2026-11-08", "findings")
    assert doc == second["findings"]
    assert doc["status"] in ("stable", "issues")
    assert all(i["compared_to"] == "2026-11-07" for s, i in doc["sources"].items())
    assert not [f for f in doc["findings"] if f["severity"] in ("critical", "warning")]
    assert doc["all_stable"]
    text = main_mod.format_summary(second)
    assert "Findings written to" in text and "All stable" in text


def test_cli_diff_only_and_compare_to(cfg, tmp_path, capsys):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert main_mod.main(["--diff-only", "--config", str(cfg_path), "--date", "2026-11-08"]) == 1
    assert "SNAPSHOT ERROR" in capsys.readouterr().err
    root = snap.snapshot_root(cfg, tmp_path)
    recs = {"robots": {"status": "ok", "allowed": {}, "mode": "parsed"}, "sitemap": {"status": "ok", "sitemaps": []},
            "crawl": {"status": "ok", "pages": []}, "links": {"status": "ok", "links": []},
            "psi": {"status": "disabled", "runs": []}}
    for day in ("2026-11-07", "2026-11-08"):
        snap.write_snapshot(root, day, recs, snap.build_meta(cfg, day, recs, datetime(2026, 11, 8, tzinfo=timezone.utc)))
    assert main_mod.main(["--diff-only", "--config", str(cfg_path), "--date", "2026-11-08",
                          "--compare-to", "2026-11-07"]) == 0
    out = capsys.readouterr().out
    assert "Findings for 2026-11-08 written" in out and "All stable" in out
    assert main_mod.main(["--diff-only", "--compare-to", "7-11-2026"]) == 2
    assert main_mod.main(["--collect-only", "--compare-to", "2026-11-07"]) == 2
