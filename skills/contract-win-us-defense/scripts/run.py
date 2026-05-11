"""
Orchestrator for the US Defense contract-win routine.

Designed to be invoked once per scheduled run (16:30 ET, Mon-Fri).
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

from fetch_dod_contracts import fetch_latest_dod_page, find_company_mentions  # noqa: E402
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

    print("[1/4] Fetching latest DoD contracts page...", flush=True)
    try:
        article = fetch_latest_dod_page()
    except Exception as e:
        print(f"FATAL: failed to fetch DoD page: {e}", file=sys.stderr)
        return 2
    announcement_date = article.announcement_date
    text = article.text
    source_url = article.article_url
    print(f"      date={announcement_date} chars={len(text)} url={source_url}", flush=True)

    print("[2/4] Filtering for watchlist matches...", flush=True)
    hits = find_company_mentions(text, watchlist, announcement_date, source_url)
    print(f"      {len(hits)} match(es)", flush=True)

    if not hits:
        log_record({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "announcement_date": announcement_date,
            "source_url": source_url,
            "decision": "no_hits",
        })
        print("Done — no watchlist matches.", flush=True)
        return 0

    alerts_sent = 0
    for hit in hits:
        print(f"\n[3/4] {hit.ticker} — {hit.company_name}", flush=True)
        print(f"      contract_value=${hit.contract_value_usd:,}", flush=True)
        print(f"      paragraph (first 200): {hit.paragraph[:200]!r}", flush=True)

        record: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "announcement_date": announcement_date,
            "source_url": source_url,
            "ticker": hit.ticker,
            "company_name": hit.company_name,
            "matched_alias": hit.matched_alias,
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

            reaction = get_baseline_and_current_price(hit.ticker, announcement_date)
            if reaction is None:
                record["decision"] = "no_price_data"
                print("      skipping — no price data", flush=True)
                log_record(record)
                continue
            record["reaction"] = reaction

            actual_pct = reaction["actual_pct"]
            print(
                f"      actual={actual_pct:+.2f}% "
                f"(baseline {reaction['baseline_date']} ${reaction['baseline_close']:.2f} "
                f"→ ${reaction['current_price']:.2f})",
                flush=True,
            )

            decision = evaluate_reaction(predicted_pct, actual_pct, unreacted_ratio)
            record["decision"] = decision
            print(f"      decision={decision}", flush=True)

            if decision == "alert":
                print("[4/4] Sending email alert...", flush=True)
                resp = send_alert(
                    ticker=hit.ticker,
                    company_name=hit.company_name,
                    contract_value_usd=hit.contract_value_usd,
                    predicted_pct=predicted_pct,
                    actual_pct=actual_pct,
                    confidence=prediction.get("confidence", "n/a"),
                    materiality=prediction.get("materiality", "n/a"),
                    rationale=prediction.get("rationale", ""),
                    paragraph=hit.paragraph,
                    baseline_close=reaction["baseline_close"],
                    baseline_date=reaction["baseline_date"],
                    current_price=reaction["current_price"],
                    source_url=source_url,
                    announcement_date=announcement_date,
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

    print(f"\nRoutine complete. Alerts sent: {alerts_sent}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
