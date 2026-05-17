---
name: contract-monitor-gov-uk
description: Monitor UK Contracts Finder (contractsfinder.service.gov.uk) OCDS award releases for newly-published contract awards where the supplier matches any company on the cross-sector master watchlist (Auto, IT, Defence, Pharma, Energy). For each match, predict the next-trading-day stock price move via OpenRouter LLM and email an alert to NOTIFY_EMAIL_TO when |predicted_pct| > 3%. Designed to run as a scheduled remote routine daily at 08:00 UTC after the overnight UK gov ETL.
---

# Contract Monitor — gov.uk (Contracts Finder)

Pages the **public** OCDS search endpoint at `contractsfinder.service.gov.uk` filtered to `stages=award`, scans every release for a `suppliers[].name` that matches the master watchlist, and processes hits through the shared pipeline.

## When to run

- **Scheduled:** daily at 08:00 UTC Mon–Fri (UK gov ETL completes overnight, daytime publications also captured by next-morning sweep).
- **Ad-hoc:** any time.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENROUTER_API_KEY` | yes | — | LLM prediction |
| `RESEND_API_KEY` | yes | — | Email alerts |
| `NOTIFY_EMAIL_TO` | yes | — | Destination |
| `NOTIFY_EMAIL_FROM` | no | `Contract Routine <onboarding@resend.dev>` | |
| `OPENROUTER_MODEL` | no | `perplexity/sonar` (then anthropic fallback) | |

Contracts Finder OCDS feed is **public** — no key required.

## Data source

| Source | Latency | Auth |
|---|---|---|
| `contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?stages=award` | minutes-to-hours | none |

Pagination is cursor-based; the script follows `links.next` until exhausted or the soft page cap is hit.

## Alert logic

Identical to other `contract-monitor-*` skills (see `skills/_shared/runner.py`):
- Match `release.awards[].suppliers[].name` against `aliases` on word boundary.
- Convert award value to USD using `fx_to_usd` (GBP default 1.27).
- Drop below `min_contract_value_usd` ($1M default).
- Dedup keyed by `gov.uk:<release.ocid>` using `release.date` as last-modified anchor.
- Email if `|predicted_pct| > 3.0`.

## How to invoke

```bash
cd skills/contract-monitor-gov-uk
pip install -q -r requirements.txt
python scripts/run.py            # default 2-day lookback
python scripts/run.py 7          # 7-day lookback (backtest)
```

## Output

- **Email** per qualifying alert.
- **`state/log.jsonl`** — append-only.
- **`state/seen.json`** — dedup keyed by `gov.uk:<ocid>`.

## Notes

- Contracts Finder covers below-threshold contracts (under £139k central gov, £213k public sector services). Above-threshold high-value contracts now live on the **Find a Tender Service** (FTS), which moved to OAuth-only access. If/when FTS exposes a public OCDS feed again, add it as a secondary fetcher in this skill.
- Most watchlist matches will be very small (£20k–£500k) — they'll be filtered by `min_contract_value_usd: 1000000`. Mega-cap IT (AWS, Microsoft, Google) is the most likely real hit.
