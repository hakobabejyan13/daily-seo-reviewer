"""PageSpeed Insights collector (PSI API v5, runPagespeed).

One run per URL x strategy (psi.urls, empty = homepage only; psi.strategies).
Each run records Lighthouse lab metrics and, when Google has them, CrUX field
data for the URL and for the whole origin.

Lab metrics (lighthouseResult):
  performance_score  0-100 (Lighthouse performance category score x 100)
  lcp_ms, tbt_ms     audits largest-contentful-paint / total-blocking-time
  cls                audit cumulative-layout-shift
Field data (loadingExperience = this URL, originLoadingExperience = origin):
  lcp_ms, inp_ms, cls with the 75th percentile and Google's category
  (FAST / AVERAGE / SLOW) plus overall_category. When PSI has no data for the
  URL it either leaves the metrics out or sends origin data with
  origin_fallback true; both are stored as "no field data for this URL"
  (field.url is null and field.notes says why), never as URL data.

Client:
  PSI is Google's API, not the target site, so it does not go through the
  PoliteClient: robots.txt and the 1 request/s site rate limit do not apply.
  It uses its own requests.Session (so it can run in a background thread next
  to the crawl), the crawler's User-Agent, psi.timeout_seconds per call and
  psi.max_retries retries on HTTP 429, 5xx, timeouts and connection errors.
  The API key comes from the environment variable named in psi.api_key_env
  and is sent as the documented `key` query parameter. It is never written to
  the record, and every error text is scrubbed of it.

Budget:
  deadline is a value on the collector's clock. A call (or retry) is only
  started when it can finish before the deadline even if it takes the full
  psi.timeout_seconds; otherwise that run and the rest are "skipped".

Run status:
  ok      -> Lighthouse returned a performance score
  failed  -> HTTP error, network error or timeout after retries, Lighthouse
             runtime error, or an unreadable response
  skipped -> runtime.budget_seconds would be exceeded
Record status:
  ok       -> every run ok
  partial  -> some runs ok, some failed or skipped (partial_reasons says why)
  failed   -> no run ok (also: no API key, every call failed)
  disabled -> psi.enabled is false; nothing was requested
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit

import requests

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
CONNECT_TIMEOUT = 10.0
MAX_ERROR_LEN = 300

LAB_AUDITS = {
    "lcp_ms": "largest-contentful-paint",
    "cls": "cumulative-layout-shift",
    "tbt_ms": "total-blocking-time",
}
FIELD_METRICS = {
    "lcp_ms": "LARGEST_CONTENTFUL_PAINT_MS",
    "inp_ms": "INTERACTION_TO_NEXT_PAINT",
    "cls": "CUMULATIVE_LAYOUT_SHIFT_SCORE",
}


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def select_urls(cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
    """psi.urls resolved against base_url (empty = homepage), same host only, deduped."""
    base = cfg["site"]["base_url"]
    host = _host(base)
    notes: list[str] = []
    wanted = cfg["psi"]["urls"] or [base + "/"]
    urls: list[str] = []
    for raw in wanted:
        url = urljoin(base + "/", raw.strip())
        if _host(url) != host:
            notes.append(f"psi.urls entry {raw!r} is not on {host}; ignored")
            continue
        if url not in urls:
            urls.append(url)
    return urls, notes


# ---- response parsing --------------------------------------------------------

def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_lab(lh: dict[str, Any]) -> dict[str, Any]:
    perf = (lh.get("categories") or {}).get("performance") or {}
    score = _num(perf.get("score"))
    audits = lh.get("audits") or {}
    lab: dict[str, Any] = {"performance_score": round(score * 100) if score is not None else None}
    for key, audit_id in LAB_AUDITS.items():
        val = _num((audits.get(audit_id) or {}).get("numericValue"))
        if val is None:
            lab[key] = None
        elif key == "cls":
            lab[key] = round(val, 4)
        else:
            lab[key] = round(val)
    return lab


def parse_field(exp: Any) -> dict[str, Any] | None:
    """CrUX block -> {overall_category, metrics}, or None when it has no metrics."""
    if not isinstance(exp, dict):
        return None
    metrics_in = exp.get("metrics") or {}
    metrics: dict[str, Any] = {}
    for key, name in FIELD_METRICS.items():
        m = metrics_in.get(name)
        if not isinstance(m, dict) or _num(m.get("percentile")) is None:
            metrics[key] = None
            continue
        p = _num(m["percentile"])
        # PSI reports the CLS percentile x 100 (e.g. 5 = 0.05).
        value = round(p / 100, 4) if key == "cls" else round(p)
        metrics[key] = {"percentile": value, "category": m.get("category")}
    if all(v is None for v in metrics.values()):
        return None
    return {"overall_category": exp.get("overall_category"), "metrics": metrics}


def parse_response(data: Any) -> dict[str, Any]:
    """Pieces of a 200 PSI response. Raises ValueError when it is not usable."""
    if not isinstance(data, dict) or not isinstance(data.get("lighthouseResult"), dict):
        raise ValueError("PSI response has no lighthouseResult")
    lh = data["lighthouseResult"]
    runtime_error = lh.get("runtimeError")
    out: dict[str, Any] = {
        "lighthouse_version": lh.get("lighthouseVersion"),
        "fetched_at": lh.get("fetchTime") or data.get("analysisUTCTimestamp"),
        "final_url": lh.get("finalDisplayedUrl") or lh.get("finalUrl"),
        "run_warnings": [str(w) for w in (lh.get("runWarnings") or [])],
        "lab": parse_lab(lh),
    }
    notes: list[str] = []
    url_exp = data.get("loadingExperience")
    url_field = parse_field(url_exp)
    if url_field is not None and isinstance(url_exp, dict) and url_exp.get("origin_fallback"):
        url_field = None
        notes.append("no field data (CrUX) for this URL; PSI fell back to origin data, stored under origin only")
    elif url_field is None:
        notes.append("no field data (CrUX) for this URL")
    origin_field = parse_field(data.get("originLoadingExperience"))
    if origin_field is None:
        notes.append("no field data (CrUX) for the origin")
    out["field"] = {"url": url_field, "origin": origin_field, "notes": notes}
    if isinstance(runtime_error, dict) and runtime_error.get("code") not in (None, "NO_ERROR"):
        out["runtime_error"] = f"{runtime_error.get('code')}: {runtime_error.get('message', '')}".strip()
    elif out["lab"]["performance_score"] is None:
        out["runtime_error"] = "Lighthouse returned no performance score"
    return out


# ---- client ------------------------------------------------------------------

class PsiClient:
    """Small client for the PSI API with timeouts, retries and key redaction."""

    def __init__(self, cfg: dict[str, Any], api_key: str, session: requests.Session | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 retry_backoff_seconds: float = 5.0):
        self.api_key = api_key
        self.timeout = float(cfg["psi"]["timeout_seconds"])
        self.max_retries = int(cfg["psi"]["max_retries"])
        self.retry_backoff = retry_backoff_seconds
        self.clock = clock
        self.sleep = sleep
        self.request_count = 0
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": cfg["crawler"]["user_agent"]})

    def redact(self, text: str) -> str:
        if self.api_key:
            text = text.replace(self.api_key, "***")
        return text[:MAX_ERROR_LEN]

    def fits(self, deadline: float | None, extra: float = 0.0) -> bool:
        return deadline is None or self.clock() + extra + self.timeout <= deadline

    def run(self, url: str, strategy: str, deadline: float | None) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "url": url, "strategy": strategy, "status": "skipped", "error": None, "http_status": None,
            "attempts": 0, "duration_s": None, "lighthouse_version": None, "fetched_at": None,
            "final_url": None, "run_warnings": [], "lab": None, "field": None,
        }
        if not self.fits(deadline):
            rec["error"] = "runtime.budget_seconds would be exceeded by this call"
            return rec
        params = {"url": url, "strategy": strategy, "category": "performance", "key": self.api_key}
        started = self.clock()
        error: str | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                backoff = self.retry_backoff * attempt
                if not self.fits(deadline, backoff):
                    error = f"{error}; no time left in runtime.budget_seconds to retry"
                    break
                self.sleep(backoff)
            rec["attempts"] += 1
            self.request_count += 1
            try:
                resp = self.session.request("GET", PSI_ENDPOINT, params=params,
                                            timeout=(CONNECT_TIMEOUT, self.timeout))
            except requests.Timeout:
                error = f"timeout after {self.timeout:g} s"
                continue
            except requests.RequestException as e:
                error = self.redact(f"network error: {type(e).__name__}: {e}")
                continue
            rec["http_status"] = resp.status_code
            if resp.status_code != 200:
                error = self.redact(f"HTTP {resp.status_code}: {_api_error(resp)}")
                if resp.status_code in RETRY_STATUSES:
                    continue
                break
            try:
                parsed = parse_response(resp.json())
            except ValueError as e:  # includes JSON decode errors
                error = self.redact(f"unreadable PSI response: {e}")
                break
            rec.update({k: parsed[k] for k in ("lighthouse_version", "fetched_at", "final_url",
                                                "run_warnings", "lab", "field")})
            if "runtime_error" in parsed:
                error = self.redact(f"Lighthouse error: {parsed['runtime_error']}")
                rec["lab"] = None  # a failed Lighthouse run has no trustworthy lab numbers
                break
            rec["status"] = "ok"
            error = None
            break
        rec["duration_s"] = round(self.clock() - started, 1)
        if rec["status"] != "ok":
            rec["status"] = "failed"
            rec["error"] = error
        return rec


def _api_error(resp: Any) -> str:
    """Google's error.message from a JSON error body, else the reason phrase."""
    try:
        body = resp.json()
        msg = (body.get("error") or {}).get("message")
        if msg:
            return " ".join(str(msg).split())
    except (ValueError, AttributeError):
        pass
    return getattr(resp, "reason", None) or "no error message"


# ---- collector ---------------------------------------------------------------

def _record(cfg: dict[str, Any], status: str, error: str | None, runs: list[dict[str, Any]],
            notes: list[str], partial_reasons: list[str], request_count: int) -> dict[str, Any]:
    p = cfg["psi"]
    counts = {s: sum(1 for r in runs if r["status"] == s) for s in ("ok", "failed", "skipped")}
    counts["no_url_field_data"] = sum(1 for r in runs if r["status"] == "ok" and not (r["field"] or {}).get("url"))
    return {
        "status": status,
        "error": error,
        "partial_reasons": partial_reasons,
        "notes": notes,
        "endpoint": PSI_ENDPOINT,
        "limits": {"timeout_seconds": p["timeout_seconds"], "max_retries": p["max_retries"]},
        "strategies": list(p["strategies"]),
        "urls": list(dict.fromkeys(r["url"] for r in runs)),
        "counts": counts,
        "requests_made": request_count,
        "runs": runs,
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def failed_record(cfg: dict[str, Any], error: str) -> dict[str, Any]:
    """A failed PSI record with no runs (used when the collector itself could not run)."""
    return _record(cfg, "failed", error, [], [], [], 0)


def collect_psi(cfg: dict[str, Any], deadline: float | None = None, client: PsiClient | None = None,
                env: Mapping[str, str] | None = None, session: requests.Session | None = None,
                clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """PSI record for today's snapshot. Never raises for API problems.

    deadline: value of the client's clock after which no call may still be
    running (None = no limit).
    """
    p = cfg["psi"]
    urls, notes = select_urls(cfg)
    planned = [(u, s) for u in urls for s in p["strategies"]]

    def blank_runs(status: str, error: str | None) -> list[dict[str, Any]]:
        return [{"url": u, "strategy": s, "status": status, "error": error, "http_status": None, "attempts": 0,
                 "duration_s": None, "lighthouse_version": None, "fetched_at": None, "final_url": None,
                 "run_warnings": [], "lab": None, "field": None} for u, s in planned]

    if not p["enabled"]:
        return _record(cfg, "disabled", None, [], notes + ["psi.enabled is false"], [], 0)
    if not planned:
        return _record(cfg, "failed", "no PSI URL to test (every psi.urls entry was ignored)", [], notes, [], 0)
    if client is None:
        key = (env if env is not None else os.environ).get(p["api_key_env"], "").strip()
        if not key:
            err = f"no API key: environment variable {p['api_key_env']} is not set"
            return _record(cfg, "failed", err, blank_runs("failed", err), notes, [], 0)
        client = PsiClient(cfg, key, session=session, clock=clock, sleep=sleep)

    # Once one call no longer fits the budget, later ones do not either (they are skipped too).
    runs = [client.run(url, strategy, deadline) for url, strategy in planned]

    ok = [r for r in runs if r["status"] == "ok"]
    failed = [r for r in runs if r["status"] == "failed"]
    skipped = [r for r in runs if r["status"] == "skipped"]
    reasons = [f"{r['url']} ({r['strategy']}) failed: {r['error']}" for r in failed]
    if skipped:
        reasons.append(f"runtime.budget_seconds reached: {len(skipped)} run(s) not started")
    if not ok:
        errors = sorted({r["error"] for r in runs if r["error"]})
        error = "no PSI run succeeded: " + "; ".join(errors) if errors else "no PSI run succeeded"
        return _record(cfg, "failed", error, runs, notes, reasons, client.request_count)
    status = "partial" if (failed or skipped) else "ok"
    return _record(cfg, status, None, runs, notes, reasons, client.request_count)
