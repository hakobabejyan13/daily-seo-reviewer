import json
from pathlib import Path

import requests

from seo_reviewer.collectors.crawl import (collect_crawl, normalize_link, parse_html,
                                           parse_robots_directives, select_pages)
from seo_reviewer.collectors.sitemap import collect_sitemap
from seo_reviewer.http_client import PoliteClient

from conftest import FakeClock, FakeSession, make_response
from test_sitemap import BASE, site_routes

FIX = Path(__file__).parent / "fixtures" / "crawl"
HTML = {"Content-Type": "text/html; charset=utf-8"}


def page(name: str) -> bytes:
    return (FIX / name).read_bytes()


def html(url: str, name: str, status: int = 200, headers: dict | None = None):
    return make_response(url, status, page(name), {**HTML, **(headers or {})})


def page_routes(**overrides):
    """Sitemap site plus the pages a max_pages=2 crawl fetches (home, post-d, post-e)."""
    routes = site_routes()
    routes.update({
        f"{BASE}/": html(f"{BASE}/", "home.html"),
        f"{BASE}/post-d/": html(f"{BASE}/post-d/", "post-noindex.html"),
        f"{BASE}/post-e/": html(f"{BASE}/post-e/", "post-messy.html"),
    })
    routes.update(overrides)
    return routes


def make_client(cfg, routes, clock=None):
    clock = clock or FakeClock()
    session = FakeSession(routes)
    return PoliteClient(cfg["crawler"], session=session, clock=clock, sleep=clock.sleep), session


def crawl(cfg, routes, max_pages=2, deadline=None, clock=None):
    cfg["crawler"]["max_pages"] = max_pages
    client, session = make_client(cfg, routes, clock)
    sitemap = collect_sitemap(client, cfg)
    return collect_crawl(client, cfg, sitemap, deadline=deadline), session


def by_url(rec):
    return {p["url"]: p for p in rec["pages"]}


# ---- selection -------------------------------------------------------------

def sitemap_record(urls: dict, status="ok"):
    return {"status": status, "error": None, "sitemaps": [{"url": f"{BASE}/s.xml", "urls": urls}]}


def test_select_newest_by_lastmod_homepage_first_then_extras(cfg):
    cfg["crawler"]["max_pages"] = 3
    cfg["crawler"]["extra_urls"] = ["/about/", "https://other.test/x", f"{BASE}/about/"]
    rec = sitemap_record({
        f"{BASE}/old/": "2026-01-01",
        f"{BASE}/": "2026-11-07",                      # homepage: already selected
        f"{BASE}/new/": "2026-11-06T16:09:41+04:00",   # 12:09 UTC
        f"{BASE}/newer/": "2026-11-06T13:00:00Z",
        f"{BASE}/undated/": None,
        "https://cdn.other.test/x/": "2026-11-08",
    })
    pages, notes = select_pages(cfg, rec)
    assert [(p["url"], p["reason"]) for p in pages] == [
        (f"{BASE}/", "homepage"),
        (f"{BASE}/about/", "extra_url"),
        (f"{BASE}/newer/", "sitemap"),
        (f"{BASE}/new/", "sitemap"),
        (f"{BASE}/old/", "sitemap"),
    ]
    assert any("other.test/x" in n for n in notes)
    assert any("1 sitemap URL(s) ignored" in n for n in notes)


def test_select_fills_with_undated_urls(cfg):
    cfg["crawler"]["max_pages"] = 5
    pages, notes = select_pages(cfg, sitemap_record({f"{BASE}/a/": None, f"{BASE}/b/": "2026-11-01"}))
    assert [p["url"] for p in pages] == [f"{BASE}/", f"{BASE}/b/", f"{BASE}/a/"]
    assert "sitemap gave 2 page(s) for max_pages 5" in notes


def test_select_when_sitemap_failed(cfg):
    pages, notes = select_pages(cfg, {"status": "failed", "error": "HTTP 500", "sitemaps": []})
    assert [p["url"] for p in pages] == [f"{BASE}/"]
    assert "sitemap source failed (HTTP 500)" in notes[0]


# ---- parsing ---------------------------------------------------------------

def test_parse_home():
    seo = parse_html(page("home.html"), f"{BASE}/", HTML["Content-Type"])
    assert seo["title"] == "Example Blog"           # whitespace normalised, svg <title> ignored
    assert seo["title_count"] == 1
    assert seo["meta_description"].startswith("Latest posts")
    assert seo["h1"] == ["Example Blog"]
    assert seo["canonical"] == f"{BASE}/" and seo["canonical_count"] == 1
    assert seo["lang"] == "en"
    assert seo["internal_links"] == [f"{BASE}/post-d/", f"{BASE}/post-e/", f"{BASE}/tag/one/?page=2"]
    assert seo["links_total"] == 5  # http(s) anchors only, duplicates included


def test_parse_messy_page_uses_base_href_and_first_of_each():
    seo = parse_html(page("post-messy.html"), f"{BASE}/post-e/")
    assert seo["title"] is None and seo["title_count"] == 0
    assert seo["meta_description"] == "First description" and seo["meta_description_count"] == 2
    assert seo["canonical"] == f"{BASE}/blog/canonical-target/"   # multi-valued rel, relative to <base>
    assert seo["canonical_raw"] == "canonical-target/" and seo["canonical_count"] == 2
    assert seo["h1"] == ["One", "Two lines", ""]
    assert seo["internal_links"] == [f"{BASE}/blog/relative/"]


def test_parse_charset_from_meta():
    seo = parse_html(page("latin1.html"), f"{BASE}/x/", "text/html")
    assert seo["title"] == "Café crème"
    assert seo["h1"] == ["Café"]


def test_robots_directives():
    assert parse_robots_directives(["NoIndex, follow"], header=False) == ["noindex", "follow"]
    assert parse_robots_directives(["googlebot: noindex", "otherbot: none", "nofollow"], header=True) == ["noindex", "nofollow"]
    assert parse_robots_directives(["unavailable_after: 25 Jun 2030 15:00:00 PST"], header=True)[0].startswith("unavailable_after")


def test_normalize_link():
    assert normalize_link("/a#x", f"{BASE}/p/") == f"{BASE}/a"
    assert normalize_link("", f"{BASE}/") is None
    assert normalize_link("tel:123", f"{BASE}/") is None
    assert normalize_link("//example.test", f"{BASE}/") == f"{BASE}/"
    assert normalize_link("http://[bad", f"{BASE}/") is None


# ---- collector -------------------------------------------------------------

def test_crawl_ok(cfg):
    rec, session = crawl(cfg, page_routes())
    assert rec["status"] == "ok", rec["error"]
    assert rec["pages_selected"] == 3
    pages = by_url(rec)
    assert list(pages) == [f"{BASE}/", f"{BASE}/post-d/", f"{BASE}/post-e/"]
    home = pages[f"{BASE}/"]
    assert home["status"] == "ok" and home["http_status"] == 200 and home["final_url"] == f"{BASE}/"
    assert home["noindex"] is False and home["redirect_chain"] == []
    d = pages[f"{BASE}/post-d/"]
    assert d["lastmod"] == "2026-11-06T16:09:41+04:00"
    assert d["noindex"] is True and d["noindex_sources"] == ["meta-robots"]
    assert d["seo"]["meta_description"] == ""              # present but empty
    assert d["seo"]["canonical"] == f"{BASE}/post-d/"
    assert rec["counts"] == {"ok": 3, "failed": 0, "disallowed": 0, "skipped": 0,
                             "http_200": 3, "redirected": 0, "noindex": 1}
    assert ("GET", f"{BASE}/post-d/") in session.calls
    assert json.dumps(rec)


def test_redirect_chain_is_followed_hop_by_hop(cfg):
    routes = page_routes(**{
        f"{BASE}/post-d/": make_response(f"{BASE}/post-d/", 301, "", {"Location": "/post-d-new/"}),
        f"{BASE}/post-d-new/": make_response(f"{BASE}/post-d-new/", 302, "", {"Location": "https://example.test/post-d-final/"}),
        f"{BASE}/post-d-final/": html(f"{BASE}/post-d-final/", "home.html", headers={"X-Robots-Tag": "noindex"}),
    })
    rec, _ = crawl(cfg, routes)
    d = by_url(rec)[f"{BASE}/post-d/"]
    assert d["status"] == "ok"
    assert d["final_url"] == f"{BASE}/post-d-final/" and d["http_status"] == 200
    assert [(h["url"], h["status"]) for h in d["redirect_chain"]] == [
        (f"{BASE}/post-d/", 301), (f"{BASE}/post-d-new/", 302)]
    assert d["x_robots_tag"] == ["noindex"] and d["noindex_sources"] == ["x-robots-tag"]
    assert rec["counts"]["redirected"] == 1


def test_offsite_redirect_not_followed_and_loop_fails(cfg):
    routes = page_routes(**{
        f"{BASE}/post-d/": make_response(f"{BASE}/post-d/", 301, "", {"Location": "https://other.test/"}),
        f"{BASE}/post-e/": make_response(f"{BASE}/post-e/", 301, "", {"Location": "/post-e/"}),
    })
    rec, session = crawl(cfg, routes)
    pages = by_url(rec)
    d = pages[f"{BASE}/post-d/"]
    assert d["status"] == "ok" and d["final_url"] == "https://other.test/" and d["seo"] is None
    assert "another host" in d["error"]
    assert all("other.test" not in url for _, url in session.calls)
    e = pages[f"{BASE}/post-e/"]
    assert e["status"] == "failed" and "redirect loop" in e["error"]
    assert rec["status"] == "partial" and "1 page(s) failed" in rec["error"]


def test_http_errors_and_non_html_are_recorded(cfg):
    routes = page_routes(**{
        f"{BASE}/post-d/": make_response(f"{BASE}/post-d/", 404, "<h1>gone</h1>", HTML),
        f"{BASE}/post-e/": make_response(f"{BASE}/post-e/", 200, b"%PDF", {"Content-Type": "application/pdf"}),
    })
    rec, _ = crawl(cfg, routes)
    pages = by_url(rec)
    assert pages[f"{BASE}/post-d/"]["status"] == "ok" and pages[f"{BASE}/post-d/"]["http_status"] == 404
    assert pages[f"{BASE}/post-d/"]["seo"] is None
    assert "not HTML" in pages[f"{BASE}/post-e/"]["error"]
    assert rec["status"] == "ok"  # the collector worked; the diff judges 404s


def test_network_failure_after_retry(cfg):
    boom = requests.ConnectionError("reset")
    rec, session = crawl(cfg, page_routes(**{f"{BASE}/post-e/": boom}))
    e = by_url(rec)[f"{BASE}/post-e/"]
    assert e["status"] == "failed" and "request failed" in e["error"]
    assert session.calls.count(("GET", f"{BASE}/post-e/")) == 2   # max_retries 1
    assert rec["status"] == "partial"


def test_robots_disallowed_page_is_recorded_not_fetched(cfg):
    robots = f"User-agent: *\nDisallow: /post-d/\nDisallow: /moved/\nAllow: /\n"
    routes = page_routes(**{
        f"{BASE}/robots.txt": make_response(f"{BASE}/robots.txt", 200, robots),
        f"{BASE}/post-e/": make_response(f"{BASE}/post-e/", 301, "", {"Location": "/moved/"}),
    })
    rec, session = crawl(cfg, routes)
    pages = by_url(rec)
    assert pages[f"{BASE}/post-d/"]["status"] == "disallowed"
    assert ("GET", f"{BASE}/post-d/") not in session.calls
    e = pages[f"{BASE}/post-e/"]                       # redirect hop into a disallowed path
    assert e["status"] == "disallowed" and e["redirect_chain"][0]["location"] == f"{BASE}/moved/"
    assert ("GET", f"{BASE}/moved/") not in session.calls
    assert rec["status"] == "ok" and rec["counts"]["disallowed"] == 2


def test_robots_unreachable_means_nothing_crawled(cfg):
    routes = page_routes(**{f"{BASE}/robots.txt": make_response(f"{BASE}/robots.txt", 503, "")})
    rec, session = crawl(cfg, routes)
    assert rec["status"] == "failed" and "robots.txt could not be fetched" in rec["error"]
    assert all(p["status"] == "disallowed" for p in rec["pages"])
    assert session.calls == [("GET", f"{BASE}/robots.txt")] * 2


def test_sitemap_failed_crawls_homepage_and_marks_partial(cfg):
    routes = page_routes(**{f"{BASE}/sitemap.xml": make_response(f"{BASE}/sitemap.xml", 500, "")})
    rec, _ = crawl(cfg, routes)
    assert [p["url"] for p in rec["pages"]] == [f"{BASE}/"]
    assert rec["status"] == "partial" and "sitemap source failed" in rec["error"]


def test_budget_stops_early(cfg):
    clock = FakeClock()
    # robots + index + 3 children = 5 requests at 1/s; give the crawl room for 2 more requests.
    rec, session = crawl(cfg, page_routes(), deadline=5.5, clock=clock)
    pages = by_url(rec)
    assert pages[f"{BASE}/"]["status"] == "ok"
    assert pages[f"{BASE}/post-d/"]["status"] == "ok"
    assert pages[f"{BASE}/post-e/"]["status"] == "skipped"
    assert ("GET", f"{BASE}/post-e/") not in session.calls
    assert rec["status"] == "partial" and "budget_seconds reached: 1 page(s)" in rec["error"]
