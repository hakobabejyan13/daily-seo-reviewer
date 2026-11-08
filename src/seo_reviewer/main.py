"""Run the daily SEO review.

Usage (from the repo root):
    PYTHONPATH=src python -m seo_reviewer.main --collect-only [--config config.yaml] [--date YYYY-MM-DD]

Only --collect-only exists so far: it collects robots.txt, the sitemap,
the on-page crawl, the link check and PageSpeed Insights, writes today's
snapshot (UTC date) to storage.snapshot_dir and applies retention. Later
tasks add diff, report and delivery.

runtime.budget_seconds is counted from the start of the run; the crawl and
the link check stop starting new requests once it is used up (record status
partial). PSI calls go to Google's API, not the target site, so they run in a
background thread at the same time as the site collectors (each call takes
10-40 s, mostly waiting). A PSI call is only started when it can finish
within the budget even at psi.timeout_seconds.

A failed source does not crash the run: it is recorded with status "failed"
and its error in the snapshot. Exit code: 0 = snapshot written,
2 = config error or bad arguments, 1 = the snapshot could not be written.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .collectors.crawl import collect_crawl
from .collectors.links import collect_links, is_broken
from .collectors.psi import collect_psi, failed_record
from .collectors.robots import collect_robots
from .collectors.sitemap import collect_sitemap
from .config import ConfigError, load_config
from .http_client import PoliteClient
from .snapshot import (SnapshotError, apply_retention, build_meta, parse_day,
                       snapshot_root, utc_today, write_snapshot)


JOIN_GRACE_SECONDS = 5


class PsiWorker:
    """Runs collect_psi in a background thread with its own clock and deadline."""

    def __init__(self, cfg: dict[str, Any], psi_kwargs: dict[str, Any] | None = None):
        kw = dict(psi_kwargs or {})
        self.clock = kw.setdefault("clock", time.monotonic)
        self.deadline = kw["deadline"] = self.clock() + cfg["runtime"]["budget_seconds"]
        self.cfg = cfg
        self.result: dict[str, Any] | None = None
        self.crash: str | None = None
        self._thread = threading.Thread(target=self._run, args=(kw,), name="psi", daemon=True)

    def _run(self, kw: dict[str, Any]) -> None:
        try:
            self.result = collect_psi(self.cfg, **kw)
        except Exception as e:  # never lose the other sources because of PSI
            self.crash = f"{type(e).__name__}: {e}"

    def start(self) -> "PsiWorker":
        self._thread.start()
        return self

    def join(self) -> dict[str, Any]:
        """Wait until the PSI deadline (plus a grace period) at most."""
        self._thread.join(max(0.0, self.deadline - self.clock()) + JOIN_GRACE_SECONDS)
        if self._thread.is_alive():
            return failed_record(self.cfg, "PSI did not finish within runtime.budget_seconds")
        if self.result is None:
            return failed_record(self.cfg, f"PSI collector crashed: {self.crash}")
        return self.result


def collect(cfg: dict[str, Any], client: PoliteClient,
            psi_kwargs: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    deadline = client.now() + cfg["runtime"]["budget_seconds"]
    psi = PsiWorker(cfg, psi_kwargs).start()  # Google's API, runs while we crawl the site
    home = cfg["site"]["base_url"] + "/"
    records = {
        "robots": collect_robots(client, cfg["site"]["base_url"], [home, cfg["site"]["sitemap_url"]]),
        "sitemap": collect_sitemap(client, cfg),
    }
    records["crawl"] = collect_crawl(client, cfg, records["sitemap"], deadline=deadline)
    records["links"] = collect_links(client, cfg, records["crawl"], deadline=deadline)
    records["psi"] = psi.join()
    return records


MAX_LINK_ROWS = 50


def _links_lines(lr: dict[str, Any]) -> list[str]:
    c = lr["counts"]
    if lr["status"] == "disabled":
        return ["  links:   disabled (links.enabled is false)"]
    lines = [f"  links:   {lr['status']}, {lr['unique_links']} unique internal link(s): "
             f"{lr['from_crawl']} from the crawl, {lr['to_check']} to check, {lr['left_out_by_cap']} over the cap; "
             f"{c['checked']} checked ({c['get_fallback']} needed GET after HEAD), {c['broken']} broken "
             f"({c['http_4xx']} 4xx, {c['http_5xx']} 5xx, {c['failed']} failed), {c['redirected']} redirected, "
             f"{c['disallowed']} disallowed, {c['skipped']} skipped"]
    for note in lr["notes"]:
        lines.append(f"    note: {note}")
    if lr.get("error"):
        lines.append(f"    error: {lr['error']}")
    rows = [lk for lk in lr["links"] if is_broken(lk) or lk["redirect_chain"] or lk["status"] == "disallowed"]
    if rows:
        lines.append("    broken, redirected and disallowed links (status | http | method | linked from | url -> final):")
        for lk in rows[:MAX_LINK_ROWS]:
            final = f" -> {lk['final_url']}" if lk["final_url"] and lk["final_url"] != lk["url"] else ""
            lines.append(f"    {lk['status']} | {lk['http_status'] or '-'} | {lk['method'] or '-'} | "
                         f"{lk['linked_from_count']} | {lk['url']}{final}"
                         + (f"  ({lk['error']})" if lk.get("error") else ""))
        if len(rows) > MAX_LINK_ROWS:
            lines.append(f"    ... {len(rows) - MAX_LINK_ROWS} more in links.json")
    return lines


def _fmt(v: Any, unit: str = "") -> str:
    return "-" if v is None else f"{v}{unit}"


def _field_txt(f: dict[str, Any] | None) -> str:
    if not f:
        return "none"
    m = f["metrics"]
    parts = []
    for key, label, unit in (("lcp_ms", "LCP", " ms"), ("inp_ms", "INP", " ms"), ("cls", "CLS", "")):
        parts.append(f"{label} {m[key]['percentile']}{unit} {m[key]['category']}" if m.get(key) else f"{label} -")
    return f"{f.get('overall_category') or '?'} ({', '.join(parts)})"


def _psi_lines(pr: dict[str, Any]) -> list[str]:
    if pr["status"] == "disabled":
        return ["  psi:     disabled (psi.enabled is false)"]
    c = pr["counts"]
    lines = [f"  psi:     {pr['status']}, {len(pr['runs'])} run(s) ({len(pr['urls'])} URL(s) x "
             f"{', '.join(pr['strategies'])}): {c['ok']} ok, {c['failed']} failed, {c['skipped']} skipped; "
             f"{pr['requests_made']} API request(s)"]
    if pr.get("error"):
        lines.append(f"    PSI failed: {pr['error']}")
    for note in pr.get("notes", []):
        lines.append(f"    note: {note}")
    if pr["runs"]:
        lines.append("    status | strategy | score | LCP | CLS | TBT | time | url")
    for r in pr["runs"]:
        lab = r.get("lab") or {}
        lines.append(f"    {r['status']} | {r['strategy']} | {_fmt(lab.get('performance_score'))} | "
                     f"{_fmt(lab.get('lcp_ms'), ' ms')} | {_fmt(lab.get('cls'))} | {_fmt(lab.get('tbt_ms'), ' ms')} | "
                     f"{_fmt(r.get('duration_s'), ' s')} | {r['url']}"
                     + (f"  ({r['error']})" if r.get("error") else ""))
        if r["status"] == "ok":
            field = r.get("field") or {}
            lines.append(f"      field (CrUX, 28 days) URL: {_field_txt(field.get('url'))}; "
                         f"origin: {_field_txt(field.get('origin'))}")
    return lines


def _crawl_lines(cr: dict[str, Any]) -> list[str]:
    c = cr["counts"]
    lines = [f"  crawl:   {cr['status']}, {cr['pages_selected']} page(s) selected: "
             f"{c.get('ok', 0)} fetched ({c.get('http_200', 0)} HTTP 200, {c.get('redirected', 0)} redirected, "
             f"{c.get('noindex', 0)} noindex), {c.get('failed', 0)} failed, "
             f"{c.get('disallowed', 0)} disallowed, {c.get('skipped', 0)} skipped"]
    for note in cr["selection_notes"]:
        lines.append(f"    note: {note}")
    if cr.get("error"):
        lines.append(f"    error: {cr['error']}")
    lines.append("    status | http | title len | desc len | h1 | canonical | noindex | links | url")
    for p in cr["pages"]:
        seo = p.get("seo") or {}
        title, desc = seo.get("title"), seo.get("meta_description")
        canon = seo.get("canonical")
        canon_txt = "-" if not seo else ("missing" if not canon else ("self" if canon == p["final_url"] else "other"))
        hops = f"{p['http_status']}" + (f" after {len(p['redirect_chain'])} redirect(s)" if p["redirect_chain"] else "")
        lines.append(
            f"    {p['status']} | {hops if p['http_status'] else '-'} | "
            f"{len(title) if title is not None else '-'} | {len(desc) if desc is not None else '-'} | "
            f"{len(seo['h1']) if seo else '-'} | {canon_txt} | {'yes' if p['noindex'] else 'no'} | "
            f"{len(seo['internal_links']) if seo else '-'} | {p['url']}"
            + (f"  ({p['error']})" if p.get("error") else ""))
    return lines


def run_collect_only(cfg: dict[str, Any], base_dir: Path, day: str | None = None,
                     client: PoliteClient | None = None,
                     now: datetime | None = None,
                     psi_kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
    started = now or datetime.now(timezone.utc)
    day = day or utc_today(started)
    client = client or PoliteClient(cfg["crawler"])
    records = collect(cfg, client, psi_kwargs)
    root = snapshot_root(cfg, base_dir)
    meta = build_meta(cfg, day, records, started)
    meta["requests_made"] = client.request_count
    folder = write_snapshot(root, day, records, meta)
    deleted = apply_retention(root, day, cfg["storage"]["retention_days"])
    return {"day": day, "folder": folder, "records": records, "deleted": deleted,
            "requests_made": client.request_count}


def format_summary(result: dict[str, Any]) -> str:
    lines = [f"Snapshot {result['day']} written to {result['folder']}"]
    rb = result["records"]["robots"]
    lines.append(f"  robots:  {rb['status']}" + (f" ({rb['error']})" if rb.get("error") else "")
                 + f", mode {rb['mode']}")
    sm = result["records"]["sitemap"]
    read = sum(1 for s in sm["sitemaps"] if s["status"] == "ok")
    lines.append(f"  sitemap: {sm['status']}, {sm['url_count']} URLs from {read} of {len(sm['sitemaps'])} sitemap file(s)"
                 + (f", {len(sm['skipped_sitemaps'])} skipped" if sm["skipped_sitemaps"] else ""))
    for s in sm["sitemaps"]:
        detail = f"{s['url_count']} URLs" + (" (truncated)" if s.get("truncated") else "") if s["status"] == "ok" else s["error"]
        lines.append(f"    - {s['url']}: {s['status']}, {s.get('kind') or '?'}, {detail}")
    for u in sm["skipped_sitemaps"]:
        lines.append(f"    - {u}: skipped (cap)")
    if sm.get("error"):
        lines.append(f"    note: {sm['error']}")
    if "crawl" in result["records"]:
        lines.extend(_crawl_lines(result["records"]["crawl"]))
    if "links" in result["records"]:
        lines.extend(_links_lines(result["records"]["links"]))
    if "psi" in result["records"]:
        lines.extend(_psi_lines(result["records"]["psi"]))
    lines.append(f"  requests made: {result['requests_made']} to the site"
                 + (f", {result['records']['psi']['requests_made']} to the PSI API" if "psi" in result["records"] else ""))
    if result["deleted"]:
        lines.append(f"  retention: deleted {len(result['deleted'])} old snapshot(s): {', '.join(result['deleted'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily SEO reviewer.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--collect-only", action="store_true",
                    help="collect robots.txt, sitemap, on-page crawl, link check and PSI and write today's snapshot (the only mode so far)")
    ap.add_argument("--date", help="snapshot date YYYY-MM-DD (default: today, UTC)")
    args = ap.parse_args(argv)
    if not args.collect_only:
        print("Only --collect-only is implemented so far.", file=sys.stderr)
        return 2
    if args.date and parse_day(args.date) is None:
        print(f"--date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
        return 2
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        return 2
    base_dir = Path(args.config).resolve().parent  # snapshot_dir is relative to config.yaml
    try:
        result = run_collect_only(cfg, base_dir, day=args.date)
    except (SnapshotError, OSError) as e:
        print(f"SNAPSHOT ERROR: {e}", file=sys.stderr)
        return 1
    print(format_summary(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
