"""Short prioritized summary of findings.json (LLM with a deterministic fallback).

The LLM only rewords what the diff engine already decided. It gets a compact
copy of findings.json (never raw HTML or snapshot data) and must:
  - summarize and prioritize those findings only, regressions first,
  - name findings by their id (F001, ...),
  - use no URL and no number that is not in the input,
  - give no next steps of its own (suggested_next_step comes from the rules).

Provider: Google Gemini REST API via requests (no SDK).
  POST llm.endpoint with {model} = llm.model, header X-goog-api-key from the
  env var named in llm.api_key_env, generationConfig.maxOutputTokens =
  llm.max_output_tokens (thought tokens count against it), thinkingConfig
  with thinkingBudget = llm.thinking_budget (Gemini 2.5) or thinkingLevel =
  llm.thinking_level (Gemini 3+; they cannot turn thinking off), whichever is
  set (neither = no thinkingConfig, model default),
  timeout llm.timeout_seconds. One retry on HTTP 5xx or a timeout (the
  model is often "overloaded" for a moment); no retry on 429 (free-tier
  quota, waiting does not help inside a 5-minute run). With a deadline (the
  end of llm.budget_seconds, from main.py) a call only starts when at
  least MIN_CALL_SECONDS are left, and its read timeout is cut to the time
  that is left.
  The key is never logged or stored, and every error text is scrubbed of it.

Validator (every Gemini answer, before it is used):
  - not empty, not cut off at max_output_tokens, not blocked,
  - every URL in the text appears in the input sent to the model (a trailing
    slash difference is allowed, same rule as the diff engine),
  - every number in the text appears in that input,
  - every finding id in the text exists.
Any failure -> the template summary, and the reason is recorded.

Template summary (deterministic): the headline, failed sources, then the top
findings in findings.json order (that order is already critical, warning,
info, regressions first). Used when llm.enabled is false, there is no key,
on HTTP 429/5xx/other errors, timeouts, empty or blocked answers and
validation failures.

Result record (data/snapshots/DAY/summary.json):
  {schema_version, date, findings_generated_at, findings_sha256 (to spot a
   summary.json left over from an older findings.json), generated_at, source
   ("gemini" | "template"), reason (why the template was used, else null),
   model, text, finding_ids, validation_errors, usage}
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import requests

from .snapshot import _dump, _write_atomic, read_source

SCHEMA_VERSION = 1
RETRY_STATUSES = frozenset({500, 502, 503, 504})
RETRY_PAUSE_SECONDS = 2.0
CONNECT_TIMEOUT = 10.0
MIN_CALL_SECONDS = 15.0
MAX_ERROR_LEN = 300
MAX_SUMMARY_CHARS = 1500
TEMPLATE_TOP = 3
PROMPT_MAX_FINDINGS = 40   # findings.json order, so the most important ones are always in
PROMPT_MAX_ITEMS = 3       # example URLs per grouped finding

_ID_RE = re.compile(r"\bF\d{3,}\b")
_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"'`]+")
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_TRAILING = ".,;:!?*_"

INSTRUCTIONS = """You write the opening summary of a daily SEO review for a small web team.
The input is JSON produced by deterministic checks (findings.json). Rules:
1. Summarize and prioritize ONLY the findings in the input. Do not add findings, causes, guesses or outside knowledge.
2. Regressions and critical/warning findings first; then failed sources; then info findings briefly.
3. Refer to findings by their id, e.g. "F002".
4. Use only URLs and numbers that appear in the input, written exactly as in the input. Do not calculate new numbers (no sums, percentages or differences).
5. Do not suggest next steps or fixes; the report lists them separately.
6. If status is "baseline", say this is the first run and these are current issues, not changes.
7. If a source failed, say so explicitly.
8. Plain text, no markdown, no headings. At most 5 short lines, under 100 words."""


class SummaryError(Exception):
    """Gemini could not be used; the message is the fallback reason."""


# ---- input for the model -------------------------------------------------------------

def compact_findings(doc: dict[str, Any]) -> dict[str, Any]:
    """The part of findings.json the model sees: no long URL lists, no details."""
    findings = []
    for f in doc.get("findings", [])[:PROMPT_MAX_FINDINGS]:
        item = {k: f.get(k) for k in ("id", "severity", "source", "check", "change", "regression",
                                       "url", "evidence", "message", "count") if f.get(k) not in (None, "")}
        if item.get("message") == (f"{f.get('check')}: {f.get('url')}" if f.get("url") else f.get("check")):
            del item["message"]  # says nothing beyond check + url
        if f.get("items"):
            item["example_urls"] = [it.get("url") for it in f["items"][:PROMPT_MAX_ITEMS] if it.get("url")]
        findings.append(item)
    left = len(doc.get("findings", [])) - len(findings)
    out = {
        "site": (doc.get("site") or {}).get("name"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "headline": doc.get("headline"),
        "counts": doc.get("counts"),
        "sources": {s: {k: v for k, v in (i or {}).items() if k in ("status", "error", "compared_to") and v}
                    for s, i in (doc.get("sources") or {}).items()},
        "findings": findings,
        "ongoing": [{"check": o.get("check"), "severity": o.get("severity"), "count": o.get("count")}
                    for o in doc.get("ongoing") or []],
    }
    if left > 0:
        out["findings_not_shown"] = left
    return out


def prompt_data(doc: dict[str, Any]) -> str:
    """The JSON the model sees; also the reference text for the validator."""
    return json.dumps(compact_findings(doc), ensure_ascii=False, separators=(",", ":"))


def build_prompt(doc: dict[str, Any]) -> str:
    return f"{INSTRUCTIONS}\n\nInput (findings.json):\n{prompt_data(doc)}"


def build_request(cfg: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    llm = cfg["llm"]
    gen: dict[str, Any] = {"maxOutputTokens": llm["max_output_tokens"], "temperature": 0.2}
    if llm.get("thinking_budget") is not None:
        gen["thinkingConfig"] = {"thinkingBudget": llm["thinking_budget"]}
    elif llm.get("thinking_level") is not None:
        gen["thinkingConfig"] = {"thinkingLevel": llm["thinking_level"]}
    return {"contents": [{"role": "user", "parts": [{"text": build_prompt(doc)}]}], "generationConfig": gen}


# ---- validator ----------------------------------------------------------------------------

def _num_value(token: str) -> float | None:
    t = token.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def _numbers(text: str) -> set[float]:
    vals = set()
    for tok in _NUM_RE.findall(text):
        tok = tok.rstrip(",")
        v = _num_value(tok)
        if v is not None:
            vals.add(v)
        if "," in tok:  # "3,4" may be a list, not a thousands separator
            vals.update(x for x in (_num_value(p) for p in tok.split(",") if p) if x is not None)
    return vals


def _clean_url(u: str) -> str:
    return u.rstrip(_TRAILING)


def _url_variants(u: str) -> set[str]:
    return {u, u.rstrip("/"), u.rstrip("/") + "/"}


def allowed_values(source_text: str) -> tuple[set[str], set[float]]:
    """URLs and numbers present in the input given to the model."""
    urls = {_clean_url(u) for u in _URL_RE.findall(source_text)}
    allowed_urls: set[str] = set()
    for u in urls:
        allowed_urls |= _url_variants(u)
    rest = _ID_RE.sub(" ", _URL_RE.sub(" ", source_text))
    nums = _numbers(rest)
    # Numbers inside URLs and ids count too (e.g. a year in a post URL).
    for u in urls:
        nums |= _numbers(u)
    return allowed_urls, nums


def validate_summary(text: str, source_text: str, finding_ids: set[str]) -> list[str]:
    """Problems with an LLM summary; empty list = usable."""
    problems: list[str] = []
    if not text.strip():
        return ["empty summary"]
    if len(text) > MAX_SUMMARY_CHARS:
        problems.append(f"summary longer than {MAX_SUMMARY_CHARS} characters")
    allowed_urls, allowed_nums = allowed_values(source_text)
    for u in (_clean_url(u) for u in _URL_RE.findall(text)):
        if u not in allowed_urls:
            problems.append(f"URL not in findings.json: {u}")
    for fid in sorted(set(_ID_RE.findall(text))):
        if fid not in finding_ids:
            problems.append(f"finding id does not exist: {fid}")
    rest = _ID_RE.sub(" ", _URL_RE.sub(" ", text))
    for tok in _NUM_RE.findall(rest):
        tok = tok.rstrip(",")
        v = _num_value(tok)
        if v is None:
            continue
        if v in allowed_nums:
            continue
        if "," in tok and all((_num_value(p) in allowed_nums) for p in tok.split(",") if p):
            continue
        problems.append(f"number not in findings.json: {tok}")
    return problems


# ---- template ------------------------------------------------------------------------------

def template_summary(doc: dict[str, Any], top: int = TEMPLATE_TOP) -> str:
    lines = [doc["headline"] + "."]
    failed = [(s, i) for s, i in (doc.get("sources") or {}).items()
              if i.get("status") not in ("ok", "partial", "disabled")]
    for s, i in failed:
        lines.append(f"{s.upper()} failed: {i.get('error') or i.get('status')}.")
    findings = [f for f in doc.get("findings", []) if f.get("rule") not in ("source_failed", "source_failed_critical")]
    if not findings:
        lines.append("No findings today." if doc.get("status") != "stable" else "Nothing changed.")
    else:
        label = "Top findings" if len(findings) > top else "Findings"
        lines.append(f"{label}:")
        for f in findings[:top]:
            msg = f.get("message") or (f"{f['check']}: {f['url']}" if f.get("url") else f["check"])
            n = f" ({f['count']} URLs)" if (f.get("count") or 1) > 1 and f"{f['count']} URL" not in msg else ""
            ev = f" [{f['evidence']}]" if f.get("evidence") and f["evidence"] not in msg else ""
            lines.append(f"- {f['id']} {f['severity']}: {msg}{n}{ev}")
        if len(findings) > top:
            lines.append(f"- ... and {len(findings) - top} more in the report.")
    return "\n".join(lines)


# ---- Gemini call ------------------------------------------------------------------------------

def _scrub(text: str, key: str | None) -> str:
    if key:
        text = text.replace(key, "***")
    return text[:MAX_ERROR_LEN]


def _api_message(resp: requests.Response) -> str:
    try:
        err = resp.json().get("error") or {}
        msg = err.get("message") or ""
    except (ValueError, AttributeError):
        msg = ""
    return " ".join(str(msg).split()) or (resp.text or "").strip()[:150]


def parse_answer(data: Any) -> tuple[str, dict[str, Any]]:
    """(text, usage) from a generateContent response. Raises SummaryError."""
    if not isinstance(data, dict):
        raise SummaryError("Gemini response is not a JSON object")
    usage_in = data.get("usageMetadata") or {}
    usage = {k: usage_in.get(src) for k, src in (("prompt_tokens", "promptTokenCount"),
                                                  ("output_tokens", "candidatesTokenCount"),
                                                  ("thought_tokens", "thoughtsTokenCount"),
                                                  ("total_tokens", "totalTokenCount"))}
    block = (data.get("promptFeedback") or {}).get("blockReason")
    if block:
        raise SummaryError(f"Gemini blocked the prompt ({block})")
    cands = data.get("candidates") or []
    if not cands:
        raise SummaryError("Gemini returned no candidate")
    cand = cands[0] or {}
    finish = cand.get("finishReason")
    parts = ((cand.get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
    if finish == "MAX_TOKENS":
        raise SummaryError("Gemini answer was cut off at llm.max_output_tokens")
    if finish not in (None, "STOP"):
        raise SummaryError(f"Gemini stopped with finishReason {finish}")
    if not text.strip():
        raise SummaryError("Gemini returned an empty answer")
    return text.strip(), usage


def call_gemini(cfg: dict[str, Any], doc: dict[str, Any], key: str, session: Any = None,
                sleep: Callable[[float], None] = time.sleep, deadline: float | None = None,
                clock: Callable[[], float] = time.monotonic) -> tuple[str, dict[str, Any]]:
    llm = cfg["llm"]
    url = llm["endpoint"].replace("{model}", llm["model"])
    session = session or requests.Session()
    headers = {"X-goog-api-key": key, "Content-Type": "application/json",
               "User-Agent": cfg["crawler"]["user_agent"]}
    body = build_request(cfg, doc)
    attempts = 2
    last = ""
    for attempt in range(attempts):
        read_timeout = float(llm["timeout_seconds"])
        if deadline is not None:
            left = deadline - clock()
            if left < MIN_CALL_SECONDS:
                why = f"{last}; " if last else ""
                raise SummaryError(f"{why}no time left in llm.budget_seconds for "
                                   f"{'another' if attempt else 'a'} Gemini call")
            read_timeout = min(read_timeout, left)
        try:
            resp = session.request("POST", url, headers=headers, json=body,
                                   timeout=(CONNECT_TIMEOUT, read_timeout))
        except requests.Timeout:
            last = f"Gemini timed out after {round(read_timeout)} s"
            if attempt + 1 < attempts:
                continue
            raise SummaryError(last + (" (tried twice)" if attempt else "")) from None
        except requests.RequestException as e:
            raise SummaryError(_scrub(f"Gemini request failed: {type(e).__name__}: {e}", key)) from None
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                raise SummaryError("Gemini response is not valid JSON") from None
            return parse_answer(data)
        msg = _scrub(_api_message(resp), key)
        if resp.status_code in RETRY_STATUSES and attempt + 1 < attempts:
            last = f"Gemini HTTP {resp.status_code}: {msg}"
            sleep(RETRY_PAUSE_SECONDS)
            continue
        if resp.status_code == 429:
            raise SummaryError(_scrub(f"Gemini rate limit (HTTP 429): {msg}", key))
        raise SummaryError(_scrub(f"Gemini HTTP {resp.status_code}: {msg}", key))
    raise AssertionError("unreachable")


# ---- entry point -------------------------------------------------------------------------------

def summarize(cfg: dict[str, Any], doc: dict[str, Any], env: Mapping[str, str] | None = None,
              session: Any = None, sleep: Callable[[float], None] = time.sleep,
              now: datetime | None = None, deadline: float | None = None,
              clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Summary record for one findings document. Never raises for API problems."""
    env = os.environ if env is None else env
    llm = cfg["llm"]
    ids = {f["id"] for f in doc.get("findings", [])}
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "date": doc.get("date"),
        "findings_generated_at": doc.get("generated_at"),
        "findings_sha256": findings_hash(doc),
        "generated_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds"),
        "source": "template",
        "reason": None,
        "model": llm["model"],
        "text": "",
        "finding_ids": [],
        "validation_errors": [],
        "usage": None,
    }
    key = (env.get(llm["api_key_env"]) or "").strip()
    reason = None
    if not llm["enabled"]:
        reason = "llm.enabled is false"
    elif not key:
        reason = f"{llm['api_key_env']} is not set"
    else:
        try:
            text, usage = call_gemini(cfg, doc, key, session=session, sleep=sleep,
                                      deadline=deadline, clock=clock)
            record["usage"] = usage
            problems = validate_summary(text, prompt_data(doc), ids)
            if problems:
                record["validation_errors"] = problems[:10]
                record["rejected_text"] = _scrub(text, key)[:MAX_SUMMARY_CHARS]
                reason = "Gemini summary failed validation: " + "; ".join(problems[:3])
            else:
                record.update(source="gemini", text=text,
                              finding_ids=sorted(set(_ID_RE.findall(text)) & ids))
                return record
        except SummaryError as e:
            reason = _scrub(str(e), key)
    record["reason"] = reason
    record["text"] = template_summary(doc)
    record["finding_ids"] = sorted(set(_ID_RE.findall(record["text"])) & ids)
    return record


def findings_hash(doc: dict[str, Any]) -> str:
    return hashlib.sha256(_dump(doc).encode("utf-8")).hexdigest()


def write_summary(root: str | Path, day: str, record: dict[str, Any]) -> Path:
    path = Path(root) / day / "summary.json"
    _write_atomic(path, _dump(record))
    return path


def load_summary(root: str | Path, day: str, doc: dict[str, Any]) -> dict[str, Any]:
    """summary.json for the day if it matches this findings.json, else a template record."""
    rec = read_source(root, day, "summary")
    if rec and rec.get("findings_sha256") == findings_hash(doc) and rec.get("text"):
        return rec
    reason = ("summary.json is from an older findings.json" if rec else "no summary.json for this day")
    return {"schema_version": SCHEMA_VERSION, "date": doc.get("date"),
            "findings_generated_at": doc.get("generated_at"), "findings_sha256": findings_hash(doc),
            "generated_at": None,
            "source": "template", "reason": reason, "model": None, "text": template_summary(doc),
            "finding_ids": [], "validation_errors": [], "usage": None}
