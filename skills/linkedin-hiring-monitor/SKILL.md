---
name: linkedin-hiring-monitor
description: Daily LinkedIn-jobs digest for a 29-company watchlist (semiconductors, mega-cap tech, automakers, renewables). Uses SerpAPI to query Google for linkedin.com/jobs/view URLs indexed in the last 7 days, dedup'd against a local seen-state, then emails a single Resend digest flagging companies with ≥20 postings in the 7-day window as "massive hiring" signals. Designed to run as a scheduled remote routine Mon–Fri at 06:45 UTC (≈09:45 Europe/Sofia in summer).
---

# LinkedIn Hiring Monitor

Tracks LinkedIn job postings across a fixed cross-sector watchlist as a leading indicator of company expansion, hiring sprees, or strategic build-outs. The signal is **count of distinct postings per company in the last 7 days**; companies at or above `ALERT_THRESHOLD_JOBS` (default 20) are highlighted in red in the daily digest as "unusual hiring".

## Why SerpAPI / Google, not LinkedIn directly

LinkedIn does not publish a free Jobs API for non-partners and aggressively rate-limits unauthenticated scrapers. Querying Google (via SerpAPI) scoped to `site:linkedin.com/jobs/view` with the `tbs=qdr:w` recency filter is the most reliable workaround: it returns LinkedIn job URLs that Google has crawled in the past week, with stable job IDs we can use for dedup. Trade-off: 0–48h indexing lag vs LinkedIn's real-time post date.

## Watchlist (29 companies)

Lives in [`watchlist.yaml`](watchlist.yaml).

| Sector | Tickers |
|---|---|
| IT | INTC, AMD, ASML, IBM, SNOW, QCOM, BABA, CSCO, GOOGL, NVDA, AMZN, AAPL, META, MSFT, HPE, DELL, MU |
| Auto | BMW.DE, VOW3.DE, F, TSLA, P911.DE |
| Energy | NDX1.DE, VWS.CO, SCATC.OL, NEL.OL, MCPHY.PA, EDP.LS, EDPR.LS |

Each entry has `search_terms` tried in order; first one that yields ≥1 LinkedIn hit wins (saves SerpAPI quota).

## When to run

- **Scheduled:** Mon–Fri at 06:45 UTC = 09:45 Europe/Sofia (summer / EEST) / 08:45 (winter / EET).
- **Ad-hoc:** any time; safe to re-run — dedup state persists across runs.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SERPAPI_KEY` | yes | — | Google search with `tbs=qdr:w` recency filter |
| `RESEND_API_KEY` | yes | — | Daily digest email |
| `NOTIFY_EMAIL_TO` | yes | — | Destination address |
| `NOTIFY_EMAIL_FROM` | no | `LinkedIn Hiring Monitor <onboarding@resend.dev>` | |

`SERPAPI_KEY` is the only new variable vs the other monitor routines — add it to the Trader environment alongside `RESEND_API_KEY` and `NOTIFY_EMAIL_TO`.

## How to invoke

```bash
cd skills/linkedin-hiring-monitor
pip install -q -r requirements.txt
python scripts/run.py                # default: 7-day lookback, threshold=10
python scripts/fetch_linkedin.py     # SerpAPI smoke test (prints per-ticker counts)
```

Exit code 0 on success, 2 on fetch failure, 3 on email failure.

## Alert logic

For each company:

1. **Fetch** — SerpAPI Google search: `"<term>" site:linkedin.com/jobs/view` with `tbs=qdr:w`. Up to 100 results per query.
2. **Normalize** — extract job_id from `.../jobs/view/<id>` URLs; fall back to full URL when the canonical form is missing.
3. **Dedup** — key `<ticker>:<job_id>` against `state/seen.json`. New postings count toward "new since last run" but old postings still count toward the 7-day total.
4. **Threshold** — companies with `total_count ≥ 10` are flagged "massive hiring" in the email's top section.
5. **Digest** — a single email is sent every run, even when no company crosses the threshold (so you have positive confirmation the routine ran). Subject reflects the headline count.

## Output

- **Email** — one digest per run with three sections: massive hiring (≥10), moderate (1–9), and quiet (0 postings).
- **`state/log.jsonl`** — append-only one-line-per-company audit log.
- **`state/seen.json`** — dedup keys with first-seen timestamps and titles.

## Tuning

- **Threshold:** edit `ALERT_THRESHOLD_JOBS` in [`scripts/run.py`](scripts/run.py). Lower it (e.g. 5) for early-stage detection of small-cap renewables like MCPHY.PA or SCATC.OL; raise it (e.g. 20) for mega-cap-only signals.
- **Lookback:** Google's `tbs=qdr:w` is hard-coded to 1 week. For 1-month windows switch to `qdr:m` in `fetch_linkedin.py` (and update `LOOKBACK_DAYS`).
- **Per-company search terms:** edit `watchlist.yaml`. Put the most distinctive legal name first to avoid generic-word false positives.

## Caveats

- **Indexing lag:** Google may surface a posting a day or two after LinkedIn published it, and may drop postings that LinkedIn removed early. The 7-day window mostly absorbs this.
- **SerpAPI quota:** each routine run makes one query per company (29 total) when the first search term hits, more if early terms return zero. Default SerpAPI free tier is 100 searches/month — verify you're on a paid plan if running daily.
- **Mega-cap noise:** Amazon, Microsoft, Google, Apple, Meta nearly always exceed the 10-posting threshold. They'll appear in the "massive hiring" section every day. The interesting signal is the **delta** (`new_count`) — that's why the email surfaces both values per company.
- **Generic names:** "Meta", "Apple", "Ford" can match unrelated LinkedIn job posts whose body text mentions the word. The `site:linkedin.com/jobs/view` + quoted-term search reduces this materially but does not eliminate it.
