# data/

The data the reviewer works from. Every number in a review is copied from
these files.

```
data/snapshots/YYYY-MM-DD/   one folder per UTC day, written by the daily run
  meta.json       run times, site, status of every source
  robots.json     robots.txt          (source: the target site)
  sitemap.json    sitemap URLs        (source: the target site)
  crawl.json      on-page SEO fields  (source: the target site)
  links.json      internal link check (source: the target site)
  psi.json        PageSpeed Insights  (source: Google PSI API v5)
  findings.json   diff result         (made by the tool from the files above)
  summary.json    summary text        (Gemini, or the template)
  delivery.json   Telegram result     (Telegram Bot API)
```

The reports built from them are in `reports/YYYY-MM-DD.md` (the Markdown
review linked from the Telegram message).

**Where the data comes from and who writes it:** everything under
`snapshots/` and `../reports/` is written by the `daily-review` GitHub
Actions workflow (commits named `report: YYYY-MM-DD` by
github-actions[bot]). Nothing in these folders is edited by hand. The
workflow is started every day at 09:00 Asia/Yerevan (05:00 UTC) by an
external cron-job.org job through GitHub's `workflow_dispatch` API, or by
the "Run workflow" button.

**History in this repo (2 days):**
- [snapshots/2026-11-08/](snapshots/2026-11-08/) and
  [reports/2026-11-08.md](../reports/2026-11-08.md): the first run, a
  baseline (nothing to compare with). Run 42528201040, collection
  22:02:57-22:06:27 UTC. 10,236 sitemap URLs, 51 pages crawled, 150 links
  checked, 0 broken, PSI mobile 99 / desktop 100, 3 info findings.
  Summary: template (Gemini timed out on the older model gemini-3.8-flash).
  Telegram: sent, 1,546 characters.
- [snapshots/2026-11-09/](snapshots/2026-11-09/) and
  [reports/2026-11-09.md](../reports/2026-11-09.md): the first day-over-day
  comparison, with 2026-11-08. Run 42586153895, collection
  08:28:21-08:31:51 UTC. All stable, 1 info finding: mobile lab LCP on the
  homepage 2251 ms -> 3001 ms, "possible regression, awaiting confirmation
  (1 of 2)". The 3 issues from the first day are listed as unchanged
  (`ongoing`: 50 / 38 / 39 pages). PSI mobile 95 / desktop 100. Summary:
  Gemini (`gemini-3.5-flash-lite`, validated). Telegram: the 2-line
  all-stable message (384 characters).

  2026-11-09 was run three times. Run 42571863547 (06:14 UTC) was
  committed first; run 42586153895 (08:28 UTC) replaced it, because a
  re-run of the same UTC day overwrites that day's folder (its LCP was
  2701 ms, PSI mobile 96; only the newest files are kept). Run 42586448513
  (queued at 08:30, started 08:31:58 UTC) collected and sent its Telegram
  message, but its push conflicted with the files just committed, so it
  committed nothing and sent a failure alert. The commit-back step now
  keeps the newest run's files on such a conflict instead of failing.

Later days are added by the workflow, one folder and one report per day.
Folders older than 90 days are deleted (`storage.retention_days`).

Where each source comes from, and its limits: [docs/data-sources.md](../docs/data-sources.md).

## General rules

- Compact JSON, UTF-8, sorted keys, written atomically (no half files).
- Times are UTC ISO 8601. The folder name is the UTC date of the run.
  Re-running a day overwrites it.
- Every source record has `source`, `status` (`ok` / `partial` / `failed`),
  `error` (null or the error text) and, when partial, `partial_reasons`.
- A record keeps its `limits` (the caps from `config.yaml` used that day), so
  a reader knows what was not collected.
- No secrets: API keys and tokens are never stored.
- Retention: folders older than 90 days are deleted
  (`storage.retention_days`).
- Size: about 3.1 MB per day (crawl.json 1.9 MB, sitemap.json 0.95 MB,
  links.json 0.2 MB, the rest under 15 KB). Git stores unchanged parts
  efficiently.

## meta.json

| Field | Meaning |
|---|---|
| `date` | Day of the snapshot (YYYY-MM-DD) |
| `run_started_at`, `run_finished_at` | Collection start and end |
| `site` | `name`, `base_url`, `sitemap_url` from config |
| `sources` | `{source: {status, error}}` for robots, sitemap, crawl, links, psi |
| `requests_made` | Requests sent to the target site |
| `tool_version`, `schema_version` | For readers of old files |

## robots.json

| Field | Meaning |
|---|---|
| `url`, `http_status`, `size_bytes`, `fetched_at` | The robots.txt fetch |
| `mode` | `parsed`, `allow_all` (no robots.txt) or `disallow_all` (401/403/5xx) |
| `allowed` | `{url: true/false}` for the homepage and the sitemap URL, for our User-Agent |
| `sitemaps_declared` | `Sitemap:` lines in robots.txt |
| `user_agent` | The User-Agent the rules were checked for |

## sitemap.json

| Field | Meaning |
|---|---|
| `root_url`, `root_kind` | `site.sitemap_url`; `sitemapindex` or `urlset` |
| `url_count`, `urls_with_lastmod` | Totals over all child sitemaps |
| `sitemaps[]` | One entry per sitemap file: `url`, `kind`, `http_status`, `status`, `url_count`, `entries`, `truncated`, `error`, and `urls` = `{loc: lastmod}` (lastmod exactly as the site wrote it, or null) |
| `skipped_sitemaps` | Child sitemaps not read because of `max_child_sitemaps` |
| `limits` | `max_child_sitemaps`, `max_urls` |

## crawl.json

| Field | Meaning |
|---|---|
| `pages_selected`, `selection_notes` | How many pages were picked and why (e.g. "6545 sitemap URL(s) left out by crawler.sitemap_include/sitemap_exclude") |
| `counts` | `ok`, `failed`, `disallowed`, `skipped`, `http_200`, `redirected`, `noindex` |
| `pages[]` | One entry per page (below) |
| `limits` | `max_pages`, `max_redirects`, `max_links_per_page` |

Each page: `url`, `reason` (why it was picked: homepage / extra / sitemap),
`lastmod`, `status`, `http_status`, `redirect_chain`, `final_url`,
`content_type`, `x_robots_tag`, `noindex`, `noindex_sources`, `error`, and
`seo` (only for HTTP 200 HTML):

| `seo` field | Meaning |
|---|---|
| `title`, `title_count` | First `<title>` text and how many there are |
| `meta_description`, `meta_description_count` | Same for the meta description |
| `h1` | All H1 texts |
| `canonical`, `canonical_raw`, `canonical_count` | Canonical as absolute URL, as written, and how many tags |
| `meta_robots` | Meta robots values |
| `lang` | `<html lang>` |
| `internal_links`, `links_total`, `internal_links_truncated` | Same-host links on the page (input for the link check) |

## links.json

| Field | Meaning |
|---|---|
| `unique_links` | Unique internal links found by the crawl |
| `from_crawl` | Links already fetched by the crawl (result reused) |
| `to_check`, `left_out_by_cap` | Checked today; not checked because of `links.max_links` |
| `counts` | `ok`, `broken`, `http_4xx`, `http_5xx`, `failed`, `disallowed`, `skipped`, `redirected`, `get_fallback`, `checked` |
| `links[]` | `url`, `status`, `http_status`, `final_url`, `redirect_chain`, `method` (HEAD/GET), `linked_from` (up to 20 pages), `linked_from_count`, `source` (crawl or check), `notes`, `error` |
| `notes`, `limits` | e.g. "890 link(s) not checked: over links.max_links (150)" |

## psi.json

| Field | Meaning |
|---|---|
| `endpoint`, `urls`, `strategies`, `requests_made` | What was asked of the PSI API |
| `counts` | `ok`, `failed`, `skipped`, `no_url_field_data` |
| `runs[]` | One per URL x strategy (below) |

Each run: `url`, `strategy` (mobile/desktop), `status`, `http_status`,
`final_url`, `fetched_at`, `duration_s`, `attempts`, `lighthouse_version`,
`run_warnings`, `error`, and:

- `lab`: `performance_score` (0-100), `lcp_ms`, `cls`, `tbt_ms` (Lighthouse,
  one synthetic load).
- `field.url` and `field.origin`: CrUX 28-day real-user data, `metrics` =
  `{lcp_ms, inp_ms, cls: {percentile, category}}` (75th percentile; FAST /
  AVERAGE / SLOW) plus `overall_category`. `field.url` is null when Google
  has no data for the URL; `field.notes` says why.

## findings.json

Made by `src/seo_reviewer/diff.py` from the day's files and earlier days.

| Field | Meaning |
|---|---|
| `status` | `baseline`, `issues` or `stable`; `all_stable` = `status == "stable"` |
| `headline`, `counts` | One-line result; `{critical, warning, info}` |
| `compare_to` | The `--compare-to` day, if given |
| `sources` | Per source: `status`, `error`, `compared_to` (day used), `baseline`, `notes` |
| `findings[]` | Ordered critical, warning, info; regressions first |
| `ongoing` | Issues present on both days, counted per rule (not repeated as findings) |

Each finding: `id` (F001...), `severity`, `source`, `rule` (see
`src/seo_reviewer/rules.py`), `check`, `change` (`appeared`, `resolved`,
`worsened`, `baseline`, `pending`, ...), `regression`, `url` (or null for a
grouped finding), `old_value`, `new_value`, `evidence` (`old -> new`),
`evidence_source` (file and day), `compared_to`, `message`, `count`,
`details` (for grouped findings: `count` and `urls`), `suggested_next_step`
(written by the rules).

## summary.json

| Field | Meaning |
|---|---|
| `source` | `gemini` or `template` |
| `reason` | Why the template was used (e.g. "Gemini timed out after 30 s (tried twice)"), else null |
| `model`, `usage` | Model asked; token counts when Gemini answered |
| `text` | The summary shown in the report |
| `finding_ids`, `findings_sha256`, `findings_generated_at` | Ties the summary to one exact findings.json |
| `validation_errors` | Why a Gemini answer was rejected, if it was |

## delivery.json

| Field | Meaning |
|---|---|
| `channel`, `status` | `telegram`; `sent`, `failed` or `skipped` |
| `http_status`, `message_id`, `attempts`, `plain_text_fallback` | Telegram's answer |
| `text`, `chars` | The exact message sent |
| `report_url` | Link to the full report |
| `summary_source`, `finding_ids`, `findings_total` | What the message was built from |
| `reason` | Why it was not sent, else null |
