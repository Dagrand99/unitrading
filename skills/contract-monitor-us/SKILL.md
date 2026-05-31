---
name: contract-monitor-us
description: Monitor US federal procurement (SAM.gov Award Notices + USASpending.gov contract awards) for new and materially-updated contracts to any company on the cross-sector master watchlist (Auto, IT, Defence, Pharma, Energy). For each match, predict the next-trading-day stock price move via OpenRouter LLM and email an alert to NOTIFY_EMAIL_TO when |predicted_pct| > 3%. Designed to run as a scheduled remote routine daily at 22:00 UTC after USASpending's nightly DATA-Act ingest.
---

# Contract Monitor — US (SAM.gov + USASpending.gov)

Combines two complementary US procurement feeds in one routine:

| Sub-source | Latency | Hit profile | Auth |
|---|---|---|---|
| **SAM.gov** Opportunities v2 — `ptype=a` Award Notices | real-time (publication) | sparse, clean, same-day | `SAM_API_KEY` (free, sam.gov registration) |
| **USASpending.gov** Search Awards | nightly DATA-Act ingest | comprehensive, ~1–4 week lag | none |

Both pass results through the same shared pipeline (`skills/_shared/runner.py`):
- match recipient/awardee against `aliases` on word boundary
- dedup vs `state/seen.json` (keys namespaced per sub-source — same award appearing in both feeds is treated independently)
- LLM prediction via OpenRouter (`perplexity/sonar` → anthropic fallback)
- email when `|predicted_pct| > 3.0`
- log every evaluated record to `state/log.jsonl` with `source ∈ {"SAM.gov", "USASpending"}`

## When to run

- **Scheduled:** daily at 22:00 UTC Mon–Fri (after USASpending's nightly DATA-Act ingest; also captures any SAM.gov publications from the US business day).
- **Ad-hoc:** any time.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENROUTER_API_KEY` | yes | — | LLM prediction |
| `RESEND_API_KEY` | yes | — | Email alerts |
| `NOTIFY_EMAIL_TO` | yes | — | Destination |
| `SAM_API_KEY` | optional* | — | SAM.gov key (free, register at sam.gov → Account Details). **If unset, the SAM.gov sub-routine is silently skipped and USASpending still runs.** |
| `NOTIFY_EMAIL_FROM` | no | `Contract Routine <onboarding@resend.dev>` | |
| `OPENROUTER_MODEL` | no | `perplexity/sonar` (then anthropic fallback) | |

\* Recommended — SAM.gov is the highest-alpha sub-source. USASpending is heavily lagged and most "new" awards on it have already been reported via press release / 8-K.

## How to invoke

```bash
cd skills/contract-monitor-us
pip install -q -r requirements.txt
python scripts/run.py                    # default: USASpending 2-day, SAM.gov 1-day
python scripts/run.py 7 7                # backtest 7-day lookback on both
python scripts/fetch_sam.py 1            # SAM.gov fetcher only (stdout JSON)
python scripts/fetch_usaspending.py 2    # USASpending fetcher only (stdout JSON)
```

Exit code 0 on success. Non-zero only on hard USASpending failure (SAM.gov sub-failures are logged as warnings but don't fail the run).

## Output

- **Email** per qualifying alert; subject tag distinguishes `[Contract NEW · <sector>]` vs `[Contract UPDATE · <sector>]` and which source produced it.
- **`state/log.jsonl`** — append-only; one JSON record per evaluated contract with `source`, `decision ∈ {alert, below_alert_threshold, duplicate, below_min_value, error}`.
- **`state/seen.json`** — dedup keys `SAM.gov:<noticeId>` and `USASpending:<award_id>`.

## Tuning

All thresholds live in `skills/_shared/master_watchlist.yaml`:
- `predicted_pct_alert: 3.0`
- `min_contract_value_usd: 1000000`
- `update_material_value_delta: 0.25`
