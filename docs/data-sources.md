# Data sources

The reviewer uses five data sources. Each one is collected once a day by the
`daily-review` GitHub Actions workflow and saved as one JSON file in
`data/snapshots/YYYY-MM-DD/` (file details: [data/README.md](../data/README.md)).
Every number in a review comes from these files. Nothing comes from outside
knowledge.

All settings named below live in `config.yaml`. The values quoted are the
ones on disk on 2026-11-08 (target: the Cloudflare Blog).

| Source | File | Where it comes from | Requests per day | Needs a key |
|---|---|---|---|---|
| robots.txt | `robots.json` | `https://<site>/robots.txt` | 1 | no |
| Sitemap | `sitemap.json` | `site.sitemap_url` and its child sitemaps | ~10 | no |
| On-page crawl | `crawl.json` | HTML of the homepage + the 50 newest posts | ~51 | no |
| Internal links | `links.json` | Links found by the crawl, checked with HEAD/GET | up to 150 | no |
| PageSpeed Insights | `psi.json` | Google PageSpeed Insights API v5 | 2 | `PSI_API_KEY` |

Two more services are used after collection. They are not data sources:
they only read the findings.

| Service | Used for | Key |
|---|---|---|
| Google Gemini API | The short summary at the top of the review | `GEMINI_API_KEY` |
| Telegram Bot API | Sending the daily message | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

Keys live only in GitHub Secrets (and a local `.env`, which is gitignored).
They are never written to a snapshot, a report or a log line; error texts
are scrubbed of them.

## How the site is contacted

All requests to the target site go through one polite HTTP client
(`src/seo_reviewer/http_client.py`):

- It identifies itself: `User-Agent: daily-seo-reviewer/0.1 (+https://github.com/hakobabejyan13/daily-seo-reviewer)`.
- It obeys robots.txt (`crawler.respect_robots_txt: true`) for every URL and
  every redirect hop. A disallowed URL is recorded as `disallowed` and never
  requested.
- It sends at most 1 request per second (`crawler.requests_per_second`),
  with a 20 s timeout and 1 retry.
- Redirects are followed by hand, one hop at a time, so the whole chain is
  recorded. Redirects to another host are recorded but not followed.
- The whole run has a 5-minute budget (`runtime.budget_seconds: 300`). When
  it is used up, the crawl and link check stop and record `partial`.

A typical day is about 211 requests to the site (2026-11-08: 211).

## 1. robots.txt

- **What:** the site's robots.txt: HTTP status, the parsed rules, the
  sitemaps it declares, and whether the homepage and the sitemap URL are
  allowed for our User-Agent.
- **Where from:** `https://blog.cloudflare.com/robots.txt`, parsed with
  Python's `urllib.robotparser` (standard library).
- **Status rules:** HTTP 200 = parsed; 401/403 = "disallow all"; other 4xx =
  no robots.txt, "allow all"; 5xx or network error = source failed and,
  to be safe, "disallow all" (nothing else is crawled that day).
- **Limits:** only the first 500 KiB is read (Google's limit).
  `urllib.robotparser` applies the first matching rule in a group, not
  Google's longest-match rule. For the current target this makes no
  difference; a site whose robots.txt relies on longest-match order could
  be read wrongly.

## 2. Sitemap

- **What:** every URL listed in the sitemap with its `<lastmod>`, stored per
  child sitemap.
- **Where from:** `site.sitemap_url` (`https://blog.cloudflare.com/sitemap.xml`,
  a sitemap index). Plain and gzipped XML, parsed with `lxml`.
- **Real size:** 10,236 URLs in 9 files on 2026-11-08 (5 post sitemaps,
  authors 1,339, tags 776, archives 21).
- **Limits:** at most 20 child sitemaps (`sitemap.max_child_sitemaps`) and
  15,000 URLs (`sitemap.max_urls`). Child sitemaps are read freshest first,
  so if a cap is hit the newest content is still covered and the record is
  `partial`.

## 3. On-page crawl

- **What:** for each page: HTTP status, redirect chain, final URL,
  `X-Robots-Tag`, `<title>`, meta description, H1s, canonical, meta robots,
  noindex, `lang` and the internal links on the page.
- **Where from:** the server HTML of the site (no JavaScript is run), parsed
  with `beautifulsoup4` + `lxml`.
- **Which pages:** the homepage, `crawler.extra_urls` (none now), then the 50
  newest sitemap URLs by `<lastmod>` (`crawler.max_pages: 50`).
  `crawler.sitemap_exclude` drops `/tag/`, `/author/` and translated
  (`/xx-xx/`) pages, because those get a new lastmod with every post. On
  2026-11-08 this left out 6,545 sitemap URLs; 51 pages were fetched, all
  HTTP 200.
- **Limits:** a sample, not the whole site: about 0.5% of the sitemap each
  day, always the newest posts. Older pages are only checked when they are
  updated. Up to 1,000 links per page are kept.

## 4. Internal links

- **What:** whether each internal link found by the crawl works: final HTTP
  status, redirect chain, which crawled pages link to it.
- **Where from:** the target site, HEAD first (`links.method`), GET (body not
  read) when HEAD fails or returns 4xx/5xx, so servers that handle HEAD badly
  do not show up as broken.
- **Real size:** 1,043 unique internal links on 2026-11-08. 3 were already
  fetched by the crawl (reused, no second request), 150 were checked,
  890 were left out by the cap.
- **Limits:** `links.max_links: 150` per day (1 req/s keeps this to ~2.5
  minutes). Links are ordered most-linked first, so the same ~150 links are
  checked every day; the rest are never checked. External links are not checked
  (`links.check_external: false`).

## 5. PageSpeed Insights (PSI)

- **What:** per URL and strategy (mobile, desktop):
  - Lighthouse **lab** data: performance score (0-100), LCP, CLS, TBT. One
    synthetic page load on Google's servers.
  - **Field** data from the Chrome UX Report (CrUX): 75th percentile of LCP,
    INP and CLS from real Chrome users over the last 28 days, with Google's
    category (FAST / AVERAGE / SLOW), for the URL and for the whole origin.
- **Where from:** `https://www.googleapis.com/pagespeedonline/v5/runPagespeed`
  with the key in `PSI_API_KEY`. This is Google's API, not the target site,
  so robots.txt and the 1 req/s limit do not apply; it runs in a background
  thread at the same time as the crawl.
- **Real values (2026-11-08, homepage):** mobile score 99, LCP 2,251 ms;
  desktop score 100, LCP 601 ms; CrUX FAST for URL and origin on both.
- **Limits:**
  - Only the homepage (`psi.urls: []`). Each call takes ~55 s.
  - Lab data is noisy from run to run, so small changes are ignored
    (`thresholds.lab_tolerance`) and a drop must repeat on 2 runs in a row
    (`psi.regression_confirmations: 2`) before it is a warning.
  - CrUX is a 28-day rolling average: it moves slowly and only exists for
    URLs with enough Chrome traffic. When PSI has no URL data it is stored as
    "no field data", never replaced by origin data.
  - Lighthouse may add run warnings (on 2026-11-08: "The page loaded too
    slowly to finish within the time limit"); they are kept in `psi.json`.
  - Free with an API key; the quota belongs to the Google Cloud project of
    the key. We use 2 calls per day.

## Google Gemini (summary only)

- **What it gets:** a compact copy of `findings.json` (ids, severities,
  checks, evidence, up to 3 example URLs per finding). Never raw HTML or
  snapshot data.
- **What it returns:** at most 5 short lines summarizing those findings. A
  validator checks the text before it is used (every URL, number and
  finding id must be in the data sent; not empty, not cut off, at most
  1,500 characters); otherwise a deterministic template summary is used.
- **Model:** `llm.model: gemini-3.5-flash-lite` (pinned name, never a
  "latest" alias), `thinking_level: low`, 2,048 output tokens max, 90 s per
  call, 180 s for the whole summary step.
- **Free-tier data use:** the project uses the Gemini API free tier. Under
  Google's Gemini API terms, content sent on the free tier may be used by
  Google to improve its products and may be read by human reviewers. That is
  acceptable here because the prompt holds only findings about a public
  website (URLs, titles, lengths, scores). Do not point the tool at a
  private site, or send anything confidential, while on the free tier; a
  paid tier key changes this.
- **Availability:** working since the switch to Flash-Lite (crawl-check run
  42533456015, 2026-11-08: validated summary in ~1 s). The free tier can be
  overloaded on any day; then the template is used and the report says
  why (`summary.json` `reason`). On daily-review, Gemini wrote the
  2026-11-09 summary; on 2026-11-08 it was the template (timed out on
  the older model gemini-3.8-flash).

## Telegram Bot API (delivery only)

- **What it gets:** the short daily message (built from `findings.json`,
  `summary.json` and the snapshot) with a link to the full report on GitHub.
- **Where:** `https://api.telegram.org/bot<token>/sendMessage`, HTML parse
  mode, link previews off.
- **Limits:** a message is capped at 4,096 characters by Telegram, so it
  holds at most 5 findings (`delivery.telegram.max_findings_in_message`) and
  links to the report for the rest. 2026-11-08: 1,546 characters.
