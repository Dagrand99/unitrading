---
name: contract-win-eu-defense
description: Detect contract wins for EU defense contractors (Hensoldt, Indra Sistemas, Sopra Steria, Saab AB, Leonardo) by querying the TED (Tenders Electronic Daily) EU public-procurement API for recently-published award notices, generate an LLM-based price-impact prediction, measure realized stock price reaction over one trading session, and email an alert when the actual reaction is ≤ 50% of the predicted move. Designed to run as a scheduled remote routine daily at 08:30 CET / CEST.
---

# Contract Win — EU Defense Routine

Monitors TED (Tenders Electronic Daily) at `ted.europa.eu` for newly-published contract-award notices (`notice-type=can-standard`) whose **winner** matches a configured European defense watchlist. For each matched award, generates an LLM price prediction via OpenRouter, measures realized reaction via FMP, and emails a Resend alert when the actual reaction is meaningfully below the predicted move.

## When to run

- **Scheduled:** daily at 08:30 CET Mon–Fri (TED publishes its daily batch ~08:00 CET). The routine looks at notices from the last 2 days so the publication has had one full trading session to react on the home exchange.
- **Ad-hoc:** can be run manually for backtesting.

## Required environment variables

| Variable | Purpose |
|---|---|
| `FMP_API_KEY` | Stock price data + company financials (EU tickers: HAG.DE, IDR.MC, SOP.PA, SAAB-B.ST, LDO.MI) |
| `OPENROUTER_API_KEY` | LLM-based prediction |
| `RESEND_API_KEY` | Email notifications |
| `NOTIFY_EMAIL_TO` | Destination email |
| `NOTIFY_EMAIL_FROM` | Sender (default: `Contract Routine <onboarding@resend.dev>`) |
| `OPENROUTER_MODEL` | Optional override (default: `anthropic/claude-sonnet-4.5`) |

## Workflow

1. **Fetch** — `scripts/fetch_ted_awards.py` queries TED v3 search API per watchlist company:
   ```
   (winner-name="Saab AB" OR winner-name="SAAB Aktiebolag" ...) 
   AND publication-date >= <today-2>
   AND notice-type=can-standard
   ```
2. **Normalize** — each notice is parsed for buyer, multilingual title, contract value, and converted to USD via the static `fx_to_usd` table in `references/watchlist.yaml`.
3. **Filter** — drop notices below `min_contract_value_usd` (default $5M).
4. **Predict** — `scripts/predict_move.py` builds an EU-defense-context prompt + FMP company context → OpenRouter → JSON.
5. **Measure** — `scripts/check_reaction.py` baseline close vs current price for the EU ticker.
6. **Decide:**
   - `|actual| >= |predicted|` → priced in, log only.
   - `|actual| <= 0.5 * |predicted|` → **alert**, send email.
   - else → partial reaction, log only.
7. **Notify** — `scripts/send_email.py` sends a Resend email with notice link, native + USD value, predicted vs actual, and rationale.
8. **Persist** — append to `state/log.jsonl`.

## How to invoke

```bash
cd skills/contract-win-eu-defense
pip install -q -r requirements.txt
python scripts/run.py
```

## Tuning

- `references/watchlist.yaml` — add/edit `winner_names` aliases (TED matches are exact-string-but-case-insensitive on the legal entity name; subsidiaries need their own entry).
- `fx_to_usd` — refresh annually or move to FMP forex if precision matters.
- `unreacted_ratio` (default 0.5), `min_contract_value_usd` (default $5M), `min_abs_predicted_pct` (default 1.0).
