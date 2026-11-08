# Known limitations

This file lists what the reviewer cannot do or does only partly. Task 11 completes it.

## LLM summary (Gemini free tier)

- The summary at the top of the report comes from Google Gemini (model set in `config.yaml`, `llm.model`) on the free tier. The free tier can be overloaded: on 2026-11-08, `gemini-3.8-flash` answered `HTTP 503 "This model is currently experiencing high demand"` or did not answer within 60 s, both from GitHub Actions and from a local PC (503 after 17.8 s for a one-line prompt).
- This does not break the review. On HTTP 429, 5xx, a timeout, or an empty, blocked, cut-off or invalid answer, the report uses the deterministic **template summary** (headline, failed sources, top findings). The Summary section says "Template summary (no AI). Gemini not used: REASON", so the reader always knows which one they got. The findings, severities, evidence and next steps never depend on Gemini.
- A 5xx or a timeout is retried once. The call must fit in what is left of `runtime.budget_seconds` (5 minutes for the whole run). Collection takes about 210 s, which leaves about 90 s for Gemini.
- Google retires models for new API keys: `gemini-2.5-flash` returned HTTP 404 "no longer available to new users" on 2026-11-08. A retired model also falls back to the template. The fix is to change `llm.model` in `config.yaml`.
- The validator checks that every URL, number and finding id in the Gemini text appears in findings.json. It does not check meaning: Gemini could still describe a finding wrongly in words. Numbers written as words are not checked.
- On the free tier, Google may use prompts to improve its products. The prompt holds only findings about a public website.
