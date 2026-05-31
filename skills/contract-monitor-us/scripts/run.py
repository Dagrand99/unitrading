"""
Orchestrator for contract-monitor-us. Daily 22:00 UTC.

Runs BOTH US public-procurement sources in one pass:
  1. SAM.gov  — real-time Award Notices (best latency; sparse but clean)
  2. USASpending.gov — nightly DATA-Act ingest (comprehensive but lagged)

Dedup is per-source (seen.json keys are "SAM.gov:<noticeId>" vs
"USASpending:<award_id>"), so the same award appearing in both feeds with
different ids does not cross-dedup. The shared log.jsonl tags each record
with its source.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
SHARED = SKILL_ROOT.parent / "_shared"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SHARED))

from fetch_sam import fetch_recent_awards as fetch_sam_awards  # noqa: E402
from fetch_usaspending import fetch_recent_awards as fetch_usaspending_awards  # noqa: E402
from runner import load_master_watchlist, process_hits  # noqa: E402


def main() -> int:
    days_usaspending = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    days_sam = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    wl = load_master_watchlist()
    state_dir = SKILL_ROOT / "state"
    exit_code = 0

    # ── SAM.gov (skipped if no key) ──
    if os.environ.get("SAM_API_KEY"):
        print(f"[SAM.gov] fetching last {days_sam} day(s)...", flush=True)
        try:
            sam_hits = fetch_sam_awards(wl, days_back=days_sam)
            print(f"[SAM.gov] {len(sam_hits)} matching award(s)", flush=True)
            process_hits(
                source_name="SAM.gov",
                hits=sam_hits,
                state_dir=state_dir,
                watchlist=wl,
            )
        except Exception as e:
            print(f"  [warn] SAM.gov pass failed: {e}", file=sys.stderr)
            exit_code = 1
    else:
        print("[SAM.gov] SKIPPED — SAM_API_KEY not set", flush=True)

    # ── USASpending ──
    print(f"[USASpending] fetching last {days_usaspending} day(s)...", flush=True)
    try:
        usa_hits = fetch_usaspending_awards(wl, days_back=days_usaspending)
    except Exception as e:
        print(f"FATAL: USASpending fetch failed: {e}", file=sys.stderr)
        return 2
    print(f"[USASpending] {len(usa_hits)} matching contract(s)", flush=True)
    process_hits(
        source_name="USASpending",
        hits=usa_hits,
        state_dir=state_dir,
        watchlist=wl,
    )

    print("[US] all sources complete", flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
