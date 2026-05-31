"""
Orchestrator for pharma-regulatory-monitor.

Combines three regulatory sources in one pass:
  1. FDA — openFDA Drugs@FDA approvals + Drug Enforcement recalls
  2. ClinicalTrials.gov — v2 API, high-signal status changes on Phase 2-4 trials
  3. EMA — news RSS feed (best-effort, gracefully skipped if unreachable)

Each hit goes through the same pipeline:
  - dedup vs state/seen.json (per-source key namespace)
  - LLM prediction via OpenRouter using the regulatory-event prompt
  - email when |predicted_pct| > 2.0%

Scheduled at 22:30 UTC daily. State persists to skills/pharma-regulatory-monitor/state/.
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
SHARED = SKILL_ROOT.parent / "_shared"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SHARED))

from dedup import SeenStore  # noqa: E402
from fetch_clinicaltrials import fetch_recent_events as fetch_ct_events  # noqa: E402
from fetch_ema import fetch_recent_events as fetch_ema_events  # noqa: E402
from fetch_fda import fetch_recent_events as fetch_fda_events  # noqa: E402
from notify import send_alert  # noqa: E402
from predict import predict_regulatory_move  # noqa: E402
from prices import get_company_context, get_latest_quote  # noqa: E402
from runner import load_master_watchlist  # noqa: E402

ALERT_THRESHOLD_PCT = 2.0  # user spec: notify when |predicted_pct| > 2%


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(log_file: Path, record: dict[str, Any]) -> None:
    with open(log_file, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _process_source(
    *,
    source_name: str,
    hits: list[dict[str, Any]],
    store: SeenStore,
    log_file: Path,
) -> int:
    alerts_sent = 0
    if not hits:
        _append(log_file, {"timestamp": _now(), "source": source_name, "decision": "no_hits"})
        print(f"[{source_name}] no hits", flush=True)
        return 0

    for hit in hits:
        record: dict[str, Any] = {
            "timestamp": _now(),
            "source": source_name,
            **{k: hit.get(k) for k in (
                "ticker", "company_name", "sector", "country",
                "event_id", "event_type", "event_subtype", "headline",
                "source_url", "announcement_date", "last_modified_date", "paragraph",
            )},
        }
        try:
            verdict = store.classify(
                source=source_name,
                contract_id=hit["event_id"],
                last_modified=hit.get("last_modified_date") or "",
                amount_usd=None,
                material_delta=1.0,  # not relevant for regulatory events
            )
            record["classification"] = verdict.classification
            if verdict.classification == "duplicate":
                record["decision"] = "duplicate"
                _append(log_file, record)
                continue

            ctx = get_company_context(hit["ticker"])
            record["company_context"] = ctx

            prediction = predict_regulatory_move(
                source_name=source_name,
                hit=hit,
                company_context=ctx,
            )
            record["prediction"] = prediction
            predicted_pct = float(prediction["predicted_pct"])
            print(
                f"[{source_name}] {hit['ticker']} {hit['event_type']} "
                f"predicted={predicted_pct:+.2f}% "
                f"conf={prediction.get('confidence')} mat={prediction.get('materiality')}",
                flush=True,
            )

            quote = get_latest_quote(hit["ticker"])
            if quote:
                record["quote"] = quote

            alerted = False
            if abs(predicted_pct) > ALERT_THRESHOLD_PCT:
                resp = send_alert(
                    source_name=source_name,
                    hit=hit,
                    predicted_pct=predicted_pct,
                    confidence=prediction.get("confidence", "n/a"),
                    materiality=prediction.get("materiality", "n/a"),
                    rationale=prediction.get("rationale", ""),
                    model=prediction.get("model", ""),
                    quote=quote,
                )
                record["email_resp"] = resp
                record["decision"] = "alert"
                alerted = True
                alerts_sent += 1
                print(f"  → ALERT sent (id={resp.get('id')})", flush=True)
            else:
                record["decision"] = "below_alert_threshold"

            store.record(
                source=source_name,
                contract_id=hit["event_id"],
                last_modified=hit.get("last_modified_date") or "",
                amount_usd=None,
                alerted=alerted,
            )

        except Exception as e:  # noqa: BLE001
            record["decision"] = "error"
            record["error"] = f"{type(e).__name__}: {e}"
            record["traceback"] = traceback.format_exc()
            print(f"  [error] {hit.get('ticker')}: {e}", file=sys.stderr, flush=True)

        _append(log_file, record)

    return alerts_sent


def main() -> int:
    # Lookbacks: FDA changes are timely; EMA RSS is short; CT.gov updates
    # bunch around weekend posting, so default to 7-day lookback.
    days_fda = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    days_ct = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    days_ema = int(sys.argv[3]) if len(sys.argv) > 3 else 7

    wl = load_master_watchlist()
    state_dir = SKILL_ROOT / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log_file = state_dir / "log.jsonl"
    seen_file = state_dir / "seen.json"
    store = SeenStore(seen_file)

    total_alerts = 0
    exit_code = 0

    # ── FDA ──
    print(f"[FDA] fetching last {days_fda} day(s)...", flush=True)
    try:
        fda_hits = fetch_fda_events(wl, days_back=days_fda)
        print(f"[FDA] {len(fda_hits)} matching event(s)", flush=True)
        total_alerts += _process_source(
            source_name="FDA",
            hits=fda_hits,
            store=store,
            log_file=log_file,
        )
    except Exception as e:
        print(f"  [warn] FDA pass failed: {e}", file=sys.stderr)
        exit_code = 1

    # ── ClinicalTrials.gov ──
    print(f"[ClinicalTrials.gov] fetching last {days_ct} day(s)...", flush=True)
    try:
        ct_hits = fetch_ct_events(wl, days_back=days_ct)
        print(f"[ClinicalTrials.gov] {len(ct_hits)} matching event(s)", flush=True)
        total_alerts += _process_source(
            source_name="ClinicalTrials.gov",
            hits=ct_hits,
            store=store,
            log_file=log_file,
        )
    except Exception as e:
        print(f"  [warn] ClinicalTrials.gov pass failed: {e}", file=sys.stderr)
        exit_code = 1

    # ── EMA ──
    print(f"[EMA] fetching last {days_ema} day(s)...", flush=True)
    try:
        ema_hits = fetch_ema_events(wl, days_back=days_ema)
        print(f"[EMA] {len(ema_hits)} matching event(s)", flush=True)
        total_alerts += _process_source(
            source_name="EMA",
            hits=ema_hits,
            store=store,
            log_file=log_file,
        )
    except Exception as e:
        print(f"  [warn] EMA pass failed: {e}", file=sys.stderr)
        # EMA is best-effort; don't promote to a hard failure.

    store.flush()
    print(f"[pharma-regulatory-monitor] complete. alerts={total_alerts}", flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
