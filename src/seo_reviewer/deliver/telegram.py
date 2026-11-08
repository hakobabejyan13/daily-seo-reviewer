"""Telegram delivery: one short HTML message per run.

The message is built only from the day's findings.json, summary.json and
snapshot records (no LLM call here, no new numbers):

  header     site, date, status (Critical / Warnings / All stable / ...)
  counts     "N critical, N warning, N info", compared-with day
  summary    "AI summary" + the validated Gemini text, or the one word
             "template" (its text repeats the findings list below it)
  findings   up to delivery.telegram.max_findings_in_message, in findings.json
             order (severity, then regressions first): severity, URL,
             old -> new, next step (written by the rules)
  alerts     one explicit line per failed / not-run source
  sources    one line built from report.source_health
  link       delivery.report_base_url/YYYY-MM-DD.md

All stable = 2 lines (header, then sources + link), plus alert lines if a
source failed.

Sending: Telegram Bot API sendMessage via requests, parse_mode HTML (every
piece of text is HTML-escaped), link previews off. The bot token is part of
the URL, so it is never logged and is scrubbed (with the chat id) from every
error text. HTTP 5xx, timeouts and connection errors are retried once; a 429
is retried once only when retry_after <= MAX_RETRY_AFTER seconds; a 400 about
the HTML ("can't parse entities") is retried once as plain text. Nothing
here raises for Telegram problems: the result is a delivery record that
main.py writes to data/snapshots/DAY/delivery.json.
"""

from __future__ import annotations

import html
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import requests

from .. import report as RP
from ..snapshot import _dump, _write_atomic

SCHEMA_VERSION = 1
API_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_CHARS = 4096            # Telegram's limit for one message (UTF-16 code units)
RETRY_STATUSES = frozenset({500, 502, 503, 504})
RETRY_PAUSE_SECONDS = 2.0
MAX_RETRY_AFTER = 30        # seconds; a longer 429 wait = failed delivery, not a stuck run
CONNECT_TIMEOUT = 10.0
MAX_ERROR_LEN = 300
MAX_TEXT_LINE = 300         # cap for one message / next-step line inside a finding

SEVERITY_MARK = {"critical": "\U0001F534", "warning": "\U0001F7E0", "info": "\U0001F535"}  # red / orange / blue
STABLE_MARK = "✅"
ALERT_MARK = "⚠️"
# Failed sources get their own alert line, so their findings are not repeated in the list.
ALERT_RULES = frozenset({"source_failed", "source_failed_critical", "source_skipped_upstream"})


class DeliveryError(Exception):
    """sendMessage did not succeed (message already scrubbed)."""

    def __init__(self, message: str, http_status: int | None = None, attempts: int = 1):
        super().__init__(message)
        self.http_status = http_status
        self.attempts = attempts


# ---- message -----------------------------------------------------------------------------------

def h(text: Any, quote: bool = False) -> str:
    """Plain text -> Telegram HTML (escapes &, < and >; quotes too inside attributes)."""
    return html.escape(str(text), quote=quote)


def tg_len(text: str) -> int:
    """Length as Telegram counts it (UTF-16 code units)."""
    return len(text.encode("utf-16-le")) // 2


def _one_line(text: Any, limit: int = MAX_TEXT_LINE) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[: limit - 3].rstrip() + "..."


def status_label(doc: dict[str, Any]) -> tuple[str, str]:
    """(mark, label) for the header."""
    c = doc["counts"]
    base = " (baseline run)" if doc["status"] == "baseline" else ""
    if c["critical"]:
        return SEVERITY_MARK["critical"], "Critical" + base
    if c["warning"]:
        return SEVERITY_MARK["warning"], "Warnings" + base
    if doc["status"] == "stable":
        return STABLE_MARK, "All stable"
    if doc["status"] == "baseline":
        return SEVERITY_MARK["info"], "Baseline run"
    return SEVERITY_MARK["info"], "Changes (info only)"


def report_url(cfg: dict[str, Any], day: str) -> str:
    return f"{cfg['delivery']['report_base_url'].rstrip('/')}/{day}.md"


def _compared(doc: dict[str, Any]) -> str:
    if doc.get("compare_to"):
        return f"compared with {doc['compare_to']}"
    days = sorted({i["compared_to"] for i in doc["sources"].values() if i.get("compared_to")})
    if not days:
        return "first run, nothing to compare with"
    return f"compared with {days[-1]}" if len(days) == 1 else f"compared with {', '.join(days)} (per source)"


def _first_url(f: dict[str, Any]) -> str | None:
    if f.get("url"):
        return f["url"]
    if f.get("items"):
        return f["items"][0].get("url")
    urls = (f.get("details") or {}).get("urls") or []
    return urls[0] if urls else None


def finding_lines(f: dict[str, Any]) -> list[str]:
    change = RP.CHANGE_LABEL.get(f["change"], f["change"])
    tag = ", regression" if f.get("regression") and f["change"] != "baseline" else ""
    lines = [f"{SEVERITY_MARK.get(f['severity'], '')} <b>{h(f['severity'])}</b> · {h(f['check'])} ({h(change)}{h(tag)})"]
    if f.get("message") and f["message"] != RP._default_message(f):
        lines.append(h(_one_line(f["message"])))
    url = _first_url(f)
    count = f.get("count") or 1
    if url:
        lines.append(h(url) if f.get("url") or count <= 1 else f"{count} URLs, e.g. {h(url)}")
    det = f.get("details") or {}
    if det.get("linked_from"):
        n = det.get("linked_from_count") or len(det["linked_from"])
        lines.append(f"Linked from {n} page(s), e.g. {h(det['linked_from'][0])}")
    if f.get("evidence"):
        lines.append(f"<code>{h(_one_line(f['evidence']))}</code>")
    if f.get("suggested_next_step"):
        lines.append(f"Next: {h(_one_line(f['suggested_next_step']))}")
    return lines


def sources_line(rows: list[dict[str, Any]]) -> str:
    parts = []
    for r in rows:
        show = r["coverage"] and r["status"] != "disabled" and r["source"] != "robots"  # robots mode is in the report
        cov = f" ({r['coverage']})" if show else ""
        parts.append(f"{r['label']} {r['status']}{cov}")
    return "Sources: " + h(" · ".join(parts))


def _assemble(doc: dict[str, Any], cfg: dict[str, Any], day: str, rows: list[dict[str, Any]],
              summary: dict[str, Any] | None, findings: list[dict[str, Any]], total: int,
              summary_text: str | None) -> str:
    mark, label = status_label(doc)
    site = doc["site"]["name"]
    link = f'<a href="{h(report_url(cfg, day), quote=True)}">Full report</a>'
    head = f"{mark} <b>{h(site)} · {h(day)} · {h(label)}</b>"
    alerts = [f"{ALERT_MARK} {h(r['alert'])}" for r in rows if r["alert"]]
    if doc["status"] == "stable":
        c = doc["counts"]
        info = f" ({c['info']} info)" if c["info"] else ""
        return "\n".join([head + h(f", {_compared(doc)}{info}"), *alerts, f"{sources_line(rows)} · {link}"])
    c = doc["counts"]
    out = [head, h(f"{c['critical']} critical, {c['warning']} warning, {c['info']} info · {_compared(doc)}"), ""]
    if summary and summary.get("source") == "gemini" and summary_text:
        out += ["<b>AI summary</b>", h(summary_text), ""]
    else:
        out += ["Summary: template", ""]
    if alerts:
        out += [*alerts, ""]
    if findings:
        more = f" (top {len(findings)} of {total})" if total > len(findings) else ""
        out.append(f"<b>Findings</b>{more}")
        for f in findings:
            out += finding_lines(f) + [""]
    elif total:
        out += [f"{total} finding(s): see the full report.", ""]
    out += [sources_line(rows), link]
    return "\n".join(out)


def build_message(cfg: dict[str, Any], doc: dict[str, Any], summary: dict[str, Any] | None = None,
                  records: dict[str, Any] | None = None, limit: int = MAX_CHARS) -> dict[str, Any]:
    """{"text": html, "findings": [ids in the message], "total": n} for one findings document."""
    day = doc["date"]
    rows = RP.source_health(doc, records or {})
    listed = [f for f in doc["findings"] if f["rule"] not in ALERT_RULES]
    n = min(len(listed), cfg["delivery"]["telegram"]["max_findings_in_message"])
    summary_text = (summary or {}).get("text") or None
    while True:
        text = _assemble(doc, cfg, day, rows, summary, listed[:n], len(listed), summary_text)
        if tg_len(text) <= limit:
            return {"text": text, "findings": [f["id"] for f in listed[:n]], "total": len(listed)}
        if n > 0:
            n -= 1  # drop findings from the end first (least important)
        elif summary_text and len(summary_text) > 20:
            summary_text = summary_text[: max(10, len(summary_text) - (tg_len(text) - limit) - 10)].rstrip() + "..."
        else:
            # Only alerts and the source line are left: hard cut on a line boundary.
            lines, kept = text.split("\n"), []
            for line in lines:
                if tg_len("\n".join(kept + [line])) > limit - 4:
                    break
                kept.append(line)
            return {"text": "\n".join(kept + ["..."]), "findings": [], "total": len(listed)}


def plain_text(html_text: str) -> str:
    """Telegram HTML -> plain text (fallback when Telegram rejects the HTML)."""
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


# ---- sending -----------------------------------------------------------------------------------

def _scrub(text: str, *secrets: str | None) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text[:MAX_ERROR_LEN]


def _api_error(resp: requests.Response) -> tuple[str, dict[str, Any]]:
    try:
        data = resp.json()
    except ValueError:
        return " ".join(resp.text.split())[:MAX_ERROR_LEN] or resp.reason or "", {}
    if not isinstance(data, dict):
        return "", {}
    return str(data.get("description") or ""), data.get("parameters") or {}


def send_message(cfg: dict[str, Any], text: str, token: str, chat_id: str, session: Any = None,
                 sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """POST sendMessage. Returns {"message_id", "attempts", "http_status", "plain_text_fallback"};
    raises DeliveryError (scrubbed) on failure."""
    tg = cfg["delivery"]["telegram"]
    url = API_URL.format(token=token)
    session = session or requests.Session()
    body: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": tg["parse_mode"],
                            "link_preview_options": {"is_disabled": True}}
    timeout = (CONNECT_TIMEOUT, float(tg["timeout_seconds"]))
    plain = False
    last, last_status = "", None
    attempts = 2
    for attempt in range(attempts):
        n = attempt + 1
        can_retry = n < attempts
        try:
            resp = session.request("POST", url, json=body, timeout=timeout,
                                   headers={"User-Agent": cfg["crawler"]["user_agent"]})
        except requests.Timeout:
            last, last_status = f"Telegram timed out after {round(timeout[1])} s", None
            if can_retry:
                sleep(RETRY_PAUSE_SECONDS)
                continue
            raise DeliveryError(last + " (tried twice)", None, n) from None
        except requests.RequestException as e:
            last, last_status = _scrub(f"Telegram request failed: {type(e).__name__}: {e}", token, chat_id), None
            if can_retry:
                sleep(RETRY_PAUSE_SECONDS)
                continue
            raise DeliveryError(last + " (tried twice)", None, n) from None
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                raise DeliveryError("Telegram answered HTTP 200 with invalid JSON", 200, n) from None
            if not isinstance(data, dict) or not data.get("ok"):
                raise DeliveryError(_scrub(f"Telegram answered ok=false: {(data or {}).get('description') if isinstance(data, dict) else data}",
                                           token, chat_id), 200, n)
            result = data.get("result") or {}
            return {"message_id": result.get("message_id") if isinstance(result, dict) else None,
                    "attempts": n, "http_status": 200, "plain_text_fallback": plain}
        desc, params = _api_error(resp)
        desc = _scrub(desc, token, chat_id)
        last, last_status = f"Telegram HTTP {resp.status_code}: {desc}".rstrip(": "), resp.status_code
        if resp.status_code == 429:
            wait = params.get("retry_after")
            wait = wait if isinstance(wait, (int, float)) and not isinstance(wait, bool) else None
            if can_retry and wait is not None and wait <= MAX_RETRY_AFTER:
                sleep(float(wait))
                continue
            why = (f"retry_after {wait} s is longer than {MAX_RETRY_AFTER} s" if wait is not None and wait > MAX_RETRY_AFTER
                   else "")
            raise DeliveryError(f"Telegram rate limit (HTTP 429): {desc}" + (f"; {why}" if why else "")
                                + (" (tried twice)" if attempt else ""), 429, n)
        if resp.status_code in RETRY_STATUSES and can_retry:
            sleep(RETRY_PAUSE_SECONDS)
            continue
        if resp.status_code == 400 and "parse entities" in desc.lower() and can_retry and not plain:
            body = {k: v for k, v in body.items() if k != "parse_mode"}
            body["text"] = plain_text(text)
            plain = True
            continue
        raise DeliveryError(last + (" (tried twice)" if attempt and resp.status_code in RETRY_STATUSES else ""),
                            resp.status_code, n)
    raise DeliveryError(last, last_status, attempts)  # pragma: no cover - loop always returns or raises


# ---- entry point -------------------------------------------------------------------------------

def deliver(cfg: dict[str, Any], doc: dict[str, Any], summary: dict[str, Any] | None = None,
            records: dict[str, Any] | None = None, send: bool = True,
            env: Mapping[str, str] | None = None, session: Any = None,
            sleep: Callable[[float], None] = time.sleep, now: datetime | None = None) -> dict[str, Any]:
    """Build the message and send it (unless send is False). Never raises for Telegram problems.

    Returns the delivery record (status sent / skipped / failed). The token and
    chat id are never part of it."""
    env = os.environ if env is None else env
    tg = cfg["delivery"]["telegram"]
    msg = build_message(cfg, doc, summary, records)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "date": doc.get("date"),
        "channel": "telegram",
        "generated_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds"),
        "status": "skipped",
        "reason": None,
        "http_status": None,
        "attempts": 0,
        "message_id": None,
        "plain_text_fallback": False,
        "chars": tg_len(msg["text"]),
        "finding_ids": msg["findings"],
        "findings_total": msg["total"],
        "summary_source": (summary or {}).get("source"),
        "report_url": report_url(cfg, doc["date"]),
        "text": msg["text"],
    }
    token = (env.get(tg["bot_token_env"]) or "").strip()
    chat_id = (env.get(tg["chat_id_env"]) or "").strip()
    if not send:
        record["reason"] = "not sent: --no-deliver (dry run)"
        return record
    if not tg["enabled"]:
        record["reason"] = "not sent: delivery.telegram.enabled is false"
        return record
    missing = [name for name, v in ((tg["bot_token_env"], token), (tg["chat_id_env"], chat_id)) if not v]
    if missing:
        record["reason"] = f"not sent: {' and '.join(missing)} not set"
        return record
    try:
        res = send_message(cfg, msg["text"], token, chat_id, session=session, sleep=sleep)
    except DeliveryError as e:
        record.update(status="failed", reason=_scrub(str(e), token, chat_id),
                      http_status=e.http_status, attempts=e.attempts)
        return record
    record.update(status="sent", http_status=res["http_status"], attempts=res["attempts"],
                  message_id=res["message_id"], plain_text_fallback=res["plain_text_fallback"],
                  reason="sent as plain text: Telegram rejected the HTML" if res["plain_text_fallback"] else None)
    return record


def write_delivery(root: str | Path, day: str, record: dict[str, Any]) -> Path:
    path = Path(root) / day / "delivery.json"
    _write_atomic(path, _dump(record))
    return path


def format_delivery(rec: dict[str, Any], path: Path | None = None) -> list[str]:
    where = f" (recorded in {path})" if path else ""
    if rec["status"] == "sent":
        head = (f"Telegram: sent{where}, message id {rec['message_id']}, {rec['chars']} chars, "
                f"{len(rec['finding_ids'])} of {rec['findings_total']} finding(s), attempts {rec['attempts']}"
                + (", as plain text (HTML rejected)" if rec.get("plain_text_fallback") else ""))
    elif rec["status"] == "failed":
        head = f"DELIVERY FAILED{where}: {rec['reason']}. The report is written; only the Telegram message is missing."
    else:
        head = f"Telegram: {rec['reason']}{where}"
    return [head, "  message:"] + [f"    {line}" for line in plain_text(rec["text"]).splitlines()]
