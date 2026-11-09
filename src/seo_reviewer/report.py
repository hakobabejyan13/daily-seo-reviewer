"""Report builder: findings.json -> reports/YYYY-MM-DD.md.

Deterministic except for the Summary section at the top. The report is
built only from the day's findings.json plus the day's snapshot (meta.json
for the run, a few coverage counts from the source records for the source
health table, and summary.json for the Summary section). The summary is
either written by the LLM (checked by summarize.validate_summary: every URL,
number and finding id is in findings.json) or the deterministic template;
the section says which one and, for the template, why.
Every number in the report is copied from those files; this module never
computes a metric, it only counts and lays out what the diff engine wrote.
suggested_next_step is printed as the rules wrote it.

Modes (findings.json "status"):
  - baseline: no earlier snapshot to compare with. The report says so and
    lists the current issues by severity.
  - issues:   at least one critical or warning finding. Full list:
    critical, warning, info; inside a severity regressions come first (the
    diff engine already sorted them that way).
  - stable:   no critical or warning finding. A short report: headline,
    one line of source health, the info findings as one-liners (capped)
    and the known-issue counts.

A failed source is always named at the top, with what was not compared
because of it ("PSI failed: ...; Core Web Vitals and PageSpeed scores not
compared today.").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import rules as R
from .diff import _fmt
from .snapshot import SnapshotError, _write_atomic, read_meta, read_source

# What a source failure means for the reader (static text, no numbers).
NOT_COMPARED = {
    "robots": "robots.txt rules not compared today",
    "sitemap": "sitemap URLs not compared today",
    "crawl": "on-page checks (status, title, description, H1, canonical, noindex) not compared today",
    "links": "internal links not checked today",
    "psi": "Core Web Vitals and PageSpeed scores not compared today",
}
SOURCE_LABEL = {"robots": "robots.txt", "sitemap": "Sitemap", "crawl": "Crawl", "links": "Links", "psi": "PSI"}
CHANGE_LABEL = {
    "baseline": "current issue (baseline)",
    "appeared": "new",
    "new_page": "new page",
    "failed": "failed",
    "partial": "partly collected",
    "worsened": "got worse",
    "pending": "possible regression, not confirmed yet",
    "confirmed": "regression confirmed",
    "changed": "changed",
    "removed": "removed",
    "dropped": "dropped",
    "resolved": "fixed",
    "recovered": "recovered",
    "improved": "improved",
    "cleared": "cleared",
}
SEVERITY_TITLE = {"critical": "Critical", "warning": "Warning", "info": "Info"}
STATUS_TITLE = {"baseline": "Baseline run", "issues": "Issues found", "stable": "All stable"}
STABLE_INFO_LINES = 10
LIST_OPEN_MAX = 5  # longer URL lists go into a collapsed <details> block


# ---- markdown helpers ------------------------------------------------------------

_MD_SPECIAL = "\\`*_[]<|"


def esc(text: Any) -> str:
    """Plain text safe for GitHub markdown (no accidental HTML, tables or emphasis)."""
    s = " ".join(str(text).split())
    return "".join("\\" + c if c in _MD_SPECIAL else c for c in s)


def code(text: Any) -> str:
    s = " ".join(str(text).split())
    if "`" not in s:
        return f"`{s}`"
    return "`` " + s.replace("``", "` `") + " ``"


def link(url: str | None) -> str:
    if not url:
        return "-"
    if any(c in url for c in " <>\n\t"):
        return code(url)
    return f"<{url}>"


def _cell(text: Any) -> str:
    return esc(text) if text not in (None, "") else "-"


# ---- inputs -----------------------------------------------------------------------

def load_inputs(root: str | Path, day: str) -> dict[str, Any]:
    """findings.json, meta.json and the source records of one snapshot day."""
    root = Path(root)
    doc = read_source(root, day, "findings")
    if doc is None:
        raise SnapshotError(f"no findings.json for {day} in {root} (run the diff first)")
    meta = read_meta(root, day)
    records = {src: read_source(root, day, src) for src in R.SOURCES}
    return {"findings": doc, "meta": meta, "records": {k: v for k, v in records.items() if v is not None}}


def coverage(source: str, rec: dict[str, Any] | None) -> str:
    """Short coverage text from a source record ("51 of 51 pages fetched").
    Empty when the record is missing or does not have the fields."""
    if not rec or rec.get("status") == "disabled":
        return ""
    try:
        if source == "robots":
            http = f"HTTP {rec['http_status']}, " if rec.get("http_status") else ""
            return f"{http}mode {rec['mode']}"
        if source == "sitemap":
            files = rec["sitemaps"]
            ok = sum(1 for s in files if s.get("status") == "ok")
            return f"{rec['url_count']} URLs from {ok} of {len(files)} file(s)"
        if source == "crawl":
            c = rec["counts"]
            return f"{c.get('ok', 0)} of {rec['pages_selected']} pages fetched"
        if source == "links":
            c = rec["counts"]
            return (f"{c['checked']} checked, {c['broken']} broken, "
                    f"{rec['left_out_by_cap']} over the cap")
        if source == "psi":
            c = rec["counts"]
            return f"{c['ok']} of {len(rec['runs'])} run(s) ok"
    except (KeyError, TypeError):
        return ""
    return ""


def source_health(doc: dict[str, Any], records: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """One row per source: status, compared_to, coverage, notes and, for a
    failed source, the explicit sentence. Also used for the Telegram message."""
    records = records or {}
    skipped = {f["source"]: f for f in doc["findings"] if f["rule"] == "source_skipped_upstream"}
    rows = []
    order = [s for s in R.SOURCES if s in doc["sources"]] + [s for s in doc["sources"] if s not in R.SOURCES]
    for src in order:
        info = doc["sources"][src]
        status = info["status"]
        alert = None
        if status not in ("ok", "partial", "disabled"):
            if src in skipped:
                alert = f"{skipped[src]['message']}; {NOT_COMPARED.get(src, 'not compared today')}."
            else:
                alert = (f"{src.upper()} failed: {info.get('error') or status}; "
                         f"{NOT_COMPARED.get(src, 'not compared today')}.")
        if info.get("compared_to"):
            compared = info["compared_to"]
        elif info.get("baseline"):
            compared = "baseline (no earlier ok day)"
        else:
            compared = "not compared"
        rows.append({"source": src, "label": SOURCE_LABEL.get(src, src), "status": status, "compared": compared,
                     "coverage": coverage(src, records.get(src)), "notes": list(info.get("notes") or []),
                     "alert": alert})
    return rows


# ---- pieces -------------------------------------------------------------------------

def _compared_line(doc: dict[str, Any]) -> str:
    if doc.get("compare_to"):
        return f"Compared with: {doc['compare_to']} (chosen with --compare-to)"
    days = sorted({i["compared_to"] for i in doc["sources"].values() if i.get("compared_to")})
    if not days:
        return "Compared with: nothing yet (first run)"
    if len(days) == 1:
        return f"Compared with: {days[0]}"
    return f"Compared with: last ok day per source ({', '.join(days)}), see Source health"


def _run_line(meta: dict[str, Any] | None, records: dict[str, Any]) -> str | None:
    if not meta:
        return None
    parts = []
    if meta.get("run_started_at"):
        parts.append(meta["run_started_at"] + (f" to {meta['run_finished_at']}" if meta.get("run_finished_at") else ""))
    if meta.get("requests_made") is not None:
        parts.append(f"{meta['requests_made']} request(s) to the site")
    psi = records.get("psi") or {}
    if psi.get("requests_made") is not None:
        parts.append(f"{psi['requests_made']} to the PageSpeed Insights API")
    return ("Run: " + ", ".join(parts)) if parts else None


def _default_message(f: dict[str, Any]) -> str:
    return f"{f['check']}: {f['url']}" if f.get("url") else f["check"]


def _url_list(f: dict[str, Any]) -> tuple[list[str], int]:
    """(lines, total) for a finding that covers several URLs."""
    if f.get("items"):
        lines = []
        for it in f["items"]:
            if f["change"] == "baseline":
                val = f"now {_fmt(it.get('new_value'))}"
            else:
                val = f"{_fmt(it.get('old_value'))} -> {_fmt(it.get('new_value'))}"
            lines.append(f"{link(it['url'])}: {code(val)}")
        return lines, f.get("count") or len(f["items"])
    det = f.get("details") or {}
    if det.get("urls"):
        return [link(u) for u in det["urls"]], det.get("count") or len(det["urls"])
    return [], 0


def _list_block(lines: list[str], total: int, what: str = "URLs") -> list[str]:
    left = total - len(lines)
    tail = [f"- ... and {left} more (see findings.json)"] if left > 0 else []
    body = [f"- {x}" for x in lines] + tail
    if len(lines) <= LIST_OPEN_MAX:
        return body
    return ["", f"<details><summary>{total} {what}</summary>", "", *body, "", "</details>", ""]


def finding_block(f: dict[str, Any]) -> list[str]:
    change = CHANGE_LABEL.get(f["change"], f["change"])
    tag = " · regression" if f.get("regression") and f["change"] != "baseline" else ""
    out = [f"#### {f['id']} · {esc(f['check'])}", "",
           f"- Severity: **{f['severity']}** · {esc(change)}{tag} · source: {f['source']} (rule {code(f['rule'])})"]
    if f.get("message") and f["message"] != _default_message(f):
        out.append(f"- What: {esc(f['message'])}")
    if f.get("url"):
        out.append(f"- URL: {link(f['url'])}")
    if f.get("evidence"):
        out.append(f"- Evidence: {code(f['evidence'])} ({esc(f['evidence_source'])})")
    det = f.get("details") or {}
    if det.get("linked_from"):
        more = (det.get("linked_from_count") or 0) - len(det["linked_from"])
        out.append(f"- Linked from {det.get('linked_from_count') or len(det['linked_from'])} page(s), e.g. "
                   + ", ".join(link(u) for u in det["linked_from"][:3]) + (" ..." if more > 0 or len(det["linked_from"]) > 3 else ""))
    lines, total = _url_list(f)
    if lines:
        out.append(f"- Affected: {total} URL(s)")
        out.extend(_list_block(lines, total))
    out.append(f"- Next step: {esc(f['suggested_next_step'])}")
    out.append("")
    return out


def finding_line(f: dict[str, Any]) -> str:
    """One-line form (stable mode)."""
    msg = f["message"] if f.get("message") else _default_message(f)
    n = f" ({f['count']} URLs)" if (f.get("count") or 1) > 1 else ""
    ev = f" {code(f['evidence'])}" if f.get("evidence") else ""
    return f"- {f['id']} {f['severity']} · {esc(msg)}{n}{ev}"


def _health_section(rows: list[dict[str, Any]]) -> list[str]:
    out = ["## Source health", ""]
    alerts = [r["alert"] for r in rows if r["alert"]]
    for a in alerts:
        out.append(f"> **{esc(a)}**")
        out.append(">")
    if alerts:
        out[-1] = ""
    out += ["| Source | Status | Compared with | Coverage | Notes |", "|---|---|---|---|---|"]
    for r in rows:
        status = f"**{r['status']}**" if r["status"] not in ("ok", "disabled") else r["status"]
        out.append(f"| {r['label']} | {status} | {_cell(r['compared'])} | {_cell(r['coverage'])} | "
                   f"{_cell('; '.join(r['notes']))} |")
    out.append("")
    return out


def _ongoing_section(doc: dict[str, Any], heading: bool = True) -> list[str]:
    ongoing = doc.get("ongoing") or []
    if not ongoing:
        return []
    out = ["## Known issues (unchanged since the last compared run)", ""] if heading else []
    for o in ongoing:
        first = (o.get("urls") or [""])[0]
        eg = ""
        if first:
            eg = f", e.g. {link(first) if first.startswith('http') and ' ' not in first else code(first)}"
        out.append(f"- {esc(o['check'])} ({o['severity']}, rule {code(o['rule'])}): {o['count']} item(s){eg}")
    out.append("")
    return out


def summary_section(summary: dict[str, Any] | None, headline: str = "") -> list[str]:
    if not summary or not summary.get("text"):
        return []
    if summary.get("source") == "gemini":
        note = (f"Written by Gemini ({esc(summary.get('model') or '?')}) from findings.json only; checked: "
                f"every URL, number and finding id in it appears in findings.json.")
    else:
        note = f"Template summary (no AI). Gemini not used: {esc((summary.get('reason') or 'unknown reason').rstrip('.'))}."
    out = ["## Summary", "", f"_{note}_", ""]
    lines = [x.strip() for x in summary["text"].splitlines()]
    if summary.get("source") != "gemini" and lines and lines[0].rstrip(".") == headline.rstrip("."):
        lines = lines[1:]  # the template starts with the headline, already printed above
    for line in lines:
        if not line:
            continue
        if out[-1] and not line.startswith("- "):
            out.append("")  # end a list before a plain line
        out.append(esc(line))
        if not line.startswith("- "):  # each plain line is its own paragraph; list items stay together
            out.append("")
    if out[-1]:
        out.append("")
    return out


def _footer(doc: dict[str, Any], summary: dict[str, Any] | None = None) -> list[str]:
    ai = ("The Summary section is written by Gemini and validated against findings.json; "
          "everything else is deterministic." if summary and summary.get("source") == "gemini"
          else "No AI-written text in this report.")
    return ["---", "",
            f"Built from data/snapshots/{doc['date']}/findings.json (generated {doc['generated_at']}, "
            f"tool {doc['tool_version']}) and the snapshot of the same day. Every number above is copied "
            f"from those files; next steps are written by the severity rules. {ai}",
            ""]


# ---- the report --------------------------------------------------------------------------

def build_report(doc: dict[str, Any], meta: dict[str, Any] | None = None,
                 records: dict[str, Any] | None = None, summary: dict[str, Any] | None = None) -> str:
    """Markdown report for one findings document."""
    records = records or {}
    site = doc["site"]
    status = doc["status"]
    counts = doc["counts"]
    rows = source_health(doc, records)
    out = [f"# SEO review: {esc(site['name'])}, {doc['date']}", "",
           f"**{esc(doc['headline'])}**", "",
           f"- Status: {STATUS_TITLE.get(status, status)} ({code(status)})",
           f"- Site: {link(site['base_url'])}",
           f"- {_compared_line(doc)}",
           f"- Findings: {counts['critical']} critical, {counts['warning']} warning, {counts['info']} info"]
    run = _run_line(meta, records)
    if run:
        out.append(f"- {esc(run)}")
    out.append("")
    out.extend(summary_section(summary, doc["headline"]))

    if status == "stable":
        # Short form: a few lines, not a long list.
        for r in rows:
            if r["alert"]:
                out.append(f"> **{esc(r['alert'])}**")
                out.append("")
        out.append("Sources: " + " · ".join(
            f"{r['label']} {r['status']}" + (f" ({esc(r['coverage'])})" if r["coverage"] else "") for r in rows))
        out.append("")
        infos = doc["findings"]
        if infos:
            out.append(f"Info ({len(infos)}):")
            out.append("")
            out.extend(finding_line(f) for f in infos[:STABLE_INFO_LINES])
            if len(infos) > STABLE_INFO_LINES:
                out.append(f"- ... and {len(infos) - STABLE_INFO_LINES} more info finding(s) in findings.json")
            out.append("")
        ongoing = doc.get("ongoing") or []
        if ongoing:
            out.append(f"Known issues ({len(ongoing)}, unchanged):")
            out.append("")
            out.extend(_ongoing_section(doc, heading=False))
        out.extend(_footer(doc, summary))
        return "\n".join(out)

    if status == "baseline":
        out += ["> This is a baseline run: there is no earlier snapshot to compare with, so the findings below "
                "are the current issues, not changes. Day-to-day changes start with the next run.", ""]

    out.extend(_health_section(rows))
    out += ["## Findings", ""]
    if not doc["findings"]:
        out += ["No findings.", ""]
    for sev in R.SEVERITIES:
        fs = [f for f in doc["findings"] if f["severity"] == sev]
        if not fs:
            continue
        out += [f"### {SEVERITY_TITLE[sev]} ({len(fs)})", ""]
        for f in fs:
            out.extend(finding_block(f))
    out.extend(_ongoing_section(doc))
    out.extend(_footer(doc, summary))
    return "\n".join(out)


def report_path(cfg: dict[str, Any], base_dir: str | Path, day: str) -> Path:
    d = Path(cfg["delivery"]["report_dir"])
    if not d.is_absolute():
        d = Path(base_dir) / d
    return d / f"{day}.md"


def write_report(cfg: dict[str, Any], base_dir: str | Path, day: str, text: str) -> Path:
    path = report_path(cfg, base_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(path, text.rstrip("\n"))  # adds the final newline
    return path
