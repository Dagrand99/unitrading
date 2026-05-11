"""
Orchestrator for the EU Defense contract-win routine.

Designed to run daily at 08:30 CET Mon-Fri (06:30 UTC / 07:30 UTC in winter).
Looks at TED award notices from the last 2 days for the watchlist companies,
predicts price impact, measures reaction, and emails alerts for unreacted ones.
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from fetch_ted_awards import fetch_recent_awards  # noqa: E402
from predict_move import get_company_context, predict_price_move  # noqa: E402
from check_reaction import get_baseline_and_current_price, evaluate_reaction  # noqa: E402
from send_email import send_alert  # noqa: E402

STATE_DIR = SKILL_ROOT / "state"
STATE_DIR.mkdir(exist_ok=True)
LOG_FILE = STATE_DIR / "log.jsonl"


def load_watchlist() -> dict:
    with open(SKILL_ROOT / "references" / "watchlist.yaml") as f:
        return yaml.safe_load(f)


def log_record(record: dict) -> None:
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def main() -> int:
    watchlist = load_watchlist()
    thresholds = watchlist.get("alert_thresholds", {})
    unreacted_ratio = float(thresholds.get("unreacted_ratio", 0.5))
    min_abs_pct = float(thresholds.get("min_abs_predicted_pct", 1.0))

    print("[1/4] Fetching TED award notices (last 2 days)...", flush=True)
    try:
        hits = fetch_recent_awards(watchlist, days_back=2)
    except Exception as e:
        print(f"FATAL: failed to fetch TED: {e}", file=sys.stderr)
        return 2
    print(f"      {len(hits)} hit(s) across watchlist", flush=True)

    if not hits:
        log_record({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decision": "no_hits",
        })
        print("Done — no watchlist matches.", flush=True)
        return 0

    alerts_sent = 0
    for hit in hits:
        print(
            f"\n[2/4] {hit.ticker} — {hit.company_name} "
            f"({hit.publication_date}, pub#{hit.publication_number})",
            flush=True,
        )
        if hit.contract_value_native is not None:
            print(
                f"      value={hit.contract_value_native:,.0f} {hit.contract_currency}"
                f" (~${hit.contract_value_usd:,} USD)"
                if hit.contract_value_usd
                else f"      value={hit.contract_value_native:,.0f} {hit.contract_currency}",
                flush=True,
            )
        print(f"      title: {hit.title[:180]}", flush=True)

        record: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "publication_number": hit.publication_number,
            "publication_date": hit.publication_date,
            "notice_url": hit.notice_url,
            "ticker": hit.ticker,
            "company_name": hit.company_name,
            "matched_winner_name": hit.matched_winner_name,
            "buyer_name": hit.buyer_name,
            "title": hit.title,
            "contract_value_native": hit.contract_value_native,
            "contract_currency": hit.contract_currency,
            "contract_value_usd": hit.contract_value_usd,
        }

        try:
            company_ctx = get_company_context(hit.ticker)
            prediction = predict_price_move(
                notice_text=hit.short_text(),
                contract_value_native=hit.contract_value_native,
                contract_currency=hit.contract_currency,
                contract_value_usd=hit.contract_value_usd,
                company_context=company_ctx,
            )
            record["company_context"] = company_ctx
            record["prediction"] = prediction

            predicted_pct = float(prediction["predicted_pct"])
            print(
                f"      predicted={predicted_pct:+.2f}% "
                f"confidence={prediction.get('confidence')} "
                f"materiality={prediction.get('materiality')}",
                flush=True,
            )

            if abs(predicted_pct) < min_abs_pct:
                record["decision"] = "below_min_predicted"
                print(
                    f"      skipping — |predicted| {abs(predicted_pct):.2f}% < min {min_abs_pct}%",
                    flush=True,
                )
                log_record(record)
                continue

            reaction = get_baseline_and_current_price(hit.ticker, hit.publication_date)
            if reaction is None:
                record["decision"] = "no_price_data"
                print("      skipping — no price data for this ticker on FMP", flush=True)
                log_record(record)
                continue
            record["reaction"] = reaction

            actual_pct = reaction["actual_pct"]
            print(
                f"      actual={actual_pct:+.2f}% "
                f"(baseline {reaction['baseline_date']} {reaction['baseline_close']:.2f} "
                f"→ {reaction['current_price']:.2f})",
                flush=True,
            )

            decision = evaluate_reaction(predicted_pct, actual_pct, unreacted_ratio)
            record["decision"] = decision
            print(f"      decision={decision}", flush=True)

            if decision == "alert":
                print("[3/4] Sending email alert...", flush=True)
                country = company_ctx.get("country") or "EU"
                resp = send_alert(
                    ticker=hit.ticker,
                    company_name=hit.company_name,
                    country=country,
                    buyer_name=hit.buyer_name,
                    notice_title=hit.title,
                    contract_value_native=hit.contract_value_native,
                    contract_currency=hit.contract_currency,
                    contract_value_usd=hit.contract_value_usd,
                    predicted_pct=predicted_pct,
                    actual_pct=actual_pct,
                    confidence=prediction.get("confidence", "n/a"),
                    materiality=prediction.get("materiality", "n/a"),
                    rationale=prediction.get("rationale", ""),
                    baseline_close=reaction["baseline_close"],
                    baseline_date=reaction["baseline_date"],
                    current_price=reaction["current_price"],
                    notice_url=hit.notice_url,
                    publication_date=hit.publication_date,
                )
                record["email_resp"] = resp
                alerts_sent += 1
                print(f"      sent (id={resp.get('id')})", flush=True)
        except Exception as e:
            record["decision"] = "error"
            record["error"] = f"{type(e).__name__}: {e}"
            record["traceback"] = traceback.format_exc()
            print(f"      ERROR: {e}", file=sys.stderr, flush=True)

        log_record(record)

    print(f"\n[4/4] Routine complete. Alerts sent: {alerts_sent}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
