"""Crawlability check for the site in config.yaml.

Usage:
    python -m seo_reviewer.crawl_check [--config config.yaml] [--json out.json]

Checks:
  (a) robots.txt is reachable and allows our User-Agent for the homepage
      and the sitemap URL
  (b) the homepage returns HTTP 200 with real HTML, not a bot challenge
  (c) the sitemap is reachable and parses as a sitemap index or urlset
      (for an index, the first child sitemap is fetched and parsed too)

Exit code: 0 = all pass, 1 = at least one check failed, 2 = config error.
If GITHUB_STEP_SUMMARY is set (GitHub Actions), a markdown summary is
appended there as well.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup
from lxml import etree

from .collectors.robots import collect_robots
from .config import ConfigError, load_config
from .http_client import FetchError, PoliteClient, RobotsDisallowed

# Strings that only appear on bot-challenge / block pages. Kept specific on
# purpose: normal Cloudflare-served pages may load /cdn-cgi/challenge-platform/
# scripts, so that path alone is NOT treated as a challenge.
CHALLENGE_BODY_MARKERS = (
    "<title>just a moment...</title>",
    "attention required! | cloudflare",
    "cf-browser-verification",
    "cf_chl_opt",
    "/cdn-cgi/challenge-platform/h/",
    "enable javascript and cookies to continue",
    "checking if the site connection is secure",
    "checking your browser before accessing",
    "px-captcha",
    "_incapsula_resource",
    "access denied | ",
)
CHALLENGE_HEADERS = {"cf-mitigated": "challenge"}
BLOCK_STATUSES = {401, 403, 429, 503}
MIN_HTML_BYTES = 2000
SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


@dataclass
class CheckResult:
    name: str
    passed: bool
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


def detect_challenge(status: int, headers: dict[str, str], body: str) -> list[str]:
    """Return the reasons this response looks like a bot challenge or block page."""
    reasons: list[str] = []
    if status in BLOCK_STATUSES:
        reasons.append(f"HTTP {status}")
    lower_headers = {k.lower(): str(v).lower() for k, v in headers.items()}
    for h, v in CHALLENGE_HEADERS.items():
        if lower_headers.get(h) == v:
            reasons.append(f"header {h}: {v}")
    low = body[:200_000].lower()
    for marker in CHALLENGE_BODY_MARKERS:
        if marker in low:
            reasons.append(f"body contains {marker!r}")
    return reasons


def parse_sitemap(content: bytes) -> dict[str, Any]:
    """Parse sitemap XML (optionally gzipped). Raises ValueError if not a sitemap."""
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    try:
        root = etree.fromstring(content, parser=parser)
    except etree.XMLSyntaxError as e:
        raise ValueError(f"not valid XML: {e}") from None
    kind = etree.QName(root).localname
    if kind not in ("sitemapindex", "urlset"):
        raise ValueError(f"root element is <{kind}>, expected <sitemapindex> or <urlset>")
    child_tag = "sitemap" if kind == "sitemapindex" else "url"
    entries = [el for el in root if isinstance(el.tag, str) and etree.QName(el).localname == child_tag]
    locs: list[str] = []
    with_lastmod = 0
    for el in entries:
        loc = el.find(f"{{{SITEMAP_NS}}}loc")
        if loc is None:
            loc = el.find("loc")
        if loc is not None and loc.text and loc.text.strip():
            locs.append(loc.text.strip())
        if el.find(f"{{{SITEMAP_NS}}}lastmod") is not None or el.find("lastmod") is not None:
            with_lastmod += 1
    return {"kind": kind, "entries": len(entries), "locs": locs, "with_lastmod": with_lastmod}


# ---- the three checks ---------------------------------------------------

def check_robots(client: PoliteClient, cfg: dict[str, Any]) -> CheckResult:
    home = cfg["site"]["base_url"] + "/"
    sitemap = cfg["site"]["sitemap_url"]
    rec = collect_robots(client, cfg["site"]["base_url"], [home, sitemap])
    allowed = rec["allowed"]
    problems = []
    if rec["status"] != "ok":
        problems.append(f"robots.txt failed: {rec['error']}")
    problems += [f"disallowed: {u}" for u, ok in allowed.items() if not ok]
    if sitemap not in rec["sitemaps_declared"] and rec["sitemaps_declared"]:
        note = f"sitemap_url not listed in robots.txt (declared: {', '.join(rec['sitemaps_declared'][:3])})"
    else:
        note = ""
    passed = not problems
    summary = (
        f"robots.txt HTTP {rec['http_status']}, mode {rec['mode']}; homepage and sitemap allowed for our User-Agent"
        if passed else "; ".join(problems)
    )
    if note:
        summary += f" (note: {note})"
    details = {k: v for k, v in rec.items() if k != "source"}
    return CheckResult("robots", passed, summary, details)


def check_homepage(client: PoliteClient, cfg: dict[str, Any]) -> CheckResult:
    url = cfg["site"]["base_url"] + "/"
    try:
        resp = client.get(url)
    except RobotsDisallowed as e:
        return CheckResult("homepage", False, str(e))
    except FetchError as e:
        return CheckResult("homepage", False, f"request failed: {e}")
    body = resp.text or ""
    ctype = resp.headers.get("Content-Type", "")
    details: dict[str, Any] = {
        "url": url,
        "final_url": resp.url,
        "http_status": resp.status_code,
        "redirects": [r.url for r in resp.history],
        "content_type": ctype,
        "size_bytes": len(resp.content or b""),
        "server": resp.headers.get("Server"),
        "cf_ray_present": "cf-ray" in {k.lower() for k in resp.headers},
    }
    problems = detect_challenge(resp.status_code, dict(resp.headers), body)
    if resp.status_code != 200 and f"HTTP {resp.status_code}" not in problems:
        problems.append(f"HTTP {resp.status_code}")
    if "html" not in ctype.lower():
        problems.append(f"Content-Type is {ctype!r}, not HTML")
    title = ""
    if body:
        soup = BeautifulSoup(body, "lxml")
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        details["links_found"] = len(soup.find_all("a", href=True))
    details["title"] = title
    if not title:
        problems.append("no <title> in HTML")
    if details["size_bytes"] < MIN_HTML_BYTES:
        problems.append(f"HTML only {details['size_bytes']} bytes (expected >= {MIN_HTML_BYTES})")
    details["challenge_reasons"] = problems
    if problems:
        return CheckResult("homepage", False, "looks blocked or not real HTML: " + "; ".join(problems), details)
    return CheckResult(
        "homepage", True,
        f"HTTP 200, {details['size_bytes']} bytes of HTML, title {title!r}, {details['links_found']} links",
        details,
    )


def _fetch_and_parse_sitemap(client: PoliteClient, url: str) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    meta: dict[str, Any] = {"url": url}
    try:
        resp = client.get(url)
    except RobotsDisallowed as e:
        return None, str(e), meta
    except FetchError as e:
        return None, f"request failed: {e}", meta
    meta.update(http_status=resp.status_code, content_type=resp.headers.get("Content-Type", ""),
                size_bytes=len(resp.content or b""))
    if resp.status_code != 200:
        reasons = detect_challenge(resp.status_code, dict(resp.headers), resp.text or "")
        return None, f"HTTP {resp.status_code}" + (f" ({'; '.join(reasons)})" if reasons else ""), meta
    try:
        parsed = parse_sitemap(resp.content)
    except (ValueError, OSError) as e:
        return None, f"not parseable: {e}", meta
    return parsed, None, meta


def check_sitemap(client: PoliteClient, cfg: dict[str, Any]) -> CheckResult:
    url = cfg["site"]["sitemap_url"]
    parsed, err, meta = _fetch_and_parse_sitemap(client, url)
    if err or parsed is None:
        return CheckResult("sitemap", False, f"{url}: {err}", meta)
    meta.update(kind=parsed["kind"], entries=parsed["entries"], with_lastmod=parsed["with_lastmod"],
                sample=parsed["locs"][:3])
    if parsed["entries"] == 0:
        return CheckResult("sitemap", False, f"{url}: <{parsed['kind']}> has no entries", meta)
    summary = f"<{parsed['kind']}> with {parsed['entries']} entries ({parsed['with_lastmod']} with lastmod)"
    if parsed["kind"] == "sitemapindex":
        child_url = parsed["locs"][0] if parsed["locs"] else None
        if not child_url:
            return CheckResult("sitemap", False, f"{url}: index entries have no <loc>", meta)
        child, cerr, cmeta = _fetch_and_parse_sitemap(client, child_url)
        meta["first_child"] = cmeta
        if cerr or child is None:
            return CheckResult("sitemap", False, f"index OK but first child {child_url}: {cerr}", meta)
        cmeta.update(kind=child["kind"], entries=child["entries"], with_lastmod=child["with_lastmod"])
        summary += f"; first child <{child['kind']}> with {child['entries']} entries ({child['with_lastmod']} with lastmod)"
    return CheckResult("sitemap", True, summary, meta)


def run_crawl_check(cfg: dict[str, Any], client: PoliteClient | None = None) -> dict[str, Any]:
    client = client or PoliteClient(cfg["crawler"])
    results = [check_robots(client, cfg), check_homepage(client, cfg), check_sitemap(client, cfg)]
    return {
        "site": cfg["site"]["name"],
        "base_url": cfg["site"]["base_url"],
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "user_agent": client.user_agent,
        "requests_made": client.request_count,
        "passed": all(r.passed for r in results),
        "checks": [asdict(r) for r in results],
    }


def format_text(report: dict[str, Any]) -> str:
    lines = [f"Crawl check: {report['site']} ({report['base_url']})",
             f"User-Agent: {report['user_agent']}"]
    for c in report["checks"]:
        lines.append(f"  [{'PASS' if c['passed'] else 'FAIL'}] {c['name']}: {c['summary']}")
    lines.append(f"Requests made: {report['requests_made']}")
    lines.append("RESULT: " + ("PASS, site is crawlable" if report["passed"] else "FAIL, see above"))
    return "\n".join(lines)


def format_markdown(report: dict[str, Any]) -> str:
    lines = [f"## Crawl check: {report['site']}", "",
             f"Base URL: {report['base_url']}  ", f"Checked at: {report['checked_at']}  ",
             f"User-Agent: `{report['user_agent']}`", "",
             "| Check | Result | Details |", "|---|---|---|"]
    for c in report["checks"]:
        summary = c["summary"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|")
        lines.append(f"| {c['name']} | {'PASS' if c['passed'] else 'FAIL'} | {summary} |")
    lines += ["", "**Overall: " + ("PASS" if report["passed"] else "FAIL") + "**", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check that the configured site can be crawled politely.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--json", dest="json_path", help="also write the full result as JSON to this path")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        return 2
    report = run_crawl_check(cfg)
    print(format_text(report))
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(format_markdown(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
