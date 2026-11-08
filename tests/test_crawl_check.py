import gzip

import pytest

from seo_reviewer.crawl_check import detect_challenge, format_markdown, parse_sitemap, run_crawl_check
from seo_reviewer.http_client import PoliteClient

from conftest import FakeClock, FakeSession, make_response

BASE = "https://example.test"
INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.test/sitemap-posts.xml</loc><lastmod>2026-11-06</lastmod></sitemap>
  <sitemap><loc>https://example.test/sitemap-tags.xml</loc></sitemap>
</sitemapindex>"""
URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.test/a/</loc><lastmod>2026-11-06</lastmod></url>
  <url><loc>https://example.test/b/</loc></url>
</urlset>"""
HOME = "<html><head><title>Example blog</title></head><body>" + ('<a href="/x">x</a>' * 200) + "</body></html>"
HTML = {"Content-Type": "text/html; charset=utf-8"}
XML = {"Content-Type": "application/xml"}


def run(cfg, routes):
    clock = FakeClock()
    client = PoliteClient(cfg["crawler"], session=FakeSession(routes), clock=clock, sleep=clock.sleep)
    return run_crawl_check(cfg, client)


def good_routes():
    return {
        f"{BASE}/robots.txt": make_response(f"{BASE}/robots.txt", 200, f"User-agent: *\nAllow: /\nSitemap: {BASE}/sitemap.xml\n"),
        f"{BASE}/": make_response(f"{BASE}/", 200, HOME, HTML),
        f"{BASE}/sitemap.xml": make_response(f"{BASE}/sitemap.xml", 200, INDEX, XML),
        f"{BASE}/sitemap-posts.xml": make_response(f"{BASE}/sitemap-posts.xml", 200, URLSET, XML),
    }


def by_name(report):
    return {c["name"]: c for c in report["checks"]}


def test_all_pass(cfg):
    report = run(cfg, good_routes())
    assert report["passed"], report
    checks = by_name(report)
    assert checks["sitemap"]["details"]["kind"] == "sitemapindex"
    assert checks["sitemap"]["details"]["first_child"]["entries"] == 2
    assert checks["homepage"]["details"]["title"] == "Example blog"


def test_robots_disallows_homepage(cfg):
    routes = good_routes()
    routes[f"{BASE}/robots.txt"] = make_response(f"{BASE}/robots.txt", 200, "User-agent: *\nDisallow: /\n")
    report = run(cfg, routes)
    checks = by_name(report)
    assert not report["passed"]
    assert not checks["robots"]["passed"]
    assert not checks["homepage"]["passed"]
    assert "disallows" in checks["homepage"]["summary"]


def test_cloudflare_challenge_detected(cfg):
    routes = good_routes()
    page = "<html><head><title>Just a moment...</title></head><body>" + "x" * 3000 + "</body></html>"
    routes[f"{BASE}/"] = make_response(f"{BASE}/", 403, page, {**HTML, "cf-mitigated": "challenge"})
    report = run(cfg, routes)
    hp = by_name(report)["homepage"]
    assert not hp["passed"]
    assert "HTTP 403" in hp["summary"] and "cf-mitigated" in hp["summary"]


def test_normal_page_with_cloudflare_scripts_is_not_a_challenge():
    body = '<html><title>Blog</title><script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script></html>'
    assert detect_challenge(200, {"cf-ray": "abc"}, body) == []


def test_tiny_or_non_html_homepage_fails(cfg):
    routes = good_routes()
    routes[f"{BASE}/"] = make_response(f"{BASE}/", 200, '{"ok": true}', {"Content-Type": "application/json"})
    hp = by_name(run(cfg, routes))["homepage"]
    assert not hp["passed"]
    assert "not HTML" in hp["summary"] and "no <title>" in hp["summary"]


def test_sitemap_not_xml_fails(cfg):
    routes = good_routes()
    routes[f"{BASE}/sitemap.xml"] = make_response(f"{BASE}/sitemap.xml", 200, "<html>nope", HTML)
    sm = by_name(run(cfg, routes))["sitemap"]
    assert not sm["passed"]
    assert "not parseable" in sm["summary"]


def test_sitemap_404_fails(cfg):
    routes = good_routes()
    routes[f"{BASE}/sitemap.xml"] = make_response(f"{BASE}/sitemap.xml", 404, "")
    sm = by_name(run(cfg, routes))["sitemap"]
    assert not sm["passed"] and "HTTP 404" in sm["summary"]


def test_parse_sitemap_urlset_and_gzip():
    plain = parse_sitemap(URLSET.encode())
    assert plain == {"kind": "urlset", "entries": 2, "locs": ["https://example.test/a/", "https://example.test/b/"], "with_lastmod": 1,
                     "items": [("https://example.test/a/", "2026-11-06"), ("https://example.test/b/", None)]}
    assert parse_sitemap(gzip.compress(URLSET.encode()))["entries"] == 2


def test_parse_sitemap_wrong_root():
    with pytest.raises(ValueError, match="expected <sitemapindex> or <urlset>"):
        parse_sitemap(b"<rss></rss>")


def test_parse_sitemap_ignores_external_entities():
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>&e;</loc></url></urlset>'
    result = parse_sitemap(evil)
    assert all("root:" not in loc for loc in result["locs"])


def test_markdown_summary_escapes_tag_names(cfg):
    md = format_markdown(run(cfg, good_routes()))
    assert "&lt;sitemapindex&gt; with 2 entries" in md
    assert "<sitemapindex>" not in md
