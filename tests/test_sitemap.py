import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from seo_reviewer.collectors.sitemap import collect_sitemap, parse_lastmod, parse_sitemap
from seo_reviewer.http_client import PoliteClient

from conftest import FakeClock, FakeSession, make_response

BASE = "https://example.test"
FIX = Path(__file__).parent / "fixtures" / "sitemap"
XML = {"Content-Type": "application/xml"}
ROBOTS_OK = f"User-agent: *\nAllow: /\nSitemap: {BASE}/sitemap.xml\n"


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


def site_routes(**overrides):
    routes = {
        f"{BASE}/robots.txt": make_response(f"{BASE}/robots.txt", 200, ROBOTS_OK),
        f"{BASE}/sitemap.xml": make_response(f"{BASE}/sitemap.xml", 200, fixture("index.xml"), XML),
        f"{BASE}/sitemap-posts.xml": make_response(f"{BASE}/sitemap-posts.xml", 200, fixture("posts.xml"), XML),
        f"{BASE}/sitemap-posts-2.xml": make_response(f"{BASE}/sitemap-posts-2.xml", 200, fixture("posts-2.xml"), XML),
        f"{BASE}/sitemap-tags.xml": make_response(f"{BASE}/sitemap-tags.xml", 200, fixture("tags.xml"), XML),
    }
    routes.update(overrides)
    return routes


def run(cfg, routes):
    clock = FakeClock()
    session = FakeSession(routes)
    client = PoliteClient(cfg["crawler"], session=session, clock=clock, sleep=clock.sleep)
    return collect_sitemap(client, cfg), session


def by_url(rec):
    return {s["url"]: s for s in rec["sitemaps"]}


def test_index_all_children_ok(cfg):
    rec, session = run(cfg, site_routes())
    assert rec["status"] == "ok", rec["error"]
    assert rec["error"] is None and rec["partial_reasons"] == []
    assert rec["root_kind"] == "sitemapindex"
    assert rec["url_count"] == 7  # post-a is listed twice, counted once
    assert rec["urls_with_lastmod"] == 4
    sm = by_url(rec)
    assert sm[f"{BASE}/sitemap-posts.xml"]["urls"] == {
        f"{BASE}/post-a/": "2026-10-29T13:16:41.361Z",
        f"{BASE}/post-b/": "2026-10-01",
        f"{BASE}/post-c/": None,
    }
    assert sm[f"{BASE}/sitemap-tags.xml"]["url_count"] == 2
    assert all(json.dumps(rec))  # JSON-serializable


def test_children_fetched_freshest_first(cfg):
    _, session = run(cfg, site_routes())
    fetched = [u for _, u in session.calls if u.endswith(".xml")]
    assert fetched == [f"{BASE}/sitemap.xml", f"{BASE}/sitemap-posts-2.xml",
                       f"{BASE}/sitemap-posts.xml", f"{BASE}/sitemap-tags.xml"]


def test_plain_urlset_root(cfg):
    routes = site_routes(**{f"{BASE}/sitemap.xml": make_response(f"{BASE}/sitemap.xml", 200, fixture("posts.xml"), XML)})
    rec, _ = run(cfg, routes)
    assert rec["status"] == "ok"
    assert rec["root_kind"] == "urlset"
    assert rec["url_count"] == 3
    assert len(rec["sitemaps"]) == 1


def test_gzipped_child(cfg):
    gz = gzip.compress(fixture("tags.xml"))
    routes = site_routes(**{f"{BASE}/sitemap-tags.xml": make_response(f"{BASE}/sitemap-tags.xml", 200, gz,
                                                                    {"Content-Type": "application/x-gzip"})})
    rec, _ = run(cfg, routes)
    assert rec["status"] == "ok"
    assert by_url(rec)[f"{BASE}/sitemap-tags.xml"]["url_count"] == 2


def test_failed_child_makes_partial_and_says_which(cfg):
    routes = site_routes(**{f"{BASE}/sitemap-tags.xml": make_response(f"{BASE}/sitemap-tags.xml", 500, "")})
    rec, _ = run(cfg, routes)
    assert rec["status"] == "partial"
    assert rec["url_count"] == 5
    assert any("sitemap-tags.xml: HTTP 500" in r for r in rec["partial_reasons"])
    tags = by_url(rec)[f"{BASE}/sitemap-tags.xml"]
    assert tags["status"] == "failed" and tags["http_status"] == 500
    assert by_url(rec)[f"{BASE}/sitemap-posts.xml"]["status"] == "ok"


def test_unparseable_child_is_partial(cfg):
    routes = site_routes(**{f"{BASE}/sitemap-posts.xml": make_response(f"{BASE}/sitemap-posts.xml", 200, "<html>oops", {})})
    rec, _ = run(cfg, routes)
    assert rec["status"] == "partial"
    assert "not parseable" in by_url(rec)[f"{BASE}/sitemap-posts.xml"]["error"]


def test_root_failure_is_failed(cfg):
    routes = site_routes(**{f"{BASE}/sitemap.xml": make_response(f"{BASE}/sitemap.xml", 404, "")})
    rec, _ = run(cfg, routes)
    assert rec["status"] == "failed"
    assert rec["error"] == f"{BASE}/sitemap.xml: HTTP 404"
    assert rec["url_count"] == 0


def test_all_children_failing_is_failed(cfg):
    bad = {u: make_response(u, 503, "") for u in (f"{BASE}/sitemap-posts.xml", f"{BASE}/sitemap-posts-2.xml", f"{BASE}/sitemap-tags.xml")}
    rec, _ = run(cfg, site_routes(**bad))
    assert rec["status"] == "failed"
    assert rec["error"].startswith("no child sitemap could be read")


def test_robots_disallow_is_recorded_not_fetched(cfg):
    routes = site_routes(**{f"{BASE}/robots.txt": make_response(f"{BASE}/robots.txt", 200, "User-agent: *\nDisallow: /sitemap-tags.xml\n")})
    rec, session = run(cfg, routes)
    assert rec["status"] == "partial"
    assert ("GET", f"{BASE}/sitemap-tags.xml") not in session.calls
    assert "disallows" in by_url(rec)[f"{BASE}/sitemap-tags.xml"]["error"]


def test_robots_unreachable_means_nothing_fetched(cfg):
    routes = site_routes(**{f"{BASE}/robots.txt": make_response(f"{BASE}/robots.txt", 503, "")})
    rec, session = run(cfg, routes)
    assert rec["status"] == "failed"
    assert "robots.txt could not be fetched" in rec["error"]
    assert [u for _, u in session.calls if u.endswith(".xml")] == []


def test_max_urls_cap_truncates_and_skips(cfg):
    cfg["sitemap"]["max_urls"] = 4
    rec, session = run(cfg, site_routes())
    assert rec["status"] == "partial"
    assert rec["url_count"] == 4
    sm = by_url(rec)
    assert sm[f"{BASE}/sitemap-posts-2.xml"]["truncated"] is False      # 3 URLs, fits
    posts = sm[f"{BASE}/sitemap-posts.xml"]
    # post-a was already counted from posts-2, so it does not use budget; post-b does; post-c does not fit
    assert posts["truncated"] is True and list(posts["urls"]) == [f"{BASE}/post-a/", f"{BASE}/post-b/"]
    assert rec["skipped_sitemaps"] == [f"{BASE}/sitemap-tags.xml"]
    assert ("GET", f"{BASE}/sitemap-tags.xml") not in session.calls
    assert any("max_urls (4) reached" in r for r in rec["partial_reasons"])


def test_max_child_sitemaps_cap(cfg):
    cfg["sitemap"]["max_child_sitemaps"] = 2
    rec, session = run(cfg, site_routes())
    assert rec["status"] == "partial"
    assert rec["skipped_sitemaps"] == [f"{BASE}/sitemap-tags.xml"]
    assert any("max_child_sitemaps (2)" in r for r in rec["partial_reasons"])
    assert ("GET", f"{BASE}/sitemap-tags.xml") not in session.calls


def test_nested_index_is_followed_without_loops(cfg):
    nested = f"""<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>{BASE}/sitemap-tags.xml</loc></sitemap>
      <sitemap><loc>{BASE}/sitemap.xml</loc></sitemap></sitemapindex>"""
    routes = site_routes(**{f"{BASE}/sitemap-posts.xml": make_response(f"{BASE}/sitemap-posts.xml", 200, nested, XML)})
    rec, session = run(cfg, routes)
    assert rec["status"] == "ok"
    assert [u for _, u in session.calls].count(f"{BASE}/sitemap.xml") == 1
    assert [u for _, u in session.calls].count(f"{BASE}/sitemap-tags.xml") == 1


def test_parse_sitemap_items_and_gzip_bomb_guard(monkeypatch):
    parsed = parse_sitemap(fixture("posts.xml"))
    assert parsed["items"][2] == (f"{BASE}/post-c/", None)
    import seo_reviewer.collectors.sitemap as sm_mod
    monkeypatch.setattr(sm_mod, "MAX_SITEMAP_BYTES", 100)
    with pytest.raises(ValueError, match="larger than"):
        sm_mod.parse_sitemap(gzip.compress(b"<urlset>" + b" " * 1000 + b"</urlset>"))


@pytest.mark.parametrize("value,expected", [
    ("2026-11-06", datetime(2026, 11, 6, tzinfo=timezone.utc)),
    ("2026-10-29T13:16:41.361Z", datetime(2026, 10, 29, 13, 16, 41, 361000, tzinfo=timezone.utc)),
    ("2026-11-06T16:09:41+04:00", datetime(2026, 11, 6, 12, 9, 41, tzinfo=timezone.utc)),
    ("2026-11-06T16:09-0130", datetime(2026, 11, 6, 17, 39, tzinfo=timezone.utc)),
    ("2026-11", datetime(2026, 11, 1, tzinfo=timezone.utc)),
    ("2026-13-01", None),
    ("yesterday", None),
    (None, None),
])
def test_parse_lastmod(value, expected):
    assert parse_lastmod(value) == expected
