import requests

from seo_reviewer.collectors.links import (collect_links, crawl_results, gather_links, is_broken,
                                           order_links)
from seo_reviewer.http_client import PoliteClient

from conftest import FakeClock, FakeSession, make_response
from test_crawl import crawl, page_routes
from test_sitemap import BASE, ROBOTS_OK

ROBOTS = make_response(f"{BASE}/robots.txt", 200, ROBOTS_OK)


def make_client(cfg, routes, clock=None):
    clock = clock or FakeClock()
    session = FakeSession({f"{BASE}/robots.txt": ROBOTS, **routes})
    return PoliteClient(cfg["crawler"], session=session, clock=clock, sleep=clock.sleep), session


def crawl_page(url, links, status="ok", http_status=200, final_url=None, redirect_chain=None, truncated=False):
    seo = {"internal_links": links, "internal_links_truncated": truncated} if http_status == 200 and status == "ok" else None
    return {"url": url, "status": status, "http_status": http_status if status == "ok" else None,
            "final_url": final_url or (url if status == "ok" else None),
            "redirect_chain": redirect_chain or [], "error": None if status == "ok" else "x", "seo": seo}


def crawl_rec(pages, status="ok"):
    return {"status": status, "error": None if status == "ok" else "crawl problem", "pages": pages}


def resp(url, status=200, headers=None):
    return make_response(url, status, "", headers)


def by_url(rec):
    return {lk["url"]: lk for lk in rec["links"]}


# ---- input -----------------------------------------------------------------

def test_gather_inbound_and_order_most_linked_then_alphabetical():
    rec = crawl_rec([
        crawl_page(f"{BASE}/", [f"{BASE}/b/", f"{BASE}/a/", f"{BASE}/c/"]),
        crawl_page(f"{BASE}/p1/", [f"{BASE}/c/", f"{BASE}/b/", "https://other.test/x"]),
        crawl_page(f"{BASE}/p2/", [f"{BASE}/c/"], truncated=True),
        crawl_page(f"{BASE}/gone/", [], http_status=404),
        crawl_page(f"{BASE}/skip/", [], status="skipped"),
    ])
    inbound, notes = gather_links(rec, "example.test", check_external=False)
    assert inbound[f"{BASE}/c/"] == [f"{BASE}/", f"{BASE}/p1/", f"{BASE}/p2/"]
    assert order_links(inbound) == [f"{BASE}/c/", f"{BASE}/b/", f"{BASE}/a/"]
    assert any("p2/" in n and "more internal links" in n for n in notes)
    assert any("1 link(s) to another host ignored" in n for n in notes)


def test_crawl_results_reuse_page_and_redirect_target():
    rec = crawl_rec([
        crawl_page(f"{BASE}/old/", [], final_url=f"{BASE}/new/",
                   redirect_chain=[{"url": f"{BASE}/old/", "status": 301, "location": f"{BASE}/new/"}]),
        crawl_page(f"{BASE}/skip/", [], status="skipped"),
        crawl_page(f"{BASE}/no/", [], status="disallowed"),
    ])
    known = crawl_results(rec)
    assert known[f"{BASE}/old/"]["http_status"] == 200 and len(known[f"{BASE}/old/"]["redirect_chain"]) == 1
    assert known[f"{BASE}/new/"] == {"status": "ok", "http_status": 200, "final_url": f"{BASE}/new/",
                                     "redirect_chain": [], "error": None}
    assert f"{BASE}/skip/" not in known
    assert known[f"{BASE}/no/"]["status"] == "disallowed"


# ---- end to end with the real crawl ------------------------------------------

def test_links_after_real_crawl_reuses_crawled_pages(cfg):
    routes = page_routes(**{
        f"{BASE}/tag/one/?page=2": resp(f"{BASE}/tag/one/?page=2"),
        f"{BASE}/blog/relative/": resp(f"{BASE}/blog/relative/", 404),
    })
    crawl_record, session = crawl(cfg, routes)
    assert crawl_record["status"] == "ok"
    client = PoliteClient(cfg["crawler"], session=session, clock=FakeClock(), sleep=lambda s: None)
    session.calls.clear()
    rec = collect_links(client, cfg, crawl_record)
    links = by_url(rec)
    assert rec["status"] == "ok", rec["error"]
    # home -> post-d, post-e, tag; post-d -> post-e; post-e (base href /blog/) -> blog/relative
    assert list(links) == [f"{BASE}/post-e/", f"{BASE}/blog/relative/", f"{BASE}/post-d/",
                           f"{BASE}/tag/one/?page=2"]
    assert links[f"{BASE}/post-e/"]["linked_from"] == [f"{BASE}/", f"{BASE}/post-d/"]
    assert links[f"{BASE}/post-e/"]["source"] == "crawl"
    assert links[f"{BASE}/post-d/"]["source"] == "crawl"
    assert rec["unique_links"] == 4 and rec["from_crawl"] == 2 and rec["to_check"] == 2
    bad = links[f"{BASE}/blog/relative/"]
    assert bad["http_status"] == 404 and bad["method"] == "GET" and is_broken(bad)
    assert rec["counts"]["broken"] == 1 and rec["counts"]["http_4xx"] == 1
    # robots.txt (new client), relative HEAD 404 + GET confirm, tag HEAD; crawled pages not requested again
    assert session.calls == [("GET", f"{BASE}/robots.txt"), ("HEAD", f"{BASE}/blog/relative/"),
                             ("GET", f"{BASE}/blog/relative/"), ("HEAD", f"{BASE}/tag/one/?page=2")]


# ---- methods, redirects, robots ----------------------------------------------

def single(cfg, routes, links, **kw):
    client, session = make_client(cfg, routes)
    rec = collect_links(client, cfg, crawl_rec([crawl_page(f"{BASE}/", links)]), **kw)
    return rec, session


def test_head_405_falls_back_to_get(cfg):
    url = f"{BASE}/a/"
    rec, session = single(cfg, {url: [resp(url, 405), resp(url, 200)]}, [url])
    lk = by_url(rec)[url]
    assert lk["status"] == "ok" and lk["http_status"] == 200 and lk["method"] == "GET"
    assert "HEAD returned 405" in lk["notes"][0]
    assert not is_broken(lk) and rec["counts"]["get_fallback"] == 1


def test_head_network_error_falls_back_to_get(cfg):
    url = f"{BASE}/a/"
    cfg["crawler"]["max_retries"] = 0
    rec, _ = single(cfg, {url: [requests.ConnectionError("reset"), resp(url, 200)]}, [url])
    lk = by_url(rec)[url]
    assert lk["status"] == "ok" and lk["method"] == "GET" and "HEAD failed" in lk["notes"][0]


def test_both_methods_fail_is_failed_and_partial(cfg):
    url = f"{BASE}/a/"
    cfg["crawler"]["max_retries"] = 0
    rec, _ = single(cfg, {url: requests.Timeout("slow"), f"{BASE}/b/": resp(f"{BASE}/b/")}, [url, f"{BASE}/b/"])
    lk = by_url(rec)[url]
    assert lk["status"] == "failed" and "request failed" in lk["error"] and is_broken(lk)
    assert rec["status"] == "partial" and "1 link(s) failed" in rec["error"]


def test_method_get_never_sends_head(cfg):
    url = f"{BASE}/a/"
    cfg["links"]["method"] = "get"
    rec, session = single(cfg, {url: resp(url, 200)}, [url])
    assert ("HEAD", url) not in session.calls and by_url(rec)[url]["method"] == "GET"


def test_redirects_followed_by_hand_and_off_host_not_followed(cfg):
    a, b, c, d = (f"{BASE}/{x}/" for x in "abcd")
    routes = {
        a: resp(a, 301, {"Location": "/b/"}),
        b: resp(b, 200),
        c: resp(c, 302, {"Location": "https://other.test/x"}),
        d: resp(d, 301, {"Location": "/d/"}),
    }
    rec, session = single(cfg, routes, [a, c, d])
    links = by_url(rec)
    assert links[a]["final_url"] == b and links[a]["http_status"] == 200
    assert links[a]["redirect_chain"] == [{"url": a, "status": 301, "location": b, "method": "HEAD"}]
    assert links[c]["status"] == "ok" and links[c]["final_url"] == "https://other.test/x"
    assert "not followed" in links[c]["error"]
    assert ("HEAD", "https://other.test/x") not in session.calls
    assert links[d]["status"] == "failed" and "redirect loop" in links[d]["error"]
    assert rec["counts"]["redirected"] == 3


def test_robots_disallowed_link_is_not_requested(cfg):
    robots = make_response(f"{BASE}/robots.txt", 200, "User-agent: *\nDisallow: /private/\n")
    url, ok = f"{BASE}/private/x", f"{BASE}/a/"
    rec, session = single(cfg, {f"{BASE}/robots.txt": robots, ok: resp(ok)}, [url, ok])
    lk = by_url(rec)[url]
    assert lk["status"] == "disallowed" and "disallows" in lk["error"]
    assert all(u != url for _, u in session.calls)
    assert rec["status"] == "ok" and rec["counts"]["disallowed"] == 1 and not is_broken(lk)


def test_redirect_hop_disallowed(cfg):
    robots = make_response(f"{BASE}/robots.txt", 200, "User-agent: *\nDisallow: /private/\n")
    a = f"{BASE}/a/"
    rec, session = single(cfg, {f"{BASE}/robots.txt": robots, a: resp(a, 301, {"Location": "/private/y"})}, [a])
    lk = by_url(rec)[a]
    assert lk["status"] == "disallowed" and len(lk["redirect_chain"]) == 1


# ---- cap, budget, record status ----------------------------------------------

def test_cap_checks_most_linked_first_and_counts_left_out(cfg):
    cfg["links"]["max_links"] = 2
    pages = [crawl_page(f"{BASE}/", [f"{BASE}/x/", f"{BASE}/y/", f"{BASE}/z/", f"{BASE}/p/"]),
             crawl_page(f"{BASE}/p/", [f"{BASE}/z/"])]
    client, session = make_client(cfg, {f"{BASE}/z/": resp(f"{BASE}/z/"), f"{BASE}/x/": resp(f"{BASE}/x/")})
    rec = collect_links(client, cfg, crawl_rec(pages))
    assert [lk["url"] for lk in rec["links"]] == [f"{BASE}/z/", f"{BASE}/p/", f"{BASE}/x/"]
    assert rec["to_check"] == 2 and rec["left_out_by_cap"] == 1 and rec["from_crawl"] == 1
    assert rec["status"] == "ok"  # the cap is a configured limit, not a failure
    assert any("over links.max_links (2)" in n for n in rec["notes"])


def test_budget_reached_marks_rest_skipped(cfg):
    clock = FakeClock()
    urls = [f"{BASE}/{x}/" for x in "abc"]
    client, session = make_client(cfg, {u: resp(u) for u in urls}, clock)
    rec = collect_links(client, cfg, crawl_rec([crawl_page(f"{BASE}/", urls)]), deadline=1.5)
    statuses = [lk["status"] for lk in rec["links"]]
    assert statuses == ["ok", "ok", "skipped"]  # 1 req/s: robots at 0, a at 1, b at 2 -> c after deadline
    assert rec["status"] == "partial" and "runtime.budget_seconds reached: 1 link(s)" in rec["error"]


def test_crawl_failed_means_links_failed_without_requests(cfg):
    client, session = make_client(cfg, {})
    rec = collect_links(client, cfg, crawl_rec([], status="failed"))
    assert rec["status"] == "failed" and "crawl source failed (crawl problem)" in rec["error"]
    assert session.calls == []
    rec = collect_links(client, cfg, None)
    assert rec["status"] == "failed" and "crawl source missing" in rec["error"]


def test_crawl_partial_makes_links_partial(cfg):
    a = f"{BASE}/a/"
    client, _ = make_client(cfg, {a: resp(a)})
    rec = collect_links(client, cfg, crawl_rec([crawl_page(f"{BASE}/", [a])], status="partial"))
    assert rec["status"] == "partial" and "crawl source partial" in rec["error"]


def test_disabled_sends_nothing(cfg):
    cfg["links"]["enabled"] = False
    client, session = make_client(cfg, {})
    rec = collect_links(client, cfg, crawl_rec([crawl_page(f"{BASE}/", [f"{BASE}/a/"])]))
    assert rec["status"] == "disabled" and rec["links"] == [] and session.calls == []


def test_all_checks_fail_is_failed(cfg):
    cfg["crawler"]["max_retries"] = 0
    a = f"{BASE}/a/"
    rec, _ = single(cfg, {a: requests.ConnectionError("down")}, [a])
    assert rec["status"] == "failed" and rec["error"].startswith("no link could be checked")


def test_linked_from_capped_count_exact(cfg):
    a = f"{BASE}/a/"
    pages = [crawl_page(f"{BASE}/p{i}/", [a]) for i in range(25)]
    client, _ = make_client(cfg, {a: resp(a)})
    lk = collect_links(client, cfg, crawl_rec(pages))["links"][0]
    assert lk["linked_from_count"] == 25 and len(lk["linked_from"]) == 20


def test_5xx_counted_as_broken(cfg):
    a = f"{BASE}/a/"
    cfg["crawler"]["max_retries"] = 0
    rec, _ = single(cfg, {a: [resp(a, 503), resp(a, 503)]}, [a])
    lk = by_url(rec)[a]
    assert lk["status"] == "ok" and lk["http_status"] == 503 and is_broken(lk)
    assert rec["counts"]["http_5xx"] == 1 and rec["status"] == "ok"
