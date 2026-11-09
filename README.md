# daily-seo-reviewer
Daily SEO performance reviewer agent: crawls a site, compares with previous runs, and delivers a prioritized review every morning.

Target now: the Cloudflare Blog (`https://blog.cloudflare.com`). Everything site-specific is in
[`config.yaml`](config.yaml); switching sites is a config change.

## What it does

Every run of `.github/workflows/daily-review.yml` (GitHub Actions):

1. **Collect** five sources into `data/snapshots/YYYY-MM-DD/`: robots.txt, the sitemap, an on-page
   crawl of the homepage + the 50 newest posts, a check of up to 150 internal links, and Google
   PageSpeed Insights (lab + CrUX field data) for the homepage, mobile and desktop.
2. **Diff** against the last day each source worked (`findings.json`). Only changes become findings,
   each with a severity (critical / warning / info), the URL, the evidence (old -> new) and a next
   step written by fixed rules (`src/seo_reviewer/rules.py`). Regressions come first; a quiet day is
   "all stable".
3. **Summary**: Gemini (`gemini-3.5-flash-lite`) writes up to 5 lines from `findings.json` only. A
   validator rejects any URL, number or finding id that is not in the data; then a deterministic
   template is used instead, and the report says why.
4. **Report**: `reports/YYYY-MM-DD.md`.
5. **Deliver**: a short Telegram message with a link to the report.
6. **Commit** `data/` and `reports/` back to the repo as `report: YYYY-MM-DD` (github-actions[bot]).

If the tests, the review or the push fail, a Telegram alert names the failed step.

## When it runs

Every day at **09:00 Asia/Yerevan (05:00 UTC)**. The workflow has no `schedule:` block (GitHub's
scheduler never created a run in this repo); it is started only through `workflow_dispatch`:

- by an external **cron-job.org** job that calls the GitHub API at 09:00 Asia/Yerevan, and
- by hand with the **Run workflow** button (inputs `compare_to`, `send_telegram`).

The GitHub token stored in cron-job.org **expires on 2026-12-09**. After that the daily run stops
**without any alert** (no run starts, so the failure alert cannot fire) until a new token is set in
cron-job.org.

Runs never overlap (`concurrency: daily-review`, queued, not cancelled). A second run on the same UTC
day overwrites that day's files and sends another message; if its push conflicts with the earlier
run's commit, the newer run's files are kept.

## Data

[`data/README.md`](data/README.md) explains every file, where it comes from, and the days in the repo.
[`docs/data-sources.md`](docs/data-sources.md) describes each source and its limits.

## Tests and local runs

```bash
pip install -r requirements.txt
python -m pytest -q
```

272 unit tests, no network (Python 3.12 in Actions; on Windows one symlink test is skipped).

A local run needs the four variables from `.env.example` in the environment (the tool does not read
`.env` itself) and writes into `data/` and `reports/`:

```bash
set -a; . ./.env; set +a
PYTHONPATH=src python -m seo_reviewer.main --config config.yaml --no-deliver
```

Secrets (`PSI_API_KEY`, `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) live only in GitHub
Secrets and a local `.env` (gitignored); they are never written to data, reports or logs.

`.github/workflows/crawl-check.yml` is a manual dry run of the same pipeline (nothing committed,
Telegram off by default).
