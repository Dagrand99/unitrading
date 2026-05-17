---
name: contract-monitor-ted-eu
description: Monitor TED (Tenders Electronic Daily, ted.europa.eu) for newly-published EU contract-award notices whose winner matches any company on the cross-sector master watchlist (Auto, IT, Defence, Pharma, Energy). For each match, predict the next-trading-day stock price move via OpenRouter LLM and email an alert to NOTIFY_EMAIL_TO when |predicted_pct| > 3%. Designed to run as a scheduled remote routine daily at 06:30 UTC (≈08:30 CET) shortly after TED publishes its daily batch.
---

# Contract Monitor — TED (EU)

Queries the public TED v3 search API for `notice-type=can-standard` (Contract Award Notice — standard) where the `winner-name` matches a company on `skills/_shared/master_watchlist.yaml`.

## When to run

- **Scheduled:** daily at 06:30 UTC Mon–Fri (TED publishes daily batch around 08:00 CET ≈ 06:00–07:00 UTC).
- **Ad-hoc:** any time.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENROUTER_API_KEY` | yes | — | LLM prediction |
| `RESEND_API_KEY` | yes | — | Email alerts |
| `NOTIFY_EMAIL_TO` | yes | — | Destination |
| `NOTIFY_EMAIL_FROM` | no | `Contract Routine <onboarding@resend.dev>` | |
| `OPENROUTER_MODEL` | no | `perplexity/sonar` (then anthropic fallback) | |

TED and yfinance need no key.

## Data source

| Source | Latency | Auth |
|---|---|---|
| `api.ted.europa.eu/v3/notices/search` | daily batch ~08:00 CET | none |

## Alert logic

Identical to all `contract-monitor-*` skills — see `skills/_shared/runner.py`. Key thresholds in `skills/_shared/master_watchlist.yaml`:
- Alert when `|predicted_pct| > 3.0`
- New vs Update via `publication-number` + `publication-date`

TED rarely modifies an existing notice — instead it publishes a *corrigendum* with a new publication number. The dedup store will treat a corrigendum as a `new` event; the LLM rationale typically notes the relationship to a prior award.

## How to invoke

```bash
cd skills/contract-monitor-ted-eu
pip install -q -r requirements.txt
python scripts/run.py            # default 2-day lookback
python scripts/run.py 7          # 7-day lookback (backtest)
```

## Output

- **Email** per qualifying alert (subject tagged `[Contract NEW · <sector>]`).
- **`state/log.jsonl`** — append-only log.
- **`state/seen.json`** — dedup keyed by `TED:<publication-number>`.
