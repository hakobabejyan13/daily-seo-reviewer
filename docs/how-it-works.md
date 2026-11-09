# How it works

One run = one day. The `daily-review` workflow starts it every morning; the
same pipeline runs locally with `PYTHONPATH=src python -m seo_reviewer.main`.

```
config.yaml
    |
    v
1. collect  robots.txt -> sitemap -> crawl -> links      (target site, 1 req/s)
            PageSpeed Insights (background thread)       (Google API)
            => data/snapshots/DAY/{meta,robots,sitemap,crawl,links,psi}.json
    |
2. diff     today vs. the last day each source was ok    (no network)
            + severity rules                             => findings.json
    |
3. summary  Gemini summary of findings.json, validated   => summary.json
            (or the deterministic template)
    |
4. report   markdown report                              => reports/DAY.md
    |
5. deliver  short Telegram message + report link         => delivery.json
    |
6. commit   workflow commits data/ and reports/ as "report: DAY"
```

Steps 2 to 5 only read files written by the step before. Each can be re-run
for an existing day without the network: `--diff-only`, `--report-only`,
`--deliver-only` (Telegram only), and `--no-deliver` to build the message
without sending it.

The code is generic: the site, caps, thresholds, model and delivery channel
are all in `config.yaml`. Switching sites means editing `site:` (and the
crawl filters) only.

## 1. Collect

Source code: `src/seo_reviewer/collectors/`. Details per source:
[data-sources.md](data-sources.md).

- Order: robots.txt first (the HTTP client needs it), then the sitemap, the
  crawl (picks the newest sitemap URLs) and the link check (uses the links
  the crawl found). PSI runs in parallel because it only waits on Google.
- Each source writes its own record with `status` = `ok`, `partial` (some
  data, with `partial_reasons`) or `failed` (with `error`). A failed source
  never stops the run; `meta.json` lists the status of every source.
- Budget: 5 minutes for collection (`runtime.budget_seconds`). Requests stop
  starting when it runs out and the source is `partial`. A PSI call only
  starts if it can finish in time.
- Retention: day folders older than 90 days (`storage.retention_days`) are
  deleted.

## 2. Diff and severity rules

Source code: `src/seo_reviewer/diff.py` (comparison) and
`src/seo_reviewer/rules.py` (one entry per rule: severity, check name,
suggested next step). Deterministic: same snapshots in, same findings out.

**What is compared with what.** Each source is compared with the last earlier
day on which that source was `ok` (or with `--compare-to DAY`). If there is
no such day, the source is a **baseline**: its current issues are listed
with change `baseline` and the report says these are not changes.

**Source health first.**
- A failed source gives exactly one finding, `SOURCE failed: ERROR`, and no
  per-URL findings for it. robots.txt, sitemap and crawl failures are
  **critical** (site data); links and PSI failures are **warning**.
- If a source failed because the one it depends on failed (crawl needs
  robots.txt, links need the crawl), it is one **info** "not run" finding, so
  one root cause is reported once.
- `partial` = one info finding with the reasons, then a like-for-like compare
  of what was collected. A source that works again = info "recovered".

**What the checks look at** (rule ids in brackets):

| Area | Critical | Warning | Info |
|---|---|---|---|
| robots.txt | key URL now blocked (`robots_blocks_url`), mode changed to disallow all (`robots_mode_changed`) | | declared sitemaps changed |
| Sitemap | | more than 10% of URLs removed in a day (`sitemap_urls_dropped`) | URLs added / removed, lastmod changed |
| Pages | HTTP 4xx/5xx, noindex, canonical missing | fetch failed, blocked by robots, redirects, canonical elsewhere / changed / multiple / broken target, title / description / H1 missing | title or description length out of range, duplicates, H1 multiple, title / description / H1 changed, sitemap vs canonical slash mismatch |
| Links | | broken internal link (`link_broken`) | link now redirects |
| PSI lab | | score dropped >= 10 points, LCP +300 ms, CLS +0.02, TBT +100 ms (confirmed) | possible regression, awaiting confirmation; cleared |
| CrUX field | category went from FAST to SLOW (`crux_poor`) | category got worse; metric is SLOW on the baseline day | category improved; AVERAGE on the baseline day; data appeared / disappeared |

Thresholds come from `config.yaml` (`thresholds:`): title 30-60
characters, meta description 70-160, PSI drop 10 points, lab tolerances.
Any rule's severity can be changed with `rules.severity_overrides`.

**Rules that keep the review short:**
- **Only changes are findings.** An issue that existed on both days is not
  repeated; it is counted under `ongoing` (count per rule). So a quiet day is
  "all stable".
- **Good news is always info** ("resolved", "recovered", "improved",
  "cleared").
- **Grouping.** More than 5 findings with the same rule and change
  (`rules.group_min`) become one grouped finding with the URL list (capped
  at 50).
- **Trailing slash.** `/x` and `/x/` count as the same URL
  (`rules.trailing_slash_equivalent`). The Cloudflare sitemap lists `/x/`
  while canonicals say `/x`; that is one grouped info finding, not 50
  "canonical points elsewhere" warnings.
- **PSI noise.** A lab metric regression must repeat on 2 runs in a row
  (`psi.regression_confirmations`). The first one is info "possible
  regression, awaiting confirmation (1 of 2)"; if the next run is fine it is
  "cleared". CrUX is compared directly by category, since it is already a
  28-day average.

**Each finding has** an id (`F001`...), severity, source, rule, the affected
URL (or a list), the evidence (`old -> new` with the day it came from), and a
suggested next step written by the rules, never by the LLM.

**Order:** critical, then warning, then info; inside a severity, regressions
first. The file's `status` is `baseline`, `issues` (at least one critical or
warning) or `stable`.

## 3. Summary

Source code: `src/seo_reviewer/summarize.py`.

The LLM only rewords what the diff decided. Gemini gets a compact copy of
`findings.json` (at most 40 findings, 3 example URLs each) and fixed
instructions: summarize only these findings, regressions first, name them by
id, use only URLs and numbers from the input, no next steps, at most 5 lines.

**Validator.** Every answer is checked before it is used:
- not empty, not cut off at `max_output_tokens`, not blocked;
- every URL in the text appears in the input (trailing slash allowed);
- every number in the text appears in the input;
- every finding id (`F001`...) exists.

If any check fails, or Gemini is off, has no key, returns 429 / 5xx, or times
out, the **template summary** is used: the headline, failed sources and the
top 3 findings, straight from `findings.json`. `summary.json` records which
one was used (`source: gemini | template`) and why (`reason`). A 5xx or
timeout is retried once; a 429 is not (free-tier quota). The summary has its
own 180 s window after the diff (`llm.budget_seconds`), 90 s per call.

## 4. Report

Source code: `src/seo_reviewer/report.py`. Output: `reports/YYYY-MM-DD.md`.

- Header: status, site, compared-with day, counts, run time and request
  counts.
- Summary: the Gemini text (marked as AI) or the template, with the reason.
- Source health table: status, compared-with day and coverage of every
  source. A failed source is named at the top with what was not compared.
- Findings by severity with what / evidence / affected URLs / next step.
- Three modes: **baseline** (first run, current issues), **issues** (full
  list), **stable** (short: headline, one line of source health, info
  findings as one-liners).

The report never computes a metric; it only lays out `findings.json`.

## 5. Delivery

Source code: `src/seo_reviewer/deliver/telegram.py`.

One HTML message per run: header (site, date, status), counts, the AI
summary or the word "template", up to 5 findings (severity, URL, old -> new,
next step), one line per failed source, a sources line and a "Full report"
link to `reports/DAY.md` on GitHub. All stable = 2 lines.

5xx and timeouts are retried once; a 429 only if Telegram asks to wait 30 s
or less; an HTML parse error is retried as plain text. A Telegram failure
never loses the report and never fails the run: it is written to
`delivery.json` and shown as a warning in the workflow.

## 6. The workflow

File: `.github/workflows/daily-review.yml`.

- **When:** cron `0 5 * * *` = 05:00 UTC = 09:00 Asia/Yerevan (GitHub may
  start it 5-30 minutes late), plus a manual "Run workflow" button with
  `compare_to` and `send_telegram` inputs.
- **Steps:** checkout, Python 3.12, install pinned requirements, unit tests
  (no network), the review, the report printed to the run summary, a
  delivery warning if Telegram failed, then commit-back.
- **Commit-back:** only after a successful review. `git add data/ reports/`,
  commit `report: DAY` as `github-actions[bot]`, push (3 tries with
  `git pull --rebase`). Nothing changed = nothing committed.
- **Failure alert:** if the tests, the review or the push fail, a short
  Telegram message names the failed step and links the run. No secrets in
  it.
- A failed run commits nothing for that day; the next run compares with the
  last good day.

The other workflow, `crawl-check.yml`, is a manual dry run of the same
pipeline (Telegram off by default) used while building.

## Tools

All approved in PLAN.md section 5; reasons are logged in `DECISIONS.md`.

| Tool | Used for |
|---|---|
| Python 3.12 | The whole tool |
| `requests` | Every HTTP call (site, PSI, Gemini, Telegram) |
| `beautifulsoup4` + `lxml` | HTML and sitemap XML parsing |
| `urllib.robotparser` | robots.txt rules (standard library) |
| `PyYAML` | `config.yaml` |
| `pytest` (dev) | 272 unit tests, run before every review |
| GitHub Actions | Scheduler, runner and storage (commits) |
| Google PSI API, Gemini API, Telegram Bot API | Data, summary, delivery |
