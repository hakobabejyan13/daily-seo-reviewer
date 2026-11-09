# Recording script (10 minutes)

Six segments. Times are targets; the "Say" lines are a guide, not a script
to read word for word. Every number below is copied from the files of
2026-11-08 and 2026-11-09 in this repo.

**History at recording time: 2 days.**
- **DAY_BASE = 2026-11-08**: the first run, a baseline (nothing to compare
  with). Manual run 42528201040.
- **DAY_SHOW = 2026-11-09**: the first day-over-day comparison. Manual run
  42571863547 (started 06:14 UTC; GitHub never created the 05:00 UTC
  scheduled run that morning).

The plan asked for 4+ days. With 2 days the video can show one baseline and
one real comparison, but not a trend, a confirmed regression or a resolved
issue. Say that once, plainly (segment 2), and do not suggest more history
than there is.

**Before recording**
- Open tabs: the repo on GitHub; Actions -> daily-review (run 42571863547);
  `reports/2026-11-09.md`; `reports/2026-11-08.md`;
  `data/snapshots/2026-11-09/findings.json` and `psi.json` (raw view);
  the Telegram chat with the bot (messages for 2026-11-08 and 2026-11-09);
  `config.yaml`; `docs/limitations.md`.
- Optional: start one more manual run (Actions -> daily-review -> Run
  workflow, send_telegram ticked) about 6 minutes before segment 1 to show
  it live. Note it replaces that day's snapshot and report if it is still
  the same UTC day.
- Close anything that shows secrets (Settings -> Secrets shows names only,
  which is fine).

---

## 1. The run (0:00 - 1:30)

**Show:** Actions -> daily-review, then run 42571863547.

**Say:**
- "This is a daily SEO reviewer for one website, here the Cloudflare Blog.
  It is scheduled on GitHub Actions every morning at 9:00 Yerevan time,
  05:00 UTC, and it can also be started by hand."
- Be honest about the runs shown: "Both runs so far were started by hand.
  The first scheduled run, this morning, did not deliver, so I ran it
  manually at 06:14 UTC. GitHub never created the scheduled run: the
  workflow was enabled, but GitHub says schedules at the top of the hour
  can be delayed or dropped when it is busy."
  (If the cron has been moved off the full hour by the time you record,
  add: "so the schedule now runs at a quieter minute.")
- "Each run: unit tests, then it collects data from the site and Google's
  PageSpeed API, compares with the previous day, writes a report, sends a
  short Telegram message, and commits the data and report back to the repo."
- Expand the review step: the log lines for robots, sitemap, crawl, links
  and PSI, and "Summary written ...: Gemini gemini-3.5-flash-lite".
  Collection ran from 06:15:10 to 06:18:40 UTC, 211 requests to the site
  and 2 to the PageSpeed API.
- Show the commit list: `report: 2026-11-08` and `report: 2026-11-09` by
  github-actions[bot].

## 2. One review: 2026-11-09 (1:30 - 3:00)

**Show:** `reports/2026-11-09.md` on GitHub.

**Say:**
- "We have two days of history, so this is the first real comparison:
  2026-11-09 against 2026-11-08."
- Headline: "All stable: no critical or warning changes since 2026-11-08,
  1 info."
- Summary: "This part is written by Gemini, and it says so. A validator
  checked that every URL, number and finding id in it is in findings.json."
  Point at the line "F001 (info, regression, psi): lab LCP got slower on
  https://blog.cloudflare.com/ with evidence 2251 ms -> 2701 ms."
- The one finding, F001: "Mobile lab LCP on the homepage went from 2251 ms
  to 2701 ms. That is more than the 300 ms tolerance, but lab data is
  noisy, so it is only info: possible regression, awaiting confirmation,
  1 of 2. It becomes a warning only if tomorrow's run is slower too."
- Known issues: "The three issues from the first day are still there, 50,
  38 and 39 pages. They are not repeated as new findings; they are counted
  as unchanged."
- Briefly open `reports/2026-11-08.md`: "The first day was a baseline: no
  history, so it listed the current issues, not changes. Its summary is the
  template, because Gemini timed out that day."

## 3. The logic (3:00 - 5:00)

**Show:** `docs/how-it-works.md` (pipeline diagram), then
`src/seo_reviewer/rules.py`, then `data/snapshots/2026-11-09/findings.json`
and `psi.json`.

**Say:**
- "Five steps: collect, diff, summary, report, deliver. Each step only reads
  the files of the step before."
- "The diff compares each source with the last day it worked. Only changes
  become findings. Issues that were already there are counted as ongoing,
  so a quiet day is a short 'all stable' message."
- rules.py: "Every rule has a fixed severity and a next step. Critical:
  page errors, noindex, missing canonical, robots blocking. Warning: broken
  links, redirects, confirmed speed drops. Info: everything else.
  Severities can be changed in config."
- Use the real PSI numbers from psi.json (mobile, homepage):
  performance score 99 -> 96, TBT 44 ms -> 0 ms, LCP 2251 ms -> 2701 ms.
  "The score dropped 3 points, below the 10-point threshold, so it is
  ignored. TBT got better. LCP rose by more than the 300 ms tolerance, so
  it is the one change reported, and only as 'awaiting confirmation'."
- In findings.json, point at F001: `"change": "pending"`, `"streak": 1`,
  `"confirmations_needed": 2`, and `suggested_next_step`: "No action yet:
  reported as a regression only if the next 1 run(s) are also worse."
  "The next step is written by the rules, not by the AI."
- "The AI only summarizes this file. If its answer has a URL, number or id
  that is not in the file, or Gemini is down, the template is used instead,
  like on 2026-11-08."

## 4. Build, tools and data (5:00 - 7:30)

**Show:** the repo tree, `PLAN.md`, `DECISIONS.md`, `config.yaml`,
`requirements.txt`, `data/README.md`, `docs/data-sources.md`, the "Unit
tests" step in the Actions run.

**Say:**
- "It was built in 11 tasks from an approved plan, one task per session.
  Every tool choice is logged in DECISIONS.md."
- Tools: "Python 3.12 and four libraries: requests, BeautifulSoup with lxml,
  PyYAML, plus pytest. No SDKs: Gemini and Telegram are plain HTTPS calls."
- config.yaml: "Nothing is hard-coded to this site. The site, caps,
  thresholds, model and delivery are all here. Switching sites is a config
  change."
- Data: open `data/snapshots/2026-11-09/`. "One folder per day: robots.txt,
  the sitemap with 10,236 URLs, the crawl of the homepage and the 50 newest
  posts, 150 internal links checked, and PageSpeed lab and real-user data
  for the homepage. Then the findings, summary and delivery records. Two
  folders so far, one per day."
- "The crawler is polite: it obeys robots.txt, sends one request a second
  with a named User-Agent, and stops at a 5-minute budget."
- "Keys are only in GitHub Secrets; nothing is logged."
- Tests: "Unit tests with no network run before every review."

## 5. Delivery (7:30 - 8:30)

**Show:** the Telegram chat: the 2026-11-09 message, tap "Full report".
Then scroll up to the 2026-11-08 message.

**Say:**
- 2026-11-09: "On an all-stable day the message is two lines: the status,
  'compared with 2026-11-08 (1 info)', and one line of source health with
  the link to the full report. 384 characters." The info finding is in the
  report, not in the message.
- 2026-11-08: "On a day with findings, the message lists up to five of them
  with the evidence and the next step."
- "If the run itself fails, a separate alert names the failed step and links
  the run. If Telegram fails, the report is still saved and committed."

## 6. Limitations (8:30 - 10:00)

**Show:** `docs/limitations.md`.

**Say:**
- "History is short: two days. So far it shows one baseline and one
  comparison, with one unconfirmed speed change. The confirmation logic and
  the 'resolved' path are covered by unit tests, not yet by real days."
- "It reads server HTML only, no JavaScript rendering."
- "It checks a sample: the homepage and 50 newest posts, and the same 150
  links each day. No Search Console data, so no rankings or traffic."
- "PageSpeed lab data is noisy and only covers the homepage; real-user data
  moves slowly."
- "One site per config. robots.txt is parsed with Python's parser, which
  uses first-match order, not Google's longest match."
- "Gemini's free tier was often overloaded while building. It wrote the
  2026-11-09 summary; if it is down, the template is used and the review is
  still complete."
- Close: "With more time: rotating samples, Search Console, JS rendering for
  key pages, and a second delivery channel."

---

**Checklist after recording:** no secret visible on screen (token, chat id,
API keys); the runs shown were green; the Telegram message and report shown
are for the same day; nothing said implies more than 2 days of history.
