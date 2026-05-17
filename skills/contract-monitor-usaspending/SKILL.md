---
name: contract-monitor-usaspending
description: Monitor USASpending.gov for new (and materially-updated) federal contract awards to any company on the cross-sector master watchlist (Auto, IT, Defence, Pharma, Energy). For each match, predict the next-trading-day stock price move via OpenRouter LLM and email an alert to NOTIFY_EMAIL_TO when |predicted_pct| > 3%. Designed to run as a scheduled remote routine daily at 22:00 UTC after USASpending's nightly DATA-Act ingest.
---

# Contract Monitor — USASpending.gov

Queries the public USASpending.gov REST API for federal contract awards (HHS, BARDA, DoD, DoE, GSA, NASA, etc.) whose **recipient name** matches any company in `skills/_shared/master_watchlist.yaml`. New + materially-updated awards are predicted via LLM and emailed when the predicted move exceeds the 3% threshold.

## When to run

- **Scheduled:** daily at 22:00 UTC (after USASpending's nightly DATA-Act ingest completes).
- **Ad-hoc:** any time.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENROUTER_API_KEY` | yes | — | LLM prediction |
| `RESEND_API_KEY` | yes | — | Email alerts |
| `NOTIFY_EMAIL_TO` | yes | — | Destination |
| `NOTIFY_EMAIL_FROM` | no | `Contract Routine <onboarding@resend.dev>` | |
| `OPENROUTER_MODEL` | no | `perplexity/sonar` (then anthropic fallback) | |

USASpending and yfinance need no key.

## Data source

| Source | Latency | Auth |
|---|---|---|
| `api.usaspending.gov/api/v2/search/spending_by_award/` | nightly batch (~1 day) — but the actual contract signing date can lag a press release by 1–4 weeks | none |

## Alert logic (shared with all `contract-monitor-*` skills)

1. Fetch awards from the last 2 days (by `last_modified_date`).
2. Match recipient name against `aliases` on a word boundary.
3. Drop below `min_contract_value_usd` ($1M default).
4. **Dedup vs `state/seen.json`:**
   - Not seen → `new`
   - Seen + `last_modified_date` changed OR amount changed > 25% → `update`
   - Otherwise → `duplicate` (skip)
5. LLM (`perplexity/sonar` → anthropic fallback) predicts signed % move.
6. **Email if `|predicted_pct| > 3.0`**; subject distinguishes `[NEW]` vs `[UPDATE]`.
7. Log everything to `state/log.jsonl`; record id in `state/seen.json`.

## How to invoke

```bash
cd skills/contract-monitor-usaspending
pip install -q -r requirements.txt
python scripts/run.py            # daily lookback (2 days)
python scripts/run.py 7          # 7-day lookback (backtest)
```

Exit code 0 on success (regardless of whether any alerts fired). Non-zero only on hard fetch failure.

## Output

- **Email** per qualifying alert.
- **`state/log.jsonl`** — append-only log; one JSON object per evaluated contract with `decision ∈ {alert, below_alert_threshold, duplicate, below_min_value, no_price_data, error}`.
- **`state/seen.json`** — dedup store keyed by `USASpending:<award_id>`.

## Tuning

All thresholds live in `skills/_shared/master_watchlist.yaml`:
- `predicted_pct_alert: 3.0` — email threshold (per spec).
- `min_contract_value_usd: 1000000` — fetch-time floor.
- `update_material_value_delta: 0.25` — re-alert sensitivity on existing contracts.
