---
name: pharma-regulatory-monitor
description: Monitor regulatory filings for a pharma/biotech watchlist (Moderna, Vertex, BioNTech, Arrowhead, Bavarian Nordic, Valneva, Genmab, Evotec, Novartis, Zealand Pharma) across three sources — FDA (openFDA Drugs@FDA approvals + Drug Enforcement recalls), ClinicalTrials.gov (v2 API, Phase 2-4 status changes), and EMA (news RSS, best-effort). For each new event, predict the next-trading-day stock price move via OpenRouter LLM and email an alert to NOTIFY_EMAIL_TO when |predicted_pct| > 2.0%. Designed to run as a scheduled remote routine daily at 22:30 UTC after US market close.
---

# Pharma Regulatory Monitor (FDA + ClinicalTrials.gov + EMA)

Tracks three independent regulatory feeds for the same fixed pharma watchlist and runs each new event through the standard predict → email pipeline. Threshold is **2.0%** per spec (lower than the contract routines' 3.0%), since drug approvals and trial readouts often produce smaller but still tradeable moves on mega-caps.

| Sub-source | Endpoint | Latency | Auth | Coverage |
|---|---|---|---|---|
| **FDA — Drugs@FDA (CDER)** | `api.fda.gov/drug/drugsfda.json` | minutes-to-hours after action | none (key optional, raises rate limit) | NDA / supplemental approvals and tentative approvals by sponsor. CDER only — vaccines and many novel biologics filed at CBER are not here. |
| **FDA — Enforcement** | `api.fda.gov/drug/enforcement.json` | hours-to-days | none / optional key | Class I/II/III drug recalls by recalling firm |
| **FDA — Press Releases (RSS)** | `fda.gov/.../press-releases/rss.xml` | feed-refresh latency | none | Catches CBER vaccine approvals, warning letters, and any newsroom item mentioning a watchlist alias on title + description |
| **ClinicalTrials.gov** | `clinicaltrials.gov/api/v2/studies` | minutes after sponsor update | none | Phase 2/3/4 trial status changes (COMPLETED, TERMINATED, WITHDRAWN, SUSPENDED) |
| **EMA** | `ema.europa.eu/news.xml` (overridable) | feed-refresh latency | none | Press-release-style news items matched by alias on title + summary |

## Watchlist

The 10 pharma tickers live under `sector: Pharma` in `skills/_shared/master_watchlist.yaml`:

| Ticker | Name | Exchange |
|---|---|---|
| MRNA | Moderna | NASDAQ |
| VRTX | Vertex Pharmaceuticals | NASDAQ |
| BNTX | BioNTech | NASDAQ |
| ARWR | Arrowhead Pharmaceuticals | NASDAQ |
| BAVA.CO | Bavarian Nordic | Copenhagen |
| VLA.PA | Valneva | Paris |
| GMAB.CO | Genmab | Copenhagen |
| EVT.DE | Evotec | Xetra |
| NOVN.SW | Novartis | SIX |
| ZEAL.CO | Zealand Pharma | Copenhagen |

If you add/remove tickers, edit the master watchlist — this skill auto-filters `sector == "Pharma"`.

## When to run

- **Scheduled:** daily at 22:30 UTC Mon–Fri (≈18:30 EDT). All US-listed names already closed; EU names closed earlier. The predictions describe the next-session move on the home exchange.
- **Ad-hoc:** any time; supports a custom lookback per source.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENROUTER_API_KEY` | yes | — | LLM prediction |
| `RESEND_API_KEY` | yes | — | Email alerts |
| `NOTIFY_EMAIL_TO` | yes | — | Destination |
| `NOTIFY_EMAIL_FROM` | no | `Pharma Reg Monitor <onboarding@resend.dev>` | |
| `OPENROUTER_MODEL` | no | `perplexity/sonar` (then anthropic fallback) | |
| `OPENFDA_API_KEY` | no | — | Raises openFDA limit from 240/day to 240/min |
| `EMA_NEWS_RSS_URL` | no | `https://www.ema.europa.eu/en/news-rss` | Override if EMA re-platforms |

## How to invoke

```bash
cd skills/pharma-regulatory-monitor
pip install -q -r requirements.txt
python scripts/run.py                # default: FDA 3-day, CT.gov 7-day, EMA 7-day
python scripts/run.py 14 30 14       # backtest: FDA 14d, CT.gov 30d, EMA 14d
python scripts/fetch_fda.py 7        # FDA fetcher only (stdout JSON)
python scripts/fetch_clinicaltrials.py 7   # CT.gov fetcher only
python scripts/fetch_ema.py 7        # EMA fetcher only
```

Exit code 0 on success. Non-zero only on hard FDA or ClinicalTrials.gov failure; EMA is best-effort (its RSS URL has historically moved across site redesigns).

## Alert logic

For each event:

1. **Dedup** — keyed by `<source>:<event_id>` in `state/seen.json`. Event IDs are stable per source:
   - `FDA-Drugs:<application_no>:<submission_type>:<submission_no>:<status>:<date>`
   - `FDA-Recall:<recall_number>`
   - `CT:<nctId>:<overallStatus>:<lastUpdate>`
   - `EMA:<rss_guid_or_link>`
2. **Predict** — pharma-specific LLM prompt (see [`scripts/predict.py`](scripts/predict.py)) calibrated against typical sector reactions (BARDA approvals, Phase 3 termination, Class I recalls, CHMP positive opinion). Event IDs for FDA press releases use `FDA-PR:<guid>`.
3. **Notify** — email when `|predicted_pct| > 2.0`. Subject includes the event tag (`APPROVAL`, `RECALL`, `TRIAL`, `EMA NEWS`) and the source.
4. **Persist** — every evaluated record appended to `state/log.jsonl` with `decision ∈ {alert, below_alert_threshold, duplicate, no_hits, error}`.

## Tuning

- **Alert threshold:** edit `ALERT_THRESHOLD_PCT` in [`scripts/run.py`](scripts/run.py).
- **Phase filter on ClinicalTrials.gov:** `HIGH_SIGNAL_PHASES` in [`scripts/fetch_clinicaltrials.py`](scripts/fetch_clinicaltrials.py) — defaults to Phase 2/3/4 only.
- **High-signal trial statuses:** `HIGH_SIGNAL_STATUSES` in the same file — currently `COMPLETED`, `TERMINATED`, `WITHDRAWN`, `SUSPENDED`. Add `ACTIVE_NOT_RECRUITING` if you want milestone-completion alerts.
- **FDA approval statuses:** `APPROVAL_STATUSES` in [`scripts/fetch_fda.py`](scripts/fetch_fda.py).

## Output

- **Email** per qualifying alert.
- **`state/log.jsonl`** — append-only one-record-per-event audit log.
- **`state/seen.json`** — dedup keys with first-seen and last-alerted timestamps.

## Caveats

- **Lag**: openFDA refreshes daily; ClinicalTrials.gov reflects sponsor's own updates which can lag the company press release. The LLM is prompted to discount predictions when the rationale suggests the news has already been digested by the market.
- **EMA**: the news RSS URL has changed multiple times as EMA re-platforms its site. The fetcher tries several fallback URLs and skips silently if all fail — set `EMA_NEWS_RSS_URL` if you know the current canonical URL.
- **Mega-cap dampening**: Novartis (NOVN.SW) rarely moves >1.5% on a single regulatory event; the LLM prompt accounts for this explicitly so most NOVN events will log as `below_alert_threshold`.
