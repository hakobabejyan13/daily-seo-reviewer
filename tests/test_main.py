from datetime import datetime, timezone

import yaml

from seo_reviewer import main as main_mod
from seo_reviewer import snapshot as snap
from seo_reviewer.http_client import PoliteClient

from conftest import FakeClock, FakeSession, make_response
from test_sitemap import BASE, site_routes


def client_for(cfg, routes):
    clock = FakeClock()
    return PoliteClient(cfg["crawler"], session=FakeSession(routes), clock=clock, sleep=clock.sleep)


def test_collect_only_writes_snapshot_and_applies_retention(cfg, tmp_path):
    root = snap.snapshot_root(cfg, tmp_path)
    (root / "2026-01-01").mkdir(parents=True)  # older than 90 days
    now = datetime(2026, 11, 8, 5, 3, tzinfo=timezone.utc)
    result = main_mod.run_collect_only(cfg, tmp_path, client=client_for(cfg, site_routes()), now=now)
    assert result["day"] == "2026-11-08"
    assert result["deleted"] == ["2026-01-01"]
    data = snap.read_snapshot(root, "2026-11-08")
    assert set(data) == {"meta", "robots", "sitemap"}
    assert data["meta"]["sources"]["robots"]["status"] == "ok"
    assert data["meta"]["sources"]["sitemap"]["status"] == "ok"
    assert data["meta"]["requests_made"] == 5  # robots + index + 3 children
    assert data["robots"]["allowed"] == {f"{BASE}/": True, f"{BASE}/sitemap.xml": True}
    assert data["sitemap"]["url_count"] == 7
    text = main_mod.format_summary(result)
    assert "sitemap: ok, 7 URLs from 4 of 4 sitemap file(s)" in text


def test_collect_only_records_failed_sources(cfg, tmp_path):
    routes = site_routes(**{f"{BASE}/robots.txt": make_response(f"{BASE}/robots.txt", 503, "")})
    result = main_mod.run_collect_only(cfg, tmp_path, day="2026-11-08", client=client_for(cfg, routes))
    meta = snap.read_meta(snap.snapshot_root(cfg, tmp_path), "2026-11-08")
    assert meta["sources"]["robots"]["status"] == "failed"
    assert meta["sources"]["sitemap"]["status"] == "failed"
    assert "robots.txt could not be fetched" in meta["sources"]["sitemap"]["error"]
    assert "sitemap: failed" in main_mod.format_summary(result)


def test_cli_argument_errors(tmp_path, capsys):
    assert main_mod.main(["--config", str(tmp_path / "missing.yaml")]) == 2
    assert main_mod.main(["--collect-only", "--date", "08-11-2026"]) == 2
    assert main_mod.main(["--collect-only", "--config", str(tmp_path / "missing.yaml")]) == 2
    assert "CONFIG ERROR" in capsys.readouterr().err


def test_cli_end_to_end_with_fake_network(cfg, tmp_path, monkeypatch, capsys):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    real_init = PoliteClient.__init__

    def fake_init(self, crawler_cfg, **kw):
        clock = FakeClock()
        real_init(self, crawler_cfg, session=FakeSession(site_routes()), clock=clock, sleep=clock.sleep)

    monkeypatch.setattr(PoliteClient, "__init__", fake_init)
    assert main_mod.main(["--collect-only", "--config", str(cfg_path), "--date", "2026-11-08"]) == 0
    assert (tmp_path / "data" / "snapshots" / "2026-11-08" / "meta.json").is_file()  # relative to config.yaml
    assert "Snapshot 2026-11-08 written" in capsys.readouterr().out
