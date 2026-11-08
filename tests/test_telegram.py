"""Telegram delivery (no network: a fake session stands in for the Bot API).

Findings documents come from the real diff engine (builders from test_diff),
so the message is tested against the exact shape diff.py writes.
"""

from __future__ import annotations

import json
import re

import pytest
import requests

from seo_reviewer import main as main_mod
from seo_reviewer import snapshot as snap
from seo_reviewer.deliver import telegram as T
from seo_reviewer.report import load_inputs
from seo_reviewer.summarize import load_summary, template_summary

from conftest import FakeSession, make_response
from test_diff import BASE, HOME, crawl, day_records, findings_for, link, links, page, psi, sitemap, write

TOKEN = "123456:SECRET-token_value"
CHAT = "-1009876543210"
ENV = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}
URL = T.API_URL.format(token=TOKEN)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "snapshots"


def ok(message_id=42):
    return make_response(URL, 200, json.dumps({"ok": True, "result": {"message_id": message_id}}))


def err(status, description, **params):
    body = {"ok": False, "error_code": status, "description": description}
    if params:
        body["parameters"] = params
    return make_response(URL, status, json.dumps(body))


class Recorder(FakeSession):
    def __init__(self, items):
        super().__init__({URL: items})
        self.bodies: list[dict] = []
        self.kwargs: list[dict] = []

    def request(self, method, url, **kwargs):
        self.bodies.append(kwargs.get("json"))
        self.kwargs.append(kwargs)
        return super().request(method, url, **kwargs)


def sleeps():
    out: list[float] = []
    return out, out.append


def visible(text):
    return T.plain_text(text)


# ---- days ---------------------------------------------------------------------------

def stable_day(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    rec = day_records(sitemap=sitemap({f"{BASE}/post-a": "2026-11-01", f"{BASE}/post-b": "2026-11-02",
                                       f"{BASE}/post-c": "2026-11-02"}))
    return findings_for(cfg, root, "2026-11-02", rec), rec


def issues_day(cfg, root, **over):
    write(cfg, root, "2026-11-01", day_records())
    rec = day_records(crawl=crawl([page("/", title="Example Blog"), page("/post-a", noindex=True),
                                   page("/post-b", title=None)]),
                      links=links([link("/tag/x", http=404), link("/about")]), **over)
    return findings_for(cfg, root, "2026-11-02", rec), rec


def gemini_summary(doc, text="F001 is the main problem today."):
    return {"source": "gemini", "model": "gemini-2.5-flash", "text": text}


# ---- message -----------------------------------------------------------------------------

def test_issues_message_header_findings_sources_and_link(cfg, root):
    doc, rec = issues_day(cfg, root)
    msg = T.build_message(cfg, doc, gemini_summary(doc), rec)
    text = msg["text"]
    first = text.splitlines()[0]
    assert first == "\U0001F534 <b>Example · 2026-11-02 · Critical</b>"
    c = doc["counts"]
    assert f"{c['critical']} critical, {c['warning']} warning, {c['info']} info · compared with 2026-11-01" in text
    assert "<b>AI summary</b>\nF001 is the main problem today." in text
    noindex = next(f for f in doc["findings"] if f["rule"] == "page_noindex")
    assert f"<b>critical</b> · {noindex['check']} (new, regression)" in text
    assert f"{BASE}/post-a" in text
    assert "<code>'indexable' -&gt; 'meta-robots'</code>" in text  # old -> new, escaped
    assert f"Next: {T.h(noindex['suggested_next_step'])}" in text
    assert f"Linked from 3 page(s), e.g. {HOME}" in text  # broken link: where it is linked from
    assert "Sources: robots.txt ok · Sitemap ok" in text and "Crawl ok" in text and "PSI ok" in text
    assert text.endswith(f'<a href="{cfg["delivery"]["report_base_url"]}/2026-11-02.md">Full report</a>')
    # same order as findings.json (critical first, regressions first), capped at max_findings_in_message
    assert msg["findings"] == [f["id"] for f in doc["findings"]][:cfg["delivery"]["telegram"]["max_findings_in_message"]]
    assert T.tg_len(text) <= T.MAX_CHARS


def test_max_findings_cap_says_top_n_of_total(cfg, root):
    cfg["delivery"]["telegram"]["max_findings_in_message"] = 1
    doc, rec = issues_day(cfg, root)
    msg = T.build_message(cfg, doc, None, rec)
    assert msg["findings"] == [doc["findings"][0]["id"]]
    assert f"<b>Findings</b> (top 1 of {len(doc['findings'])})" in msg["text"]


def test_template_summary_is_one_word_not_repeated(cfg, root):
    doc, rec = issues_day(cfg, root)
    summary = {"source": "template", "reason": "GEMINI_API_KEY is not set", "text": template_summary(doc)}
    text = T.build_message(cfg, doc, summary, rec)["text"]
    assert "Summary: template" in text
    assert "AI summary" not in text and "Top findings" not in text and "GEMINI_API_KEY" not in text


def test_every_number_and_url_comes_from_the_data(cfg, root):
    doc, rec = issues_day(cfg, root)
    text = visible(T.build_message(cfg, doc, None, rec)["text"])
    data = json.dumps([doc, rec], ensure_ascii=False) + " 2026-11-02 " + cfg["delivery"]["report_base_url"]
    for u in re.findall(r"https?://\S+", text):
        assert u.rstrip(".,)") in data, u
    for n in re.findall(r"\d+", text):
        assert n in data, n


def test_html_escaping_of_site_text(cfg, root):
    cfg["site"]["name"] = "A&B <Blog>"
    write(cfg, root, "2026-11-01", day_records())
    rec = day_records(crawl=crawl([page("/", title="Example Blog"), page("/post-a", title=None)]))
    doc = findings_for(cfg, root, "2026-11-02", rec)
    doc["findings"][0]["message"] = "<script>alert(1)</script> & more"
    text = T.build_message(cfg, doc, gemini_summary(doc, "Title <b>gone</b> & F001"), rec)["text"]
    assert "A&amp;B &lt;Blog&gt;" in text
    assert "<script>" not in text and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more" in text
    assert "Title &lt;b&gt;gone&lt;/b&gt; &amp; F001" in text
    tags = set(re.findall(r"</?([a-z]+)", text))
    assert tags <= {"b", "code", "a"}  # only tags we wrote


def test_all_stable_is_two_lines(cfg, root):
    doc, rec = stable_day(cfg, root)
    assert doc["status"] == "stable"
    text = T.build_message(cfg, doc, gemini_summary(doc), rec)["text"]
    lines = text.splitlines()
    assert len(lines) == 2
    assert lines[0] == "✅ <b>Example · 2026-11-02 · All stable</b>, compared with 2026-11-01 (1 info)"
    assert lines[1].startswith("Sources: robots.txt ok") and lines[1].endswith("Full report</a>")
    assert "AI summary" not in text


def test_failed_source_is_explicit(cfg, root):
    doc, rec = issues_day(cfg, root, psi=psi(runs=[], status="failed", error="HTTP 403: blocked"))
    text = T.build_message(cfg, doc, None, rec)["text"]
    assert ("⚠️ PSI failed: HTTP 403: blocked; Core Web Vitals and PageSpeed scores "
            "not compared today.") in text
    findings_part = text.split("<b>Findings</b>")[1].split("Sources:")[0]
    assert "PSI" not in findings_part  # not repeated as a finding
    assert "PSI failed" in text.split("Sources:")[1]  # status in the sources line


def test_upstream_failure_says_not_run(cfg, root):
    rec = day_records(robots={"source": "robots", "status": "failed", "error": "timeout", "mode": None,
                              "allowed": {}, "sitemaps_declared": []},
                      crawl=crawl([], status="failed"), links=links(status="failed"))
    doc = findings_for(cfg, root, "2026-11-01", rec)
    text = T.build_message(cfg, doc, None, rec)["text"]
    assert "ROBOTS failed: timeout" in text and "CRAWL not run: robots failed" in text
    assert "(baseline run)" in text.splitlines()[0]


def test_grouped_finding_shows_count_and_example(cfg, root):
    pages = [page("/", title="Example Blog")] + [page(f"/p{i}", title="short") for i in range(8)]
    doc = findings_for(cfg, root, "2026-11-01", day_records(crawl=crawl(pages)))
    g = next(f for f in doc["findings"] if f["rule"] == "title_length")
    assert g["count"] == 9
    text = T.build_message(cfg, doc, None, {})["text"]
    assert f"9 URLs, e.g. {HOME}" in text
    assert text.splitlines()[0].endswith("Baseline run</b>")


def test_length_cap_drops_findings_then_shortens_summary(cfg, root):
    doc, rec = issues_day(cfg, root)
    full = T.build_message(cfg, doc, gemini_summary(doc), rec)
    small = T.build_message(cfg, doc, gemini_summary(doc), rec, limit=T.tg_len(full["text"]) - 50)
    assert T.tg_len(small["text"]) <= T.tg_len(full["text"]) - 50
    assert len(small["findings"]) < len(full["findings"])
    assert "AI summary" in small["text"] and "Full report" in small["text"]
    long_summary = gemini_summary(doc, "F001 " + "word " * 2000)
    msg = T.build_message(cfg, doc, long_summary, rec)
    assert T.tg_len(msg["text"]) <= T.MAX_CHARS
    assert msg["findings"] == [] and "..." in msg["text"] and msg["text"].endswith("Full report</a>")


def test_tg_len_counts_utf16():
    assert T.tg_len("\U0001F534") == 2 and T.tg_len("abc") == 3


# ---- sending -----------------------------------------------------------------------------

def test_send_ok_request_shape(cfg, root):
    doc, rec = issues_day(cfg, root)
    s = Recorder([ok(7)])
    out = T.deliver(cfg, doc, None, rec, env=ENV, session=s, sleep=lambda x: None)
    assert out["status"] == "sent" and out["message_id"] == 7 and out["attempts"] == 1
    body = s.bodies[0]
    assert body["chat_id"] == CHAT and body["parse_mode"] == "HTML"
    assert body["link_preview_options"] == {"is_disabled": True}
    assert body["text"] == out["text"]
    assert s.kwargs[0]["timeout"] == (T.CONNECT_TIMEOUT, 20.0)
    assert TOKEN not in json.dumps(out) and CHAT not in json.dumps(out)


def test_429_with_short_retry_after_is_retried(cfg, root):
    doc, rec = issues_day(cfg, root)
    waited, sleep = sleeps()
    s = Recorder([err(429, "Too Many Requests: retry after 3", retry_after=3), ok()])
    out = T.deliver(cfg, doc, None, rec, env=ENV, session=s, sleep=sleep)
    assert out["status"] == "sent" and out["attempts"] == 2 and waited == [3.0]


def test_429_with_long_retry_after_fails_without_waiting(cfg, root):
    doc, rec = issues_day(cfg, root)
    waited, sleep = sleeps()
    s = Recorder([err(429, "Too Many Requests: retry after 120", retry_after=120)])
    out = T.deliver(cfg, doc, None, rec, env=ENV, session=s, sleep=sleep)
    assert out["status"] == "failed" and out["http_status"] == 429 and waited == []
    assert "rate limit (HTTP 429)" in out["reason"] and "longer than 30 s" in out["reason"]
    assert out["text"]  # the message is still recorded


def test_5xx_retried_once_then_fails(cfg, root):
    doc, rec = issues_day(cfg, root)
    s = Recorder([err(502, "Bad Gateway"), err(502, "Bad Gateway")])
    out = T.deliver(cfg, doc, None, rec, env=ENV, session=s, sleep=lambda x: None)
    assert out["status"] == "failed" and out["attempts"] == 2 and "HTTP 502" in out["reason"]
    s2 = Recorder([err(503, "x"), ok()])
    assert T.deliver(cfg, doc, None, rec, env=ENV, session=s2, sleep=lambda x: None)["status"] == "sent"


def test_timeout_retried_once_then_fails(cfg, root):
    doc, rec = issues_day(cfg, root)
    s = Recorder([requests.Timeout("read timed out")])
    out = T.deliver(cfg, doc, None, rec, env=ENV, session=s, sleep=lambda x: None)
    assert out["status"] == "failed" and out["attempts"] == 2
    assert out["reason"] == "Telegram timed out after 20 s (tried twice)"


def test_connection_error_text_is_scrubbed(cfg, root):
    doc, rec = issues_day(cfg, root)
    boom = requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage chat {CHAT}")
    out = T.deliver(cfg, doc, None, rec, env=ENV, session=Recorder([boom]), sleep=lambda x: None)
    assert out["status"] == "failed"
    assert TOKEN not in out["reason"] and CHAT not in out["reason"] and "***" in out["reason"]


def test_bad_chat_id_is_not_retried(cfg, root):
    doc, rec = issues_day(cfg, root)
    s = Recorder([err(400, "Bad Request: chat not found")])
    out = T.deliver(cfg, doc, None, rec, env=ENV, session=s, sleep=lambda x: None)
    assert out["status"] == "failed" and out["attempts"] == 1
    assert out["reason"] == "Telegram HTTP 400: Bad Request: chat not found"
    s = Recorder([err(401, "Unauthorized")])
    assert T.deliver(cfg, doc, None, rec, env=ENV, session=s, sleep=lambda x: None)["http_status"] == 401


def test_html_rejected_falls_back_to_plain_text(cfg, root):
    doc, rec = issues_day(cfg, root)
    s = Recorder([err(400, "Bad Request: can't parse entities: unexpected end tag"), ok()])
    out = T.deliver(cfg, doc, None, rec, env=ENV, session=s, sleep=lambda x: None)
    assert out["status"] == "sent" and out["plain_text_fallback"]
    assert "parse_mode" not in s.bodies[1] and "<b>" not in s.bodies[1]["text"]
    assert "Example · 2026-11-02" in s.bodies[1]["text"]


@pytest.mark.parametrize("env,missing", [
    ({}, "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID not set"),
    ({"TELEGRAM_BOT_TOKEN": TOKEN}, "TELEGRAM_CHAT_ID not set"),
    ({"TELEGRAM_BOT_TOKEN": "  ", "TELEGRAM_CHAT_ID": CHAT}, "TELEGRAM_BOT_TOKEN not set"),
])
def test_missing_secrets_skip_without_network(cfg, root, env, missing):
    doc, rec = issues_day(cfg, root)
    out = T.deliver(cfg, doc, None, rec, env=env, session=FakeSession({}))
    assert out["status"] == "skipped" and out["reason"] == f"not sent: {missing}"
    assert out["text"]


def test_disabled_and_no_deliver_skip_without_network(cfg, root):
    doc, rec = issues_day(cfg, root)
    out = T.deliver(cfg, doc, None, rec, send=False, env=ENV, session=FakeSession({}))
    assert out["reason"] == "not sent: --no-deliver (dry run)"
    cfg["delivery"]["telegram"]["enabled"] = False
    out = T.deliver(cfg, doc, None, rec, env=ENV, session=FakeSession({}))
    assert out["status"] == "skipped" and "enabled is false" in out["reason"]


def test_format_delivery_lines():
    rec = {"status": "failed", "reason": "Telegram HTTP 400: Bad Request: chat not found", "text": "<b>a &amp; b</b>",
           "finding_ids": [], "findings_total": 0, "chars": 1}
    lines = T.format_delivery(rec)
    assert lines[0].startswith("DELIVERY FAILED: Telegram HTTP 400") and lines[-1] == "    a & b"


# ---- main.py wiring --------------------------------------------------------------------

def _write_day(cfg, root, day="2026-11-02"):
    doc, rec = issues_day(cfg, root)
    from seo_reviewer.diff import write_findings
    write_findings(root, day, doc)
    return doc


def test_run_deliver_writes_delivery_json_and_diff_ignores_it(cfg, tmp_path):
    root = snap.snapshot_root(cfg, tmp_path)
    doc = _write_day(cfg, root)
    s = Recorder([ok(9)])
    out = main_mod.run_deliver(cfg, tmp_path, "2026-11-02", tg_kwargs={"env": ENV, "session": s})
    saved = snap.read_source(root, "2026-11-02", "delivery")
    assert saved == out["delivery"] and saved["status"] == "sent" and saved["message_id"] == 9
    assert saved["summary_source"] == "template"  # no summary.json -> template record
    from seo_reviewer.diff import build_findings
    again = build_findings(cfg, root, "2026-11-02")  # delivery.json is not read as a source
    assert set(again["sources"]) == set(doc["sources"])


def test_cli_deliver_only_and_no_deliver(cfg, tmp_path, capsys, monkeypatch):
    import yaml
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    root = snap.snapshot_root(cfg, tmp_path)
    _write_day(cfg, root)
    assert main_mod.main(["--deliver-only", "--no-deliver", "--config", str(cfg_path), "--date", "2026-11-02"]) == 0
    out = capsys.readouterr().out
    assert "Telegram: not sent: --no-deliver (dry run)" in out and "Example · 2026-11-02 · Critical" in out
    # no secrets in the environment (conftest): skipped with a clear line, exit 0
    assert main_mod.main(["--deliver-only", "--config", str(cfg_path), "--date", "2026-11-02"]) == 0
    assert "not sent: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID not set" in capsys.readouterr().out
    assert main_mod.main(["--deliver-only", "--config", str(cfg_path), "--date", "2026-11-03"]) == 1
    assert main_mod.main(["--report-only", "--no-deliver", "--config", str(cfg_path)]) == 2
    assert main_mod.main(["--deliver-only", "--compare-to", "2026-11-01", "--config", str(cfg_path)]) == 2


def test_delivery_failure_does_not_lose_the_report(cfg, tmp_path, monkeypatch):
    from test_main import client_for, link_routes, psi_kwargs
    cfg["crawler"]["max_pages"] = 2
    s = Recorder([err(500, "Internal"), requests.Timeout("x")])
    res = main_mod.run_daily(cfg, tmp_path, day="2026-11-07", client=client_for(cfg, link_routes()),
                             psi_kwargs=psi_kwargs(), tg_kwargs={"env": ENV, "session": s, "sleep": lambda x: None})
    assert res["report_path"].is_file()
    assert res["delivery"]["status"] == "failed" and res["delivery_path"].is_file()
    assert "DELIVERY FAILED" in main_mod.format_summary(res)


def test_delivery_crash_is_caught(cfg, tmp_path, monkeypatch):
    from test_main import client_for, link_routes, psi_kwargs
    cfg["crawler"]["max_pages"] = 2

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(main_mod, "deliver", boom)
    res = main_mod.run_daily(cfg, tmp_path, day="2026-11-07", client=client_for(cfg, link_routes()),
                             psi_kwargs=psi_kwargs())
    assert res["report_path"].is_file()
    assert res["delivery"]["reason"] == "delivery crashed: RuntimeError: bug"
    assert "DELIVERY FAILED: delivery crashed" in main_mod.format_summary(res)


def test_config_requires_html_parse_mode(cfg):
    from seo_reviewer.config import ConfigError, validate_config
    cfg["delivery"]["telegram"]["parse_mode"] = "MarkdownV2"
    with pytest.raises(ConfigError, match="only 'HTML' is supported"):
        validate_config(cfg)
