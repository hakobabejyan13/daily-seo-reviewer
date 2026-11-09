# Known limitations

What the reviewer cannot do, or does only partly, and what would come next
with more time. Values are from `config.yaml` as of 2026-11-08.

## What it sees of the site

- **No JavaScript rendering.** The crawl reads the server HTML only. A title,
  canonical, meta robots tag or link added by JavaScript is not seen, and a
  page that renders its content in the browser looks empty. The Cloudflare
  Blog serves full HTML, so this does not matter for it today.
  *With more time:* an optional headless-browser crawl (Playwright) for a few
  key pages, compared with the server HTML.
- **A sample of 50 pages, not the whole site.** Each day: the homepage plus
  the 50 newest sitemap URLs by lastmod (`crawler.max_pages`), after
  dropping tag, author and translated pages. That is about 0.5% of the
  10,236 sitemap URLs. An old post that breaks without a new lastmod is not
  seen. *With more time:* a rotating sample (a different slice of old pages
  every day) on top of the newest posts, and `crawler.extra_urls` for the
  pages that matter most.
- **Link check covers the same ~150 links every day.** Of 1,043 internal
  links on 2026-11-08, 150 were checked (`links.max_links`) and 890 were left
  out. Links are ordered most-linked first, so the same ones are picked
  daily and the long tail is never checked. External links are not checked.
  *With more time:* rotate through the rest of the list across days.
- **No Search Console data.** No impressions, clicks, positions, index
  coverage or Google's own crawl errors. Those need site ownership and OAuth.
  The review only says what changed on the site, not how rankings or
  traffic moved. *With more time:* a Search Console collector for a site
  we own.
- **Single site.** One site per config file. Switching sites is a config
  change only, but checking several sites needs one run per config.

## Measurement noise

- **PSI lab data is noisy.** Lighthouse runs one synthetic page load; scores
  and LCP move between runs without any site change (on 2026-11-08 both
  runs had the Lighthouse warning "The page loaded too slowly to finish
  within the time limit"). The tool ignores changes inside
  `thresholds.lab_tolerance` (LCP 300 ms, CLS 0.02, TBT 100 ms, score 10
  points) and needs 2 regressed runs in a row before a warning. A real
  slowdown is therefore reported a day late, and a small one not at all.
- **PSI covers the homepage only** (`psi.urls: []`), mobile and desktop.
  More URLs cost ~55 s each inside the 5-minute budget.
- **CrUX moves slowly.** Field data is a 28-day rolling 75th percentile, so a
  regression shows up gradually, and only for URLs with enough Chrome
  traffic.
- **Length thresholds are noisy for this site.** Title 30-60 and description
  70-160 characters flag 39 and 38 of the 51 pages (info only, then counted
  as ongoing). Widen `thresholds` in `config.yaml` if the team does not care.
- **Day-over-day only.** Each source is compared with its last ok day, not a
  long trend. Slow drifts below a threshold are not reported.

## robots.txt parsing

- Python's `urllib.robotparser` applies the **first matching rule** in a
  user-agent group. Google applies the **longest (most specific) match**, and
  `Allow` wins a tie. A robots.txt like `Disallow: /blog/` followed by
  `Allow: /blog/public/` is read differently: Google allows
  `/blog/public/x`, `robotparser` blocks it. `robotparser` also does not
  support `*` and `$` wildcards inside paths the way Google does. The
  Cloudflare Blog's robots.txt is simple, so it makes no difference today,
  but on another site the "blocked by robots.txt" findings could differ from
  Google's view. *With more time:* a parser that follows Google's rules
  (RFC 9309).

## Gemini availability

- The summary at the top of the review comes from Google Gemini on the free
  tier (`llm.model: gemini-3.5-flash-lite`). It works now: crawl-check run
  42533456015 (2026-11-08 23:09 UTC) got a validated Gemini summary in about
  1 s (665 prompt tokens, 118 output tokens).
- Before that, the free tier was often unavailable. On 2026-11-08
  `gemini-3.8-flash` answered once; every other call ended in
  `HTTP 503 "This model is currently experiencing high demand"` or a timeout,
  even with two full 90 s attempts, from GitHub Actions and from a local PC.
  That is why the model was switched to Flash-Lite. It can happen again on
  any day.
- **This never breaks the review.** On HTTP 429, 5xx, a timeout, or an empty,
  blocked, cut-off or invalid answer, the report uses the deterministic
  template summary and says "Template summary (no AI). Gemini not used:
  REASON". Findings, severities, evidence and next steps never depend on
  Gemini.
- Time: one retry on 5xx or a timeout; 90 s per call
  (`llm.timeout_seconds`) inside a 180 s window of its own after the diff
  (`llm.budget_seconds`). No retry on 429 (free-tier quota).
- Google retires models for new API keys: `gemini-2.5-flash` returned
  HTTP 404 "no longer available to new users" on 2026-11-08. A retired model
  also falls back to the template; the fix is a new `llm.model` in
  `config.yaml`.
- The validator checks that every URL, number and finding id in the Gemini
  text is in findings.json. It does not check meaning: Gemini could still
  describe a finding wrongly in words. Numbers written as words are not
  checked.
- On the free tier, Google may use prompts to improve its products (see
  [data-sources.md](data-sources.md#google-gemini-summary-only)). The prompt
  holds only findings about a public website.

## Delivery and storage

- **Telegram only.** One channel, at most 5 findings per message; the rest
  are in the linked report. A Telegram failure is a workflow warning, not a
  failed run.
- **Cron timing.** GitHub may start scheduled runs 5-30 minutes late. In a
  public repository GitHub also turns schedules off after 60 days without
  repository activity; the workflow then has to be re-enabled by hand.
- **A failed run commits nothing** that day; the next run compares with the
  last good day, so one day's changes are merged into the next review.
- **Repo growth.** About 3.1 MB of JSON per day (mostly crawl.json and
  sitemap.json), kept for 90 days. Git compresses unchanged parts, but the
  history keeps every day. *With more time:* gzip the big files or store
  snapshots outside git.
- **Re-runs overwrite the day.** Running twice on the same UTC day replaces
  that day's snapshot and report.
