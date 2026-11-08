import pytest
import requests

from seo_reviewer.collectors.robots import collect_robots, parse_robots
from seo_reviewer.http_client import FetchError, PoliteClient, RobotsDisallowed

from conftest import FakeClock, FakeSession, make_response

UA = "daily-seo-reviewer/0.1 (+https://github.com/example/daily-seo-reviewer)"
ROBOTS = "https://example.test/robots.txt"

ROBOTS_TXT = """\
User-agent: *
Disallow: /private/
Sitemap: https://example.test/sitemap.xml

User-agent: daily-seo-reviewer
Disallow: /no-reviewer/
"""


def make_client(cfg, routes, **overrides):
    crawler = dict(cfg["crawler"], user_agent=UA, **overrides)
    clock = FakeClock()
    session = FakeSession(routes)
    client = PoliteClient(crawler, session=session, clock=clock, sleep=clock.sleep)
    return client, session, clock


# ---- parsing (pure) ----------------------------------------------------

def test_parse_robots_rules_and_sitemaps():
    rb = parse_robots(ROBOTS, ROBOTS_TXT)
    assert rb.sitemaps == ["https://example.test/sitemap.xml"]
    assert rb.can_fetch(UA, "https://example.test/")
    assert rb.can_fetch(UA, "https://example.test/private/x")  # our own group applies, not *
    assert not rb.can_fetch(UA, "https://example.test/no-reviewer/x")
    assert not rb.can_fetch("OtherBot/1.0", "https://example.test/private/x")


def test_parse_robots_disallow_all():
    rb = parse_robots(ROBOTS, "User-agent: *\nDisallow: /\n")
    assert not rb.can_fetch(UA, "https://example.test/")


def test_parse_robots_empty_allows_all():
    rb = parse_robots(ROBOTS, "")
    assert rb.can_fetch(UA, "https://example.test/anything")


# ---- fetching via the client -------------------------------------------

@pytest.mark.parametrize("status,mode,status_field,allowed", [
    (404, "allow_all", "ok", True),
    (410, "allow_all", "ok", True),
    (401, "disallow_all", "ok", False),
    (403, "disallow_all", "ok", False),
    (500, "disallow_all", "failed", False),
])
def test_robots_http_status_handling(cfg, status, mode, status_field, allowed):
    client, _, _ = make_client(cfg, {ROBOTS: make_response(ROBOTS, status)}, max_retries=0)
    rec = collect_robots(client, "https://example.test", ["https://example.test/"])
    assert rec["mode"] == mode
    assert rec["status"] == status_field
    assert rec["allowed"] == {"https://example.test/": allowed}


def test_robots_network_failure_is_recorded(cfg):
    client, _, _ = make_client(cfg, {ROBOTS: requests.ConnectionError("boom")})
    rec = collect_robots(client, "https://example.test", ["https://example.test/"])
    assert rec["status"] == "failed"
    assert "boom" in rec["error"]
    assert rec["allowed"]["https://example.test/"] is False


def test_client_enforces_robots_and_fetches_it_once(cfg):
    routes = {
        ROBOTS: make_response(ROBOTS, 200, ROBOTS_TXT),
        "https://example.test/": make_response("https://example.test/", 200, "<html></html>"),
    }
    client, session, _ = make_client(cfg, routes)
    assert client.get("https://example.test/").status_code == 200
    assert client.get("https://example.test/").status_code == 200
    with pytest.raises(RobotsDisallowed):
        client.get("https://example.test/no-reviewer/page")
    assert [u for _, u in session.calls].count(ROBOTS) == 1
    assert ("GET", "https://example.test/no-reviewer/page") not in session.calls


def test_user_agent_header_is_set(cfg):
    client, session, _ = make_client(cfg, {})
    assert session.headers["User-Agent"] == UA


def test_rate_limit_spaces_requests(cfg):
    routes = {ROBOTS: make_response(ROBOTS, 404),
              "https://example.test/a": make_response("https://example.test/a", 200, "x")}
    client, _, clock = make_client(cfg, routes, requests_per_second=0.5)
    client.get("https://example.test/a")  # robots + page = 2 requests
    client.get("https://example.test/a")
    assert clock.sleeps == [2.0, 2.0]


def test_retry_once_on_5xx_then_success(cfg):
    url = "https://example.test/a"
    routes = {ROBOTS: make_response(ROBOTS, 404),
              url: [make_response(url, 503), make_response(url, 200, "ok")]}
    client, session, _ = make_client(cfg, routes, max_retries=1)
    assert client.get(url).status_code == 200
    assert session.calls.count(("GET", url)) == 2


def test_retry_exhausted_returns_last_error_status(cfg):
    url = "https://example.test/a"
    routes = {ROBOTS: make_response(ROBOTS, 404), url: make_response(url, 503)}
    client, session, _ = make_client(cfg, routes, max_retries=1)
    assert client.get(url).status_code == 503
    assert session.calls.count(("GET", url)) == 2


def test_timeout_after_retries_raises_fetch_error(cfg):
    url = "https://example.test/a"
    routes = {ROBOTS: make_response(ROBOTS, 404), url: requests.Timeout("slow")}
    client, session, _ = make_client(cfg, routes, max_retries=1)
    with pytest.raises(FetchError, match="2 attempt"):
        client.get(url)
    assert session.calls.count(("GET", url)) == 2


def test_no_retry_on_404(cfg):
    url = "https://example.test/a"
    routes = {ROBOTS: make_response(ROBOTS, 404), url: make_response(url, 404)}
    client, session, _ = make_client(cfg, routes, max_retries=1)
    assert client.get(url).status_code == 404
    assert session.calls.count(("GET", url)) == 1


def test_failed_robots_blocks_crawl_with_clear_reason(cfg):
    routes = {ROBOTS: make_response(ROBOTS, 503)}
    client, session, _ = make_client(cfg, routes, max_retries=0)
    with pytest.raises(RobotsDisallowed, match="could not be fetched"):
        client.get("https://example.test/")
    assert ("GET", "https://example.test/") not in session.calls
