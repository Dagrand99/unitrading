---
name: contract-win-us-defense
description: Detect US Department of Defense contract wins for watchlist defense contractors (CACI, Booz Allen Hamilton), generate an LLM-based price-impact prediction, measure realized stock price reaction over one trading session, and email an alert when the actual reaction is ≤ 50% of the predicted move (unreacted opportunity). Designed to run as a scheduled remote routine daily at 16:30 ET after US market close.
---

# Contract Win — US Defense Routine

Monitors the US Department of Defense daily contract announcements at `defense.gov/News/Contracts/` for the watchlist defined in `references/watchlist.yaml`. For each matched contract, generates an LLM-based predicted price move via OpenRouter, measures the realized 1-session price reaction via FMP, and sends an email alert via Resend when the actual reaction is meaningfully smaller than predicted.

## When to run

- **Scheduled:** daily at 16:30 ET Mon–Fri. Captures previous trading day's DoD batch (which has had one full trading session to react).
- **Ad-hoc:** can be run manually for backtesting or spot-checks.

## Required environment variables

| Variable | Purpose |
|---|---|
| `FMP_API_KEY` | Stock price data + company financials |
| `OPENROUTER_API_KEY` | LLM-based price-move prediction |
| `RESEND_API_KEY` | Email notifications |
| `NOTIFY_EMAIL_TO` | Destination email |
| `NOTIFY_EMAIL_FROM` | Sender (default: `Contract Routine <onboarding@resend.dev>`) |
| `OPENROUTER_MODEL` | Model override (default: `anthropic/claude-sonnet-4.5`) |
| `SAM_API_KEY` | Reserved for future supplemental award-data fetch (not used in v1) |

## Data sources (with fallback)

| Priority | Source | Latency | Auth | Notes |
|---|---|---|---|---|
| 1 (primary) | DoD daily wire — `war.gov/News/Contracts/` (Akamai-protected) | real-time at 17:00 ET | none | Scraped via `curl_cffi` with multi-profile TLS impersonation and session warming. Cloud datacenter IPs often hit 403; the scraper retries through chrome124 → chrome131 → safari17_0 → firefox133. |
| 2 (fallback) | USASpending.gov REST API | ~24h lag | none | Structured JSON, no bot detection. Triggered automatically when the DoD fetcher raises. Returns the same `ContractHit` shape with a synthesized contract paragraph. |

The source for each analyzed contract is tagged in `state/log.jsonl` under the `source` field (`DoD` or `USASpending`).

## Workflow

1. **Fetch** — `scripts/fetch_dod_contracts.py` scrapes the latest daily article from `war.gov`. If 403/error after the impersonation fallback chain, `scripts/fetch_usaspending.py` queries USASpending per watchlist company.
2. **Filter** — match paragraphs against `references/watchlist.yaml` (name aliases + min contract value, with word-boundary regex to drop substring noise like COMUNICACIONES → CACI).
3. **Predict** — `scripts/predict_move.py` builds a prompt with contract text + company context (market cap, TTM revenue from FMP) → OpenRouter → JSON `{predicted_pct, confidence, rationale, materiality}`.
4. **Measure** — `scripts/check_reaction.py` fetches baseline close (last close before announcement) and current price from FMP → actual % move.
5. **Decide:**
   - `|actual| >= |predicted|` → priced in, log only.
   - `|actual| <= 0.5 * |predicted|` → **alert**, send email.
   - else → partial reaction, log only.
6. **Notify** — `scripts/send_email.py` sends a Resend email with contract excerpt, prediction, gap, and source URL.
7. **Persist** — append a JSON record to `state/log.jsonl`.

## How to invoke

```bash
cd skills/contract-win-us-defense
pip install -r requirements.txt
python scripts/run.py
```

Exit code 0 on success (regardless of whether any alerts fired). Non-zero if a hard failure occurs (e.g., DoD page unreachable, all FMP requests failing).

## Output

- **Email** per qualifying alert.
- **`state/log.jsonl`** — append-only audit log. One JSON object per analyzed contract with `timestamp, announcement_date, ticker, contract_value_usd, predicted_pct, actual_pct, decision, rationale, source_url`.

## Tuning

- `references/watchlist.yaml` — add tickers, aliases, or adjust `unreacted_ratio` (default 0.5) and `min_contract_value_usd` (default 7.5M, DoD reporting floor).
