"""LLM summarizer (no network): fake Gemini session, validator, template fallback."""

from __future__ import annotations

import json

import pytest
import requests

from seo_reviewer import diff as D
from seo_reviewer import report as RP
from seo_reviewer import summarize as S

from conftest import make_response
from test_diff import BASE, crawl, day_records, findings_for, link, links, page, psi, write

KEY = "AIza-test-key-123"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"


class GeminiSession:
    """Serves queued answers for the Gemini endpoint and records each request."""

    def __init__(self, *answers, url=ENDPOINT):
        self.url = url
        self.answers = list(answers)
        self.requests: list[dict] = []

    def request(self, method, url, **kw):
        self.requests.append({"method": method, "url": url, **kw})
        assert url == self.url, url
        item = self.answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def answer(text, finish="STOP", usage=None, status=200):
    body = {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}],
            "usageMetadata": usage or {"promptTokenCount": 900, "candidatesTokenCount": 60, "totalTokenCount": 960}}
    return make_response(ENDPOINT, status, json.dumps(body), {"Content-Type": "application/json"})


def error(status, message):
    body = {"error": {"code": status, "message": message, "status": "X"}}
    return make_response(ENDPOINT, status, json.dumps(body), {"Content-Type": "application/json"})


@pytest.fixture
def root(tmp_path):
    return tmp_path / "snapshots"


@pytest.fixture
def issues_doc(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    rec = day_records(crawl=crawl([page("/", title="Example Blog"), page("/post-a", noindex=True),
                                   page("/post-b", title=None)]),
                      links=links([link("/tag/x", http=404), link("/about")]))
    doc = findings_for(cfg, root, "2026-11-02", rec)
    D.write_findings(root, "2026-11-02", doc)
    assert doc["status"] == "issues"
    return doc


def good_text(doc):
    crit = next(f for f in doc["findings"] if f["severity"] == "critical")
    return (f"{doc['counts']['critical']} critical issue today: {crit['id']} {crit['check']} on {crit['url']}.\n"
            f"Compared with 2026-11-01.")


def run(cfg, doc, *answers, env=None):
    session = GeminiSession(*answers)
    rec = S.summarize(cfg, doc, env={"GEMINI_API_KEY": KEY} if env is None else env,
                      session=session, sleep=lambda s: None)
    return rec, session


# ---- request ---------------------------------------------------------------------------

def test_ok_uses_gemini_and_request_is_capped(cfg, issues_doc):
    rec, session = run(cfg, issues_doc, answer(good_text(issues_doc)))
    assert rec["source"] == "gemini" and rec["reason"] is None
    assert rec["model"] == "gemini-2.5-flash"
    assert rec["text"] == good_text(issues_doc)
    assert rec["finding_ids"] == [next(f["id"] for f in issues_doc["findings"] if f["severity"] == "critical")]
    assert rec["usage"]["prompt_tokens"] == 900 and rec["usage"]["thought_tokens"] is None
    (req,) = session.requests
    assert req["method"] == "POST"
    assert req["headers"]["X-goog-api-key"] == KEY
    gen = req["json"]["generationConfig"]
    assert gen["maxOutputTokens"] == cfg["llm"]["max_output_tokens"] == 800
    assert gen["thinkingConfig"] == {"thinkingBudget": 0}
    assert req["timeout"][1] == cfg["llm"]["timeout_seconds"]
    prompt = req["json"]["contents"][0]["parts"][0]["text"]
    assert "ONLY the findings in the input" in prompt and "Do not suggest next steps" in prompt
    assert "suggested_next_step" not in prompt  # the model never sees the rules' next steps
    assert KEY not in json.dumps(rec)


def test_prompt_is_compact(cfg, issues_doc):
    doc = json.loads(json.dumps(issues_doc))
    doc["findings"][0]["items"] = [{"url": f"{BASE}/p{i}", "new_value": i} for i in range(50)]
    data = json.loads(S.prompt_data(doc))
    assert data["findings"][0]["example_urls"] == [f"{BASE}/p0", f"{BASE}/p1", f"{BASE}/p2"]
    assert "items" not in data["findings"][0] and "details" not in data["findings"][0]


# ---- fallbacks ---------------------------------------------------------------------------

@pytest.mark.parametrize("answers, reason", [
    ((error(429, "Resource has been exhausted (e.g. check quota)."),), "Gemini rate limit (HTTP 429): Resource has been exhausted"),
    ((error(503, "The model is overloaded."), error(500, "Internal error")), "Gemini HTTP 500: Internal error"),
    ((error(400, "Unknown name thinkingConfig"),), "Gemini HTTP 400: Unknown name thinkingConfig"),
    ((requests.Timeout("read timed out"), requests.Timeout("again")), "Gemini timed out after 30 s (tried twice)"),
    ((requests.ConnectionError("boom"),), "Gemini request failed: ConnectionError: boom"),
    ((answer(""),), "Gemini returned an empty answer"),
    ((answer("cut off", finish="MAX_TOKENS"),), "cut off at llm.max_output_tokens"),
    ((answer("x", finish="SAFETY"),), "finishReason SAFETY"),
    ((make_response(ENDPOINT, 200, json.dumps({"promptFeedback": {"blockReason": "OTHER"}})),), "blocked the prompt (OTHER)"),
    ((make_response(ENDPOINT, 200, json.dumps({"candidates": []})),), "no candidate"),
    ((make_response(ENDPOINT, 200, "<html>"),), "not valid JSON"),
])
def test_api_problems_fall_back_to_template(cfg, issues_doc, answers, reason):
    rec, _ = run(cfg, issues_doc, *answers)
    assert rec["source"] == "template"
    assert reason in rec["reason"]
    assert rec["text"] == S.template_summary(issues_doc)


def test_5xx_is_retried_once_then_ok(cfg, issues_doc):
    rec, session = run(cfg, issues_doc, error(503, "The model is overloaded."), answer(good_text(issues_doc)))
    assert rec["source"] == "gemini" and len(session.requests) == 2


def test_429_is_not_retried(cfg, issues_doc):
    _, session = run(cfg, issues_doc, error(429, "quota"), answer("never used"))
    assert len(session.requests) == 1


def test_key_is_scrubbed_from_errors(cfg, issues_doc):
    rec, _ = run(cfg, issues_doc, error(400, f"API key not valid: {KEY}"))
    assert rec["source"] == "template" and KEY not in rec["reason"] and "***" in rec["reason"]


def test_disabled_and_missing_key_make_no_request(cfg, issues_doc):
    rec, session = run(cfg, issues_doc, env={})
    assert rec["source"] == "template" and rec["reason"] == "GEMINI_API_KEY is not set" and not session.requests
    rec, session = run(cfg, issues_doc, env={"GEMINI_API_KEY": "  "})
    assert rec["reason"] == "GEMINI_API_KEY is not set"
    cfg["llm"]["enabled"] = False
    rec, session = run(cfg, issues_doc)
    assert rec["reason"] == "llm.enabled is false" and not session.requests


# ---- validator --------------------------------------------------------------------------

def test_invented_number_is_rejected(cfg, issues_doc):
    rec, _ = run(cfg, issues_doc, answer(good_text(issues_doc) + "\nTraffic fell by 37%."))
    assert rec["source"] == "template"
    assert "number not in findings.json: 37" in rec["reason"]
    assert rec["validation_errors"] and "37%" in rec["rejected_text"]


def test_invented_url_is_rejected(cfg, issues_doc):
    rec, _ = run(cfg, issues_doc, answer(good_text(issues_doc) + f"\nSee {BASE}/made-up-page."))
    assert rec["source"] == "template"
    assert f"URL not in findings.json: {BASE}/made-up-page" in rec["reason"]


def test_unknown_finding_id_is_rejected(cfg, issues_doc):
    rec, _ = run(cfg, issues_doc, answer(good_text(issues_doc) + "\nAlso F999."))
    assert "finding id does not exist: F999" in rec["reason"]


def test_validator_accepts_data_values():
    src = json.dumps({"url": "https://example.test/post-a", "evidence": "1043 -> 1,200", "date": "2026-11-08",
                      "cls": 0.05, "count": 50})
    assert S.validate_summary("Post https://example.test/post-a/ (trailing slash), 1,043 links, CLS 0.05, "
                              "50 pages on 2026-11-08.", src, set()) == []
    assert S.validate_summary("CLS 0.06", src, set()) == ["number not in findings.json: 0.06"]
    assert S.validate_summary("   ", src, set()) == ["empty summary"]
    assert S.validate_summary("x" * 2000, src, set())[0].startswith("summary longer than")


def test_instruction_numbers_do_not_count_as_data(cfg, issues_doc):
    # "100 words" / "5 short lines" are in the prompt instructions, not in the data
    assert "number not in findings.json: 100" in S.validate_summary("Under 100 words.", S.prompt_data(issues_doc), set())


# ---- template ---------------------------------------------------------------------------

def test_template_lists_headline_and_top_findings(issues_doc):
    text = S.template_summary(issues_doc)
    lines = text.splitlines()
    assert lines[0] == issues_doc["headline"] + "."
    top = issues_doc["findings"][:S.TEMPLATE_TOP]
    for f in top:
        assert f"- {f['id']} {f['severity']}:" in text
    if len(issues_doc["findings"]) > S.TEMPLATE_TOP:
        assert f"... and {len(issues_doc['findings']) - S.TEMPLATE_TOP} more in the report." in text
    # the template passes its own validator
    assert S.validate_summary(text, json.dumps(issues_doc), {f["id"] for f in issues_doc["findings"]}) == []


def test_template_names_failed_source(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    doc = findings_for(cfg, root, "2026-11-02", day_records(psi=psi(runs=[], status="failed", error="HTTP 403: blocked")))
    text = S.template_summary(doc)
    assert "PSI failed: HTTP 403: blocked." in text


def test_template_stable_and_empty(cfg, root):
    write(cfg, root, "2026-11-01", day_records())
    doc = findings_for(cfg, root, "2026-11-02", day_records())
    assert S.template_summary(doc).splitlines() == [doc["headline"] + ".", "Nothing changed."]


# ---- record + report ---------------------------------------------------------------------

def test_summary_json_and_report_section(cfg, root, issues_doc):
    rec, _ = run(cfg, issues_doc, answer(good_text(issues_doc)))
    S.write_summary(root, "2026-11-02", rec)
    loaded = S.load_summary(root, "2026-11-02", issues_doc)
    assert loaded == rec
    inputs = RP.load_inputs(root, "2026-11-02")
    md = RP.build_report(inputs["findings"], inputs["meta"], inputs["records"], loaded)
    assert md.index("## Summary") < md.index("## Source health")
    assert "_Written by Gemini (gemini-2.5-flash) from findings.json only" in md
    assert RP.esc(good_text(issues_doc).splitlines()[0]) in md
    assert "The Summary section is written by Gemini" in md and "No AI-written text" not in md


def test_template_section_says_why(cfg, root, issues_doc):
    rec, _ = run(cfg, issues_doc, error(429, "quota"))
    md = RP.build_report(issues_doc, None, {}, rec)
    assert "_Template summary (no AI). Gemini not used: Gemini rate limit (HTTP 429): quota._" in md
    assert "No AI-written text in this report." in md


def test_stale_or_missing_summary_uses_template(cfg, root, issues_doc):
    assert S.load_summary(root, "2026-11-02", issues_doc)["reason"] == "no summary.json for this day"
    rec, _ = run(cfg, issues_doc, answer(good_text(issues_doc)))
    rec["findings_sha256"] = "0" * 64
    S.write_summary(root, "2026-11-02", rec)
    stale = S.load_summary(root, "2026-11-02", issues_doc)
    assert stale["source"] == "template" and stale["reason"] == "summary.json is from an older findings.json"


def test_summary_json_is_not_read_as_a_source(cfg, root, issues_doc):
    rec, _ = run(cfg, issues_doc, env={})
    S.write_summary(root, "2026-11-02", rec)
    again = D.build_findings(cfg, root, "2026-11-02")
    assert [f["id"] for f in again["findings"]] == [f["id"] for f in issues_doc["findings"]]


def test_thinking_level_for_gemini_3(cfg, issues_doc):
    cfg["llm"].update(model="gemini-3.8-flash", thinking_budget=None, thinking_level="low")
    session = GeminiSession(answer(good_text(issues_doc)), url=ENDPOINT.replace("gemini-2.5-flash", "gemini-3.8-flash"))
    rec = S.summarize(cfg, issues_doc, env={"GEMINI_API_KEY": KEY}, session=session, sleep=lambda s: None)
    assert rec["source"] == "gemini" and rec["model"] == "gemini-3.8-flash"
    assert session.requests[0]["json"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    cfg["llm"]["thinking_level"] = None
    assert "thinkingConfig" not in S.build_request(cfg, issues_doc)["generationConfig"]


def test_404_model_gone_falls_back(cfg, issues_doc):
    rec, _ = run(cfg, issues_doc, error(404, "This model models/gemini-2.5-flash is no longer available to new users."))
    assert rec["source"] == "template" and rec["reason"].startswith("Gemini HTTP 404: This model")
    md = RP.build_report(issues_doc, None, {}, rec)
    assert "available to new users._" in md and "users.._" not in md


def test_template_does_not_repeat_the_count():
    doc = {"headline": "h", "status": "baseline", "sources": {},
           "findings": [{"id": "F001", "severity": "info", "rule": "title_length", "check": "title length",
                         "message": "title length out of range: 39 URLs (baseline)", "count": 39, "evidence": "now: 15"}]}
    assert "- F001 info: title length out of range: 39 URLs (baseline) [now: 15]" in S.template_summary(doc)


def test_timeout_is_retried_once_then_ok(cfg, issues_doc):
    rec, session = run(cfg, issues_doc, requests.Timeout("slow"), answer(good_text(issues_doc)))
    assert rec["source"] == "gemini" and len(session.requests) == 2


def test_deadline_limits_the_call(cfg, issues_doc):
    from conftest import FakeClock
    clock = FakeClock()
    clock.now = 100.0
    # 10 s left: below MIN_CALL_SECONDS, no request at all
    session = GeminiSession(answer("never"))
    rec = S.summarize(cfg, issues_doc, env={"GEMINI_API_KEY": KEY}, session=session, sleep=clock.sleep,
                      deadline=110.0, clock=clock)
    assert rec["source"] == "template" and not session.requests
    assert rec["reason"] == "no time left in runtime.budget_seconds for a Gemini call"
    # 20 s left: the read timeout is cut to 20 s; after a timeout there is no time for a retry
    session = GeminiSession(requests.Timeout("slow"))

    def slow(method, url, **kw):
        session.requests.append(kw)
        clock.now += kw["timeout"][1]
        raise requests.Timeout("slow")
    session.request = slow
    rec = S.summarize(cfg, issues_doc, env={"GEMINI_API_KEY": KEY}, session=session, sleep=clock.sleep,
                      deadline=120.0, clock=clock)
    assert session.requests[0]["timeout"][1] == 20.0 and len(session.requests) == 1
    assert rec["reason"] == ("Gemini timed out after 20 s; no time left in runtime.budget_seconds "
                             "for another Gemini call")
