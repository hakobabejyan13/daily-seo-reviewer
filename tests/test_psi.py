"""PSI collector tests: fake session, fixture JSON, no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from seo_reviewer.collectors import psi

from conftest import FakeClock, make_response

FIX = Path(__file__).parent / "fixtures" / "psi"
HOME = "https://example.test/"
KEY = "test-key-123"


def fixture(name: str) -> str:
    return (FIX / f"{name}.json").read_text(encoding="utf-8")


def resp(name: str, status: int = 200) -> requests.Response:
    return make_response(psi.PSI_ENDPOINT, status, fixture(name), {"Content-Type": "application/json"})


class PsiSession:
    """Fake session for the PSI endpoint: routes (url, strategy) -> responses in order.

    An item may be a Response, an Exception, or a number of seconds the fake clock
    advances before the next item is served (to simulate slow calls).
    """

    def __init__(self, routes: dict, clock: FakeClock | None = None):
        self.routes = {k: list(v) if isinstance(v, list) else [v] for k, v in routes.items()}
        self.clock = clock
        self.headers: dict = {}
        self.calls: list[dict] = []

    def request(self, method, url, params=None, timeout=None, **kw):
        assert method == "GET" and url == psi.PSI_ENDPOINT
        self.calls.append({"params": dict(params), "timeout": timeout})
        key = (params["url"], params["strategy"])
        if key not in self.routes:
            raise AssertionError(f"unexpected PSI call {key}")
        queue = self.routes[key]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        while isinstance(item, (int, float)):
            self.clock.now += item
            item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def run(cfg, routes, deadline=None, clock=None, env=None):
    clock = clock or FakeClock()
    session = PsiSession(routes, clock)
    rec = psi.collect_psi(cfg, deadline=deadline, env={"PSI_API_KEY": KEY} if env is None else env,
                          session=session, clock=clock, sleep=clock.sleep)
    return rec, session, clock


def test_ok_mobile_and_desktop(cfg):
    rec, session, _ = run(cfg, {(HOME, "mobile"): resp("mobile_ok"), (HOME, "desktop"): resp("desktop_origin_fallback")})
    assert rec["status"] == "ok" and rec["error"] is None
    assert rec["urls"] == [HOME] and rec["strategies"] == ["mobile", "desktop"]
    assert rec["counts"] == {"ok": 2, "failed": 0, "skipped": 0, "no_url_field_data": 1}
    assert rec["requests_made"] == 2
    m, d = rec["runs"]
    assert (m["strategy"], d["strategy"]) == ("mobile", "desktop")
    assert m["lab"] == {"performance_score": 62, "lcp_ms": 4124, "cls": 0.0412, "tbt_ms": 356}
    assert m["lighthouse_version"] == "12.8.2"
    assert m["fetched_at"] == "2026-11-08T05:01:12.345Z"
    assert m["final_url"] == HOME
    assert m["run_warnings"] == ["The page loaded too slowly to finish within the time limit."]
    assert m["field"]["url"] == {"overall_category": "AVERAGE", "metrics": {
        "lcp_ms": {"percentile": 2710, "category": "AVERAGE"},
        "inp_ms": {"percentile": 180, "category": "FAST"},
        "cls": {"percentile": 0.05, "category": "FAST"}}}
    assert m["field"]["origin"]["metrics"]["cls"] == {"percentile": 0.03, "category": "FAST"}
    assert m["field"]["notes"] == []
    # Desktop: PSI sent origin data as URL data (origin_fallback) -> stored as no URL data.
    assert d["lab"]["performance_score"] == 91
    assert d["field"]["url"] is None
    assert d["field"]["origin"]["overall_category"] == "FAST"
    assert "fell back to origin data" in d["field"]["notes"][0]
    # Request shape: key and category sent, read timeout from config.
    p = session.calls[0]["params"]
    assert p == {"url": HOME, "strategy": "mobile", "category": "performance", "key": KEY}
    assert session.calls[0]["timeout"] == (psi.CONNECT_TIMEOUT, 60.0)


def test_key_never_in_record(cfg):
    boom = requests.ConnectionError(f"Max retries exceeded with url: /runPagespeed?key={KEY}&url=x")
    rec, _, _ = run(cfg, {(HOME, "mobile"): resp("mobile_ok"), (HOME, "desktop"): boom})
    text = json.dumps(rec)
    assert KEY not in text
    assert "key=***" in rec["runs"][1]["error"]
    assert rec["status"] == "partial"


def test_no_field_data_is_explicit(cfg):
    cfg["psi"].update(urls=["/new-post/"], strategies=["mobile"])
    rec, _, _ = run(cfg, {("https://example.test/new-post/", "mobile"): resp("no_field_data")})
    r = rec["runs"][0]
    assert r["status"] == "ok"
    assert r["field"] == {"url": None, "origin": None, "notes": [
        "no field data (CrUX) for this URL", "no field data (CrUX) for the origin"]}
    assert rec["counts"]["no_url_field_data"] == 1


def test_missing_key_fails_without_request(cfg):
    session = PsiSession({})
    rec = psi.collect_psi(cfg, env={}, session=session)
    assert rec["status"] == "failed"
    assert rec["error"] == "no API key: environment variable PSI_API_KEY is not set"
    assert [r["status"] for r in rec["runs"]] == ["failed", "failed"]
    assert session.calls == [] and rec["requests_made"] == 0


def test_blank_key_counts_as_missing(cfg):
    rec, session, _ = run(cfg, {}, env={"PSI_API_KEY": "  "})
    assert rec["status"] == "failed" and "not set" in rec["error"] and session.calls == []


def test_disabled(cfg):
    cfg["psi"]["enabled"] = False
    rec, session, _ = run(cfg, {})
    assert rec["status"] == "disabled" and rec["runs"] == [] and session.calls == []


def test_retry_on_429_then_ok(cfg):
    cfg["psi"]["strategies"] = ["mobile"]
    rec, session, clock = run(cfg, {(HOME, "mobile"): [resp("error_429", 429), resp("mobile_ok")]})
    r = rec["runs"][0]
    assert r["status"] == "ok" and r["attempts"] == 2 and r["http_status"] == 200
    assert clock.sleeps == [5.0]
    assert rec["status"] == "ok"


def test_429_twice_fails_with_google_message(cfg):
    cfg["psi"]["strategies"] = ["mobile"]
    rec, session, _ = run(cfg, {(HOME, "mobile"): resp("error_429", 429)})
    r = rec["runs"][0]
    assert r["status"] == "failed" and r["attempts"] == 2 and r["http_status"] == 429
    assert r["error"].startswith("HTTP 429: Quota exceeded for quota metric 'Queries'")
    assert rec["status"] == "failed"
    assert rec["error"].startswith("no PSI run succeeded: HTTP 429")


def test_400_and_403_are_not_retried(cfg):
    rec, session, _ = run(cfg, {(HOME, "mobile"): resp("error_400", 400), (HOME, "desktop"): resp("error_403", 403)})
    assert [r["attempts"] for r in rec["runs"]] == [1, 1]
    assert "FAILED_DOCUMENT_REQUEST" in rec["runs"][0]["error"]
    assert rec["runs"][1]["error"].startswith("HTTP 403: Requests to this API")
    assert rec["status"] == "failed"
    assert len(rec["partial_reasons"]) == 2


def test_timeout_retried_then_failed(cfg):
    cfg["psi"]["strategies"] = ["mobile"]
    rec, session, _ = run(cfg, {(HOME, "mobile"): requests.ReadTimeout("read timed out")})
    r = rec["runs"][0]
    assert r["status"] == "failed" and r["attempts"] == 2 and r["error"] == "timeout after 60 s"
    assert r["http_status"] is None


def test_lighthouse_runtime_error_is_failed(cfg):
    cfg["psi"]["strategies"] = ["mobile"]
    rec, _, _ = run(cfg, {(HOME, "mobile"): resp("runtime_error")})
    r = rec["runs"][0]
    assert r["status"] == "failed"
    assert r["error"] == "Lighthouse error: NO_FCP: The page did not paint any content."
    assert r["lab"] is None


def test_unreadable_response(cfg):
    cfg["psi"]["strategies"] = ["mobile"]
    rec, _, _ = run(cfg, {(HOME, "mobile"): make_response(psi.PSI_ENDPOINT, 200, "<html>oops</html>")})
    assert rec["runs"][0]["status"] == "failed"
    assert rec["runs"][0]["error"].startswith("unreadable PSI response")
    rec, _, _ = run(cfg, {(HOME, "mobile"): make_response(psi.PSI_ENDPOINT, 200, '{"kind": "x"}')})
    assert rec["runs"][0]["error"] == "unreadable PSI response: PSI response has no lighthouseResult"


def test_budget_skips_calls_that_cannot_finish(cfg):
    # deadline 100 s, timeout 60 s: the first call (takes 45 s) may start at 0,
    # the second would start at 45 and could end at 105 > 100, so it is skipped.
    rec, session, _ = run(cfg, {(HOME, "mobile"): [45, resp("mobile_ok")]}, deadline=100)
    assert [r["status"] for r in rec["runs"]] == ["ok", "skipped"]
    assert rec["runs"][0]["duration_s"] == 45.0
    assert rec["status"] == "partial"
    assert rec["partial_reasons"] == ["runtime.budget_seconds reached: 1 run(s) not started"]
    assert len(session.calls) == 1


def test_budget_blocks_retry(cfg):
    cfg["psi"]["strategies"] = ["mobile"]
    rec, session, _ = run(cfg, {(HOME, "mobile"): [30, resp("error_429", 429), resp("mobile_ok")]}, deadline=80)
    r = rec["runs"][0]
    assert r["status"] == "failed" and r["attempts"] == 1
    assert r["error"].endswith("no time left in runtime.budget_seconds to retry")


def test_nothing_fits_budget(cfg):
    rec, session, _ = run(cfg, {}, deadline=30)
    assert rec["status"] == "failed" and session.calls == []
    assert "runtime.budget_seconds would be exceeded" in rec["error"]


def test_select_urls(cfg):
    cfg["psi"]["urls"] = ["/", "https://example.test/a/", "a/", "https://other.test/x", HOME]
    urls, notes = psi.select_urls(cfg)
    assert urls == [HOME, "https://example.test/a/"]
    assert notes == ["psi.urls entry 'https://other.test/x' is not on example.test; ignored"]
    cfg["psi"]["urls"] = []
    assert psi.select_urls(cfg) == ([HOME], [])


def test_only_other_host_urls(cfg):
    cfg["psi"]["urls"] = ["https://other.test/"]
    rec, _, _ = run(cfg, {})
    assert rec["status"] == "failed" and "every psi.urls entry was ignored" in rec["error"]


@pytest.mark.parametrize("percentile,expected", [(0, 0.0), (5, 0.05), (12, 0.12), (250, 2.5)])
def test_field_cls_percentile_scaling(percentile, expected):
    exp = {"metrics": {"CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": percentile, "category": "FAST"}}}
    f = psi.parse_field(exp)
    assert f["metrics"]["cls"]["percentile"] == expected
    assert f["metrics"]["lcp_ms"] is None and f["metrics"]["inp_ms"] is None


def test_lab_metrics_missing_audit():
    lab = psi.parse_lab({"categories": {"performance": {"score": 1}}, "audits": {}})
    assert lab == {"performance_score": 100, "lcp_ms": None, "cls": None, "tbt_ms": None}
