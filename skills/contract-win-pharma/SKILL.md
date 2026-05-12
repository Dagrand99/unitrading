---
name: contract-win-pharma
description: Detect federal contract wins for pharma/biotech watchlist (Moderna, Vertex, Arrowhead, BioNTech, Bavarian Nordic, Valneva, Genmab, Evotec, Novartis, Zealand Pharma) via USASpending.gov, generate LLM-based price-impact prediction, measure realized reaction over one trading session on the home exchange, and email an alert when actual reaction is ≤ 50% of predicted (unreacted opportunity). Designed to run as a scheduled remote routine daily at 21:00 UTC Mon-Fri (17:00 EDT / 16:00 EST), after US market close.
---

# Contract Win — Pharma Routine

Monitors USASpending.gov for federal contract awards (HHS, BARDA, DoD/DTRA, etc.) to a watchlist of listed biotech and large-cap pharma companies. The highest-impact pharma contracts are typically BARDA stockpile or pandemic-readiness awards (Moderna mRNA-1273, Bavarian Nordic Mpox vaccine, etc.) — these are reported through USASpending with structured fields including the contract value, scope, awarding agency, period of performance, and award ID.

## When to run

- **Scheduled:** daily at 21:00 UTC Mon–Fri (17:00 EDT / 16:00 EST), 30 min after the US Defense routine. Captures activity from the US session that just closed.
- **Ad-hoc:** can be run manually for backtest / spot check.

## Required environment variables

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | LLM-based prediction |
| `RESEND_API_KEY` | Email notifications |
| `NOTIFY_EMAIL_TO` | Destination email |
| `NOTIFY_EMAIL_FROM` | Sender (default: `Contract Routine <onboarding@resend.dev>`) |
| `OPENROUTER_MODEL` | Optional override (default chain: `anthropic/claude-sonnet-4` → `anthropic/claude-3.5-sonnet`) |

USASpending and yfinance need no auth.

## Data source

| Source | Latency | Notes |
|---|---|---|
| USASpending.gov | ~days to weeks lag | Structured federal awards. Award **Start Date** used as the announcement anchor (not Last Modified Date), since the contract effective date is typically when the matching press release / 8-K dropped. |

## Workflow

1. **Fetch** — `scripts/fetch_usaspending.py` queries USASpending per watchlist company using `usaspending_search_term`, then applies word-boundary regex filter on Recipient Name to drop substring noise.
2. **Filter** — keeps awards where:
   - Award Start Date within last 60 days
   - Last Modified Date within last 2 days (recently entered USASpending)
   - Recipient name matches an alias on a word boundary
   - Award Amount ≥ $25M (configurable in `watchlist.yaml` → `min_contract_value_usd`)
3. **Predict** — `scripts/predict_move.py` builds a pharma-context prompt + yfinance company context → OpenRouter (with fallback chain) → JSON `{predicted_pct, confidence, materiality, rationale}`.
4. **Measure** — `scripts/check_reaction.py` uses yfinance to get baseline (last close before announcement_date) and current price → actual % move.
5. **Decide:**
   - `|actual| >= |predicted|` → priced in, log only.
   - `|actual| <= 0.5 * |predicted|` → **alert**, send email.
   - else → partial reaction, log only.
6. **Notify** — `scripts/send_email.py` sends a Resend email with the award details, prediction, reaction, and a USASpending source link.
7. **Persist** — append to `state/log.jsonl`.

## Important caveat — data lag

USASpending records can lag the actual contract press release / 8-K by **1–4 weeks**, much more than the DoD daily wire used in the US Defense routine. The alert email includes both `announcement_date` (award Start Date) and `last_modified_date` (when USASpending received the record) so you can judge how fresh the news actually is.

When the LLM rationale concludes "much of the move has already happened", trust it — the `alert` classification might be a false positive caused by old news being newly visible.

## How to invoke

```bash
cd skills/contract-win-pharma
pip install -q -r requirements.txt
python scripts/run.py
```

## Tuning

- `references/watchlist.yaml` — add/edit `usaspending_search_term` (broad term for the initial query) and `aliases` (used for word-boundary post-filter; e.g. `"Moderna"` vs `"Moderna Therapeutics"`).
- `min_contract_value_usd` — default $25M. Lower for tighter signal cadence; raise to focus on transformational awards.
