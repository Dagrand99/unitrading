"""
Shared orchestration: given a list of `ContractHit`-shaped dicts from a source
fetcher, run dedup → predict → email-if-above-threshold → persist log.

All contract-monitor-* skills call `process_hits(...)` so the alert logic is
identical across sources.
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from dedup import SeenStore  # noqa: E402
from notify import send_alert  # noqa: E402
from predict_move import predict_price_move  # noqa: E402
from prices import get_company_context, get_latest_quote  # noqa: E402


def load_master_watchlist() -> dict[str, Any]:
    with open(HERE / "master_watchlist.yaml") as f:
        return yaml.safe_load(f)


def companies_by_ticker(watchlist: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["ticker"]: c for c in watchlist["companies"]}


def process_hits(
    *,
    source_name: str,  # "USASpending", "TED", "SAM.gov", "gov.uk"
    hits: list[dict[str, Any]],
    state_dir: Path,
    watchlist: dict[str, Any],
) -> int:
    """
    Each hit must be a dict with keys:
      ticker, company_name, sector, country, contract_id, paragraph,
      contract_value_usd (int), source_url, announcement_date (YYYY-MM-DD),
      last_modified_date (YYYY-MM-DD)

    Returns alert count. Always writes log.jsonl + flushes seen.json.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    log_file = state_dir / "log.jsonl"
    seen_file = state_dir / "seen.json"
    store = SeenStore(seen_file)

    thresholds = watchlist.get("alert_thresholds", {})
    alert_threshold = float(thresholds.get("predicted_pct_alert", 3.0))
    material_delta = float(thresholds.get("update_material_value_delta", 0.25))
    min_value = float(thresholds.get("min_contract_value_usd", 0))

    alerts_sent = 0
    if not hits:
        _append(log_file, {
            "timestamp": _now(),
            "source": source_name,
            "decision": "no_hits",
        })
        print(f"[{source_name}] no hits", flush=True)
        store.flush()
        return 0

    for hit in hits:
        record: dict[str, Any] = {
            "timestamp": _now(),
            "source": source_name,
            **{k: hit.get(k) for k in (
                "ticker", "company_name", "sector", "country",
                "contract_id", "contract_value_usd", "source_url",
                "announcement_date", "last_modified_date", "paragraph",
            )},
        }

        try:
            amount = int(hit.get("contract_value_usd") or 0)
            if amount and amount < min_value:
                record["decision"] = "below_min_value"
                _append(log_file, record)
                continue

            verdict = store.classify(
                source=source_name,
                contract_id=hit["contract_id"],
                last_modified=hit.get("last_modified_date") or "",
                amount_usd=amount if amount > 0 else None,
                material_delta=material_delta,
            )
            record["classification"] = verdict.classification
            if verdict.amount_delta_pct is not None:
                record["amount_delta_pct"] = round(verdict.amount_delta_pct, 2)

            if verdict.classification == "duplicate":
                record["decision"] = "duplicate"
                _append(log_file, record)
                continue

            event_type = verdict.classification  # "new" or "update"

            ctx = get_company_context(hit["ticker"])
            record["company_context"] = ctx

            update_ctx = None
            if event_type == "update":
                update_ctx = {
                    "prior_amount_usd": verdict.prior_amount_usd,
                    "prior_last_modified": verdict.prior_last_modified,
                    "amount_delta_pct": verdict.amount_delta_pct,
                }

            prediction = predict_price_move(
                source_name=source_name,
                paragraph=hit["paragraph"],
                contract_value_usd=amount,
                company_context=ctx,
                sector=hit.get("sector", "unknown"),
                event_type=event_type,
                update_context=update_ctx,
                title=f"contract-monitor-{source_name.lower()}",
            )
            record["prediction"] = prediction
            predicted_pct = float(prediction["predicted_pct"])
            print(
                f"[{source_name}] {hit['ticker']} ({event_type}) "
                f"value=${amount:,} predicted={predicted_pct:+.2f}% "
                f"conf={prediction.get('confidence')} mat={prediction.get('materiality')}",
                flush=True,
            )

            quote = get_latest_quote(hit["ticker"])
            if quote:
                record["quote"] = quote

            alerted = False
            if abs(predicted_pct) > alert_threshold:
                resp = send_alert(
                    source_name=source_name,
                    event_type=event_type,
                    ticker=hit["ticker"],
                    company_name=hit["company_name"],
                    sector=hit.get("sector", ""),
                    country=hit.get("country", ""),
                    contract_value_usd=amount,
                    contract_id=hit["contract_id"],
                    paragraph=hit["paragraph"],
                    predicted_pct=predicted_pct,
                    confidence=prediction.get("confidence", "n/a"),
                    materiality=prediction.get("materiality", "n/a"),
                    rationale=prediction.get("rationale", ""),
                    model=prediction.get("model", ""),
                    source_url=hit["source_url"],
                    announcement_date=hit.get("announcement_date", ""),
                    last_modified_date=hit.get("last_modified_date", ""),
                    quote=quote,
                    update_context=update_ctx,
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
                contract_id=hit["contract_id"],
                last_modified=hit.get("last_modified_date") or "",
                amount_usd=amount if amount > 0 else None,
                alerted=alerted,
            )

        except Exception as e:  # noqa: BLE001
            record["decision"] = "error"
            record["error"] = f"{type(e).__name__}: {e}"
            record["traceback"] = traceback.format_exc()
            print(f"  [error] {hit.get('ticker')}: {e}", file=sys.stderr, flush=True)

        _append(log_file, record)

    store.flush()
    print(f"[{source_name}] done. alerts={alerts_sent}", flush=True)
    return alerts_sent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(log_file: Path, record: dict[str, Any]) -> None:
    with open(log_file, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
