"""
Orchestrator for linkedin-hiring-monitor.

For each company in skills/linkedin-hiring-monitor/watchlist.yaml:
  1. Query SerpAPI for LinkedIn job postings indexed by Google in the
     past 7 days (`tbs=qdr:w`, scoped to `site:linkedin.com/jobs/view`).
  2. Dedup against state/seen.json (keyed by LinkedIn job_id when
     available, full URL otherwise).
  3. Mark the company "massive hiring" if its 7-day dedup'd count is
     ≥ ALERT_THRESHOLD_JOBS (10).

A single digest email is sent at the end summarising:
  - companies above threshold (highlighted top section)
  - companies with 1..threshold-1 postings
  - companies with 0 postings (compact tail)

Scheduled at 06:45 UTC Mon–Fri (≈09:45 Europe/Sofia in summer / 08:45 in winter).
State persists to skills/linkedin-hiring-monitor/state/.
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
SKILL_ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from fetch_linkedin import fetch_all  # noqa: E402
from notify import send_digest  # noqa: E402

ALERT_THRESHOLD_JOBS = 20  # per user spec: "massive / unusual hiring"
LOOKBACK_DAYS = 7  # per user spec: postings max 7 days old
MAX_SAMPLE_TITLES = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_watchlist() -> list[dict[str, Any]]:
    wl_path = SKILL_ROOT / "watchlist.yaml"
    data = yaml.safe_load(wl_path.read_text())
    return data["companies"]


def _load_seen(seen_file: Path) -> dict[str, dict[str, Any]]:
    if not seen_file.exists():
        return {}
    try:
        return json.loads(seen_file.read_text())
    except json.JSONDecodeError:
        # Corrupt — back it up and start fresh.
        seen_file.rename(seen_file.with_suffix(".json.corrupt"))
        return {}


def _save_seen(seen_file: Path, seen: dict[str, dict[str, Any]]) -> None:
    seen_file.parent.mkdir(parents=True, exist_ok=True)
    seen_file.write_text(json.dumps(seen, indent=2, sort_keys=True))


def _append_log(log_file: Path, record: dict[str, Any]) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _classify(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split into (massive, moderate, quiet=empty placeholder, zero)."""
    massive = [e for e in entries if e["total_count"] >= ALERT_THRESHOLD_JOBS]
    moderate = [e for e in entries if 1 <= e["total_count"] < ALERT_THRESHOLD_JOBS]
    zero = [e for e in entries if e["total_count"] == 0]
    # Sort each bucket by count desc.
    massive.sort(key=lambda e: (-e["total_count"], e["ticker"]))
    moderate.sort(key=lambda e: (-e["total_count"], e["ticker"]))
    zero.sort(key=lambda e: e["ticker"])
    return massive, moderate, [], zero


def main() -> int:
    run_started_at = _now_iso()
    state_dir = SKILL_ROOT / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    seen_file = state_dir / "seen.json"
    log_file = state_dir / "log.jsonl"

    wl = _load_watchlist()
    print(
        f"[linkedin-hiring-monitor] starting run "
        f"(watchlist={len(wl)}, threshold={ALERT_THRESHOLD_JOBS}, "
        f"lookback={LOOKBACK_DAYS}d)",
        flush=True,
    )

    try:
        per_company = fetch_all(wl)
    except Exception as e:  # noqa: BLE001
        print(f"[fatal] fetch_all failed: {e}", file=sys.stderr)
        traceback.print_exc()
        return 2

    seen = _load_seen(seen_file)

    # Build per-company summary entries.
    entries: list[dict[str, Any]] = []
    total_postings = 0
    total_new = 0

    for c in wl:
        ticker = c["ticker"]
        jobs = per_company.get(ticker, [])
        total_postings += len(jobs)

        new_jobs: list[dict[str, Any]] = []
        for j in jobs:
            key = f"{ticker}:{j['job_id']}"
            if key in seen:
                continue
            new_jobs.append(j)
            seen[key] = {
                "first_seen": run_started_at,
                "title": j["title"],
                "url": j["url"],
            }

        total_new += len(new_jobs)

        # Sample titles for the email — prefer newly-seen, fall back to any.
        sample_pool = new_jobs if new_jobs else jobs
        sample_titles = [
            {"title": j["title"], "url": j["url"]}
            for j in sample_pool[:MAX_SAMPLE_TITLES]
        ]

        entry = {
            "ticker": ticker,
            "name": c["name"],
            "sector": c["sector"],
            "country": c["country"],
            "total_count": len(jobs),
            "new_count": len(new_jobs),
            "sample_titles": sample_titles,
        }
        entries.append(entry)

        _append_log(log_file, {
            "timestamp": run_started_at,
            "ticker": ticker,
            "name": c["name"],
            "total_count": len(jobs),
            "new_count": len(new_jobs),
            "above_threshold": len(jobs) >= ALERT_THRESHOLD_JOBS,
        })

    massive, moderate, _quiet, zero = _classify(entries)
    n_massive = len(massive)

    print(
        f"[linkedin-hiring-monitor] totals: postings={total_postings}, "
        f"new={total_new}, massive={n_massive}/{len(wl)}",
        flush=True,
    )
    for e in massive:
        print(f"  ★ MASSIVE {e['ticker']:<8} {e['name']:<28} {e['total_count']} postings", flush=True)

    _save_seen(seen_file, seen)

    # Always send a digest so the user has visible confirmation the routine ran.
    try:
        resp = send_digest(
            threshold=ALERT_THRESHOLD_JOBS,
            massive_hiring=massive,
            moderate=moderate,
            quiet=[],
            zero=zero,
            total_new=total_new,
            total_postings=total_postings,
            run_started_at=run_started_at,
        )
        print(f"[email] sent id={resp.get('id')}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[email] FAILED: {e}", file=sys.stderr)
        traceback.print_exc()
        return 3

    print(
        f"[linkedin-hiring-monitor] complete. "
        f"massive={n_massive} moderate={len(moderate)} zero={len(zero)} "
        f"new_postings={total_new}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
