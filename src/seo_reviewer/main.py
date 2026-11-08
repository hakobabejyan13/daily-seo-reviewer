"""Run the daily SEO review.

Usage (from the repo root):
    PYTHONPATH=src python -m seo_reviewer.main --collect-only [--config config.yaml] [--date YYYY-MM-DD]

Only --collect-only exists so far: it collects robots.txt, the sitemap and
the on-page crawl, writes today's snapshot (UTC date) to
storage.snapshot_dir and applies retention. Later tasks add links, PSI, diff,
report and delivery.

runtime.budget_seconds is counted from the start of the run; the crawl
stops starting new requests once it is used up (record status partial).

A failed source does not crash the run: it is recorded with status "failed"
and its error in the snapshot. Exit code: 0 = snapshot written,
2 = config error or bad arguments, 1 = the snapshot could not be written.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .collectors.crawl import collect_crawl
from .collectors.robots import collect_robots
from .collectors.sitemap import collect_sitemap
from .config import ConfigError, load_config
from .http_client import PoliteClient
from .snapshot import (SnapshotError, apply_retention, build_meta, parse_day,
                       snapshot_root, utc_today, write_snapshot)


def collect(cfg: dict[str, Any], client: PoliteClient) -> dict[str, dict[str, Any]]:
    deadline = client.now() + cfg["runtime"]["budget_seconds"]
    home = cfg["site"]["base_url"] + "/"
    records = {
        "robots": collect_robots(client, cfg["site"]["base_url"], [home, cfg["site"]["sitemap_url"]]),
        "sitemap": collect_sitemap(client, cfg),
    }
    records["crawl"] = collect_crawl(client, cfg, records["sitemap"], deadline=deadline)
    return records


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
                     now: datetime | None = None) -> dict[str, Any]:
    started = now or datetime.now(timezone.utc)
    day = day or utc_today(started)
    client = client or PoliteClient(cfg["crawler"])
    records = collect(cfg, client)
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
    lines.append(f"  requests made: {result['requests_made']}")
    if result["deleted"]:
        lines.append(f"  retention: deleted {len(result['deleted'])} old snapshot(s): {', '.join(result['deleted'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily SEO reviewer.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--collect-only", action="store_true",
                    help="collect robots.txt, sitemap and on-page crawl and write today's snapshot (the only mode so far)")
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
