"""Severity rules: one entry per rule id.

Every finding the diff engine produces names one rule here. The rule gives
the default severity, a short label for the check, and the suggested next
step. The next step is written here, by the rules, never by the LLM.

Severities can be changed per rule in config.yaml
(rules.severity_overrides: {rule_id: critical|warning|info}). A "resolved",
"recovered", "improved" or "cleared" finding is always info, whatever the
rule's severity is, and so is a "pending" (unconfirmed) PSI lab regression.
"""

from __future__ import annotations

from typing import Any

SEVERITIES = ("critical", "warning", "info")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# Changes that mean something got better (always reported as info).
GOOD_CHANGES = frozenset({"resolved", "recovered", "improved", "cleared"})
# Changes that mean something got worse (sorted first inside a severity).
REGRESSION_CHANGES = frozenset({"appeared", "new_page", "failed", "worsened", "pending", "confirmed",
                                "changed", "removed", "dropped"})

SOURCES = ("robots", "sitemap", "crawl", "links", "psi")

RULES: dict[str, dict[str, str]] = {
    # ---- source health -------------------------------------------------------
    "source_failed": {
        "severity": "warning", "check": "data source failed",
        "next_step": "Read the error in this finding and the run log; nothing from this source was compared today. "
                     "If it fails again tomorrow, fix the cause (API key, network, site down).",
    },
    "source_failed_critical": {
        "severity": "critical", "check": "data source failed (site data)",
        "next_step": "Check that the site and its robots.txt/sitemap answer from the GitHub runner; "
                     "nothing from this source was compared today.",
    },
    "source_skipped_upstream": {
        "severity": "info", "check": "data source not run (an earlier source failed)",
        "next_step": "Fix the earlier failed source; this one depends on it.",
    },
    "source_partial": {
        "severity": "info", "check": "data source partly collected",
        "next_step": "Only the collected part was compared. If this repeats, look at the reasons "
                     "(time budget, caps, failed items).",
    },
    "source_recovered": {
        "severity": "info", "check": "data source works again",
        "next_step": "No action needed.",
    },
    # ---- robots.txt -------------------------------------------------------------
    "robots_blocks_url": {
        "severity": "critical", "check": "robots.txt blocks a key URL",
        "next_step": "Check the latest robots.txt change; if blocking this URL is not intended, revert the rule.",
    },
    "robots_mode_changed": {
        "severity": "critical", "check": "robots.txt mode changed",
        "next_step": "Open robots.txt and check its HTTP status; disallow_all means crawlers are shut out.",
    },
    "robots_mode_info": {
        "severity": "info", "check": "robots.txt mode changed (parsed / allow all)",
        "next_step": "Check that robots.txt is served as intended (HTTP 200 with the expected rules).",
    },
    "robots_sitemaps_changed": {
        "severity": "info", "check": "sitemaps declared in robots.txt changed",
        "next_step": "Confirm the declared sitemap URLs are the intended ones.",
    },
    # ---- sitemap ------------------------------------------------------------------
    "sitemap_urls_added": {
        "severity": "info", "check": "new URLs in the sitemap",
        "next_step": "No action needed if these are new pages; spot-check that they are meant to be indexed.",
    },
    "sitemap_urls_removed": {
        "severity": "info", "check": "URLs removed from the sitemap",
        "next_step": "Confirm the removals are intended (deleted or merged pages) and that removed pages redirect.",
    },
    "sitemap_urls_dropped": {
        "severity": "warning", "check": "large share of sitemap URLs removed",
        "next_step": "Check the sitemap generator: a big drop is often a broken build, not real deletions.",
    },
    "sitemap_lastmod_changed": {
        "severity": "info", "check": "sitemap lastmod changed",
        "next_step": "No action needed; pages with a new lastmod are re-crawled by search engines.",
    },
    # ---- on-page crawl --------------------------------------------------------------
    "page_http_error": {
        "severity": "critical", "check": "page returns HTTP 4xx/5xx",
        "next_step": "Open the URL; restore the page or redirect it (301) to its replacement, "
                     "and remove it from the sitemap if it is gone for good.",
    },
    "page_fetch_failed": {
        "severity": "warning", "check": "page could not be fetched",
        "next_step": "Retry the URL by hand; if it keeps failing, check server logs for timeouts or blocks.",
    },
    "page_disallowed": {
        "severity": "warning", "check": "page blocked by robots.txt",
        "next_step": "Check robots.txt: a page listed in the sitemap should not be disallowed.",
    },
    "page_redirects": {
        "severity": "warning", "check": "page redirects",
        "next_step": "Update the sitemap and internal links to the final URL.",
    },
    "page_noindex": {
        "severity": "critical", "check": "page is noindex",
        "next_step": "If the page should rank, remove the noindex (meta robots or X-Robots-Tag) and redeploy.",
    },
    "canonical_missing": {
        "severity": "critical", "check": "canonical tag missing",
        "next_step": "Add <link rel=\"canonical\"> pointing to the page's preferred URL.",
    },
    "canonical_other": {
        "severity": "warning", "check": "canonical points to another URL",
        "next_step": "Confirm the canonical target is intended; if this page is the original, make it self-canonical.",
    },
    "canonical_multiple": {
        "severity": "warning", "check": "more than one canonical tag",
        "next_step": "Keep exactly one canonical tag; search engines may ignore conflicting ones.",
    },
    "canonical_changed": {
        "severity": "warning", "check": "canonical changed",
        "next_step": "Confirm the new canonical target is intended (template or CMS change?).",
    },
    "canonical_target_broken": {
        "severity": "warning", "check": "canonical target is broken",
        "next_step": "Point the canonical to a URL that returns HTTP 200.",
    },
    "canonical_slash_mismatch": {
        "severity": "info", "check": "sitemap URL and canonical differ only by a trailing slash",
        "next_step": "List the canonical form (as in the canonical tag) in the sitemap, or 301-redirect "
                     "the other form, so only one URL form is served.",
    },
    "title_missing": {
        "severity": "warning", "check": "title missing or empty",
        "next_step": "Add a unique, descriptive <title> to the page template.",
    },
    "title_length": {
        "severity": "info", "check": "title length out of range",
        "next_step": "Rewrite the title to fit the configured length range (thresholds.title_length).",
    },
    "title_duplicate": {
        "severity": "info", "check": "title shared with another crawled page",
        "next_step": "Give each page its own title.",
    },
    "title_changed": {
        "severity": "info", "check": "title changed",
        "next_step": "No action needed if the edit was intended.",
    },
    "meta_description_missing": {
        "severity": "warning", "check": "meta description missing or empty",
        "next_step": "Add a meta description that summarizes the page.",
    },
    "meta_description_length": {
        "severity": "info", "check": "meta description length out of range",
        "next_step": "Rewrite the description to fit thresholds.meta_description_length.",
    },
    "meta_description_duplicate": {
        "severity": "info", "check": "meta description shared with another crawled page",
        "next_step": "Give each page its own description.",
    },
    "meta_description_changed": {
        "severity": "info", "check": "meta description changed",
        "next_step": "No action needed if the edit was intended.",
    },
    "h1_missing": {
        "severity": "warning", "check": "H1 missing",
        "next_step": "Add one H1 that states the page topic.",
    },
    "h1_multiple": {
        "severity": "info", "check": "more than one H1",
        "next_step": "Keep one H1 per page; turn the others into H2.",
    },
    "h1_changed": {
        "severity": "info", "check": "H1 changed",
        "next_step": "No action needed if the edit was intended.",
    },
    # ---- links ------------------------------------------------------------------------
    "link_broken": {
        "severity": "warning", "check": "broken internal link",
        "next_step": "Fix or remove the link on the pages listed in linked_from, or redirect the target.",
    },
    "link_redirects": {
        "severity": "info", "check": "internal link now redirects",
        "next_step": "Update internal links to the final URL to save a redirect hop.",
    },
    # ---- PageSpeed Insights ---------------------------------------------------------------
    "psi_performance_score": {
        "severity": "warning", "check": "Lighthouse performance score dropped",
        "next_step": "Open the PSI report for this URL and strategy and compare the opportunities with the last good run.",
    },
    "psi_lcp": {
        "severity": "warning", "check": "lab LCP got slower",
        "next_step": "Check the LCP element (hero image, fonts, server response time) in the PSI report.",
    },
    "psi_cls": {
        "severity": "warning", "check": "lab CLS got worse",
        "next_step": "Look for new elements without reserved space (images, ads, embeds, late fonts).",
    },
    "psi_tbt": {
        "severity": "warning", "check": "lab TBT got worse",
        "next_step": "Look for new or heavier JavaScript (third-party tags, bundles).",
    },
    "crux_poor": {
        "severity": "critical", "check": "Core Web Vital (CrUX) went from good to poor",
        "next_step": "Real users see this now (28-day data); find the change that caused it and fix it first.",
    },
    "crux_worse": {
        "severity": "warning", "check": "Core Web Vital (CrUX) category got worse",
        "next_step": "Compare with lab data for this URL; field data moves slowly, so act before it reaches poor.",
    },
    "crux_better": {
        "severity": "info", "check": "Core Web Vital (CrUX) category improved",
        "next_step": "No action needed.",
    },
    "crux_data_changed": {
        "severity": "info", "check": "CrUX field data appeared or disappeared",
        "next_step": "No action needed; CrUX only has data for URLs with enough real-user traffic.",
    },
    "crux_slow": {
        "severity": "warning", "check": "Core Web Vital (CrUX) is poor",
        "next_step": "Real users see a poor value; investigate the metric in the PSI report.",
    },
    "crux_average": {
        "severity": "info", "check": "Core Web Vital (CrUX) needs improvement",
        "next_step": "Plan an improvement before it slips to poor.",
    },
}


def severity_for(rule: str, change: str, overrides: dict[str, str] | None = None) -> str:
    # "pending" = possible PSI lab regression not yet confirmed: info until confirmed.
    if change in GOOD_CHANGES or change == "pending":
        return "info"
    if overrides and rule in overrides:
        return overrides[rule]
    return RULES[rule]["severity"]


def next_step_for(rule: str, change: str) -> str:
    if change in GOOD_CHANGES:
        return "No action needed."
    return RULES[rule]["next_step"]


def check_for(rule: str) -> str:
    return RULES[rule]["check"]


def validate_overrides(overrides: Any) -> list[str]:
    """Problems in rules.severity_overrides (used by config.py)."""
    if not isinstance(overrides, dict):
        return [f"rules.severity_overrides: must be a mapping, got {overrides!r}"]
    errors = []
    for k, v in overrides.items():
        if k not in RULES:
            errors.append(f"rules.severity_overrides: unknown rule {k!r}")
        elif v not in SEVERITIES:
            errors.append(f"rules.severity_overrides.{k}: must be one of {', '.join(SEVERITIES)}, got {v!r}")
    return errors
