"""
Orchestrator for the Pharma contract-win routine.

Designed to run daily at 21:00 UTC Mon-Fri (17:00 EDT / 16:00 EST) — 30 min
after the US Defense routine, after US market close.

Looks at USASpending records modified in the last 2 days where the award
Start Date falls in the previous 60 days. Generates LLM prediction, measures
reaction on the home exchange via yfinance, emails alerts when unreacted.
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

from fetch_usaspending import fetch_recent_awards  # noqa: E402
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

    print("[1/4] Fetching USASpending federal awards for pharma watchlist...", flush=True)
    try:
        hits = fetch_recent_awards(watchlist, days_back=2)
    except Exception as e:
        print(f"FATAL: USASpending fetch failed: {e}", file=sys.stderr)
        return 2
    print(f"      {len(hits)} hit(s) above $25M threshold", flush=True)

    if not hits:
        log_record({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decision": "no_hits",
        })
        print("Done — no watchlist matches.", flush=True)
        return 0

    alerts_sent = 0
    for hit in hits:
        print(f"\n[2/4] {hit.ticker} — {hit.company_name}", flush=True)
        print(
            f"      value=${hit.contract_value_usd:,} "
            f"award_id={hit.award_id} "
            f"start_date={hit.announcement_date} "
            f"last_mod={hit.last_modified_date}",
            flush=True,
        )

        record: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "announcement_date": hit.announcement_date,
            "last_modified_date": hit.last_modified_date,
            "source_url": hit.source_url,
            "source": "USASpending",
            "ticker": hit.ticker,
            "company_name": hit.company_name,
            "matched_alias": hit.matched_alias,
            "award_id": hit.award_id,
            "contract_value_usd": hit.contract_value_usd,
            "paragraph": hit.paragraph,
        }

        try:
            company_ctx = get_company_context(hit.ticker)
            prediction = predict_price_move(
                paragraph=hit.paragraph,
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

            reaction = get_baseline_and_current_price(hit.ticker, hit.announcement_date)
            if reaction is None:
                record["decision"] = "no_price_data"
                print("      skipping — no price data for ticker on yfinance", flush=True)
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
                country = company_ctx.get("country") or ""
                resp = send_alert(
                    ticker=hit.ticker,
                    company_name=hit.company_name,
                    country=country,
                    contract_value_usd=hit.contract_value_usd,
                    award_id=hit.award_id,
                    paragraph=hit.paragraph,
                    predicted_pct=predicted_pct,
                    actual_pct=actual_pct,
                    confidence=prediction.get("confidence", "n/a"),
                    materiality=prediction.get("materiality", "n/a"),
                    rationale=prediction.get("rationale", ""),
                    baseline_close=reaction["baseline_close"],
                    baseline_date=reaction["baseline_date"],
                    current_price=reaction["current_price"],
                    source_url=hit.source_url,
                    announcement_date=hit.announcement_date,
                    last_modified_date=hit.last_modified_date,
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
