"""Orchestrator for contract-monitor-sam-gov. Run 2x daily (13:00 + 22:00 UTC)."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
SHARED = SKILL_ROOT.parent / "_shared"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SHARED))

from fetch import fetch_recent_awards  # noqa: E402
from runner import load_master_watchlist, process_hits  # noqa: E402


def main() -> int:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    wl = load_master_watchlist()
    print(f"[SAM.gov] fetching last {days} day(s)...", flush=True)
    try:
        hits = fetch_recent_awards(wl, days_back=days)
    except Exception as e:
        print(f"FATAL: SAM.gov fetch failed: {e}", file=sys.stderr)
        return 2
    print(f"[SAM.gov] {len(hits)} matching award(s)", flush=True)
    process_hits(
        source_name="SAM.gov",
        hits=hits,
        state_dir=SKILL_ROOT / "state",
        watchlist=wl,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
