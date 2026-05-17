---
name: contract-monitor-sam-gov
description: Monitor SAM.gov (US System for Award Management) for new and modified Award Notices (ptype=a) where the awardee matches any company on the cross-sector master watchlist (Auto, IT, Defence, Pharma, Energy). For each match, predict the next-trading-day stock price move via OpenRouter LLM and email an alert to NOTIFY_EMAIL_TO when |predicted_pct| > 3%. Designed to run as a scheduled remote routine twice daily at 13:00 and 22:00 UTC.
---

# Contract Monitor — SAM.gov

Queries the SAM.gov Opportunities v2 API for Award Notices (`ptype=a`) posted in the last N days and matches each notice's `award.awardee.name` against the master watchlist.

## When to run

- **Scheduled:** twice daily at 13:00 and 22:00 UTC (SAM.gov publishes continuously through the US business day).
- **Ad-hoc:** any time.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SAM_API_KEY` | yes | — | SAM.gov API key (free, registered at sam.gov → Account Details → "Request Public API Key") |
| `OPENROUTER_API_KEY` | yes | — | LLM prediction |
| `RESEND_API_KEY` | yes | — | Email alerts |
| `NOTIFY_EMAIL_TO` | yes | — | Destination |
| `NOTIFY_EMAIL_FROM` | no | `Contract Routine <onboarding@resend.dev>` | |
| `OPENROUTER_MODEL` | no | `perplexity/sonar` (then anthropic fallback) | |

## Data source

| Source | Latency | Auth |
|---|---|---|
| `api.sam.gov/opportunities/v2/search` | real-time at publication | `SAM_API_KEY` query param |

## Alert logic

Identical to all `contract-monitor-*` skills — see `skills/_shared/runner.py`:
- Match `award.awardee.name` against `aliases` with word-boundary regex.
- Drop below `min_contract_value_usd` ($1M default).
- Dedup keyed by `SAM.gov:<noticeId>` (`postedDate` used as the modification anchor since v2 API doesn't expose a separate `modifiedDate`).
- Email if `|predicted_pct| > 3.0`.

## How to invoke

```bash
cd skills/contract-monitor-sam-gov
pip install -q -r requirements.txt
python scripts/run.py            # default 1-day lookback
python scripts/run.py 7          # 7-day lookback (backtest)
```

## Output

- **Email** per qualifying alert.
- **`state/log.jsonl`** — append-only.
- **`state/seen.json`** — dedup keyed by `SAM.gov:<noticeId>`.

## Notes

- SAM.gov `postedFrom`/`postedTo` accept `MM/dd/yyyy` (NOT ISO).
- The free-tier rate limit is 1,000 requests/day per key. The routine does **one** paginated batch request per run (not per company), then post-filters by alias, so it uses ~2 requests/day on the 2x schedule.
- Pre-award solicitations (`ptype=o,p,k,r,g,s,i`) are explicitly excluded — only `ptype=a` (Award Notice) is fetched, because that's where the winner is named.
