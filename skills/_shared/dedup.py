"""
Shared seen-contracts store for the contract-monitor-* skills.

Each skill has its own state/seen.json:
  {
    "<source>:<contract_id>": {
      "first_seen": "2026-05-18T21:00:00Z",
      "last_modified_seen": "2026-05-18",
      "last_amount_usd": 12000000,
      "last_alerted": "2026-05-18T21:01:00Z"
    },
    ...
  }

classify_contract() returns one of:
  "new"      — never seen the id before
  "update"   — seen, but last_modified_date or award amount changed
  "duplicate"— seen, no material change → skip
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

Classification = Literal["new", "update", "duplicate"]


@dataclass
class DedupVerdict:
    classification: Classification
    prior_amount_usd: int | None
    prior_last_modified: str | None
    amount_delta_pct: float | None  # signed, vs prior amount; None if new


class SeenStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                self._data: dict = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                self._data = {}
        else:
            self._data = {}

    def classify(
        self,
        source: str,
        contract_id: str,
        last_modified: str,
        amount_usd: int | None,
        material_delta: float = 0.25,
    ) -> DedupVerdict:
        key = f"{source}:{contract_id}"
        prior = self._data.get(key)
        if prior is None:
            return DedupVerdict("new", None, None, None)

        prior_amt = prior.get("last_amount_usd")
        prior_lm = prior.get("last_modified_seen")
        delta_pct: float | None = None
        if amount_usd is not None and prior_amt:
            delta_pct = (amount_usd - prior_amt) / prior_amt * 100.0

        material = False
        if last_modified and prior_lm and last_modified != prior_lm:
            material = True
        if delta_pct is not None and abs(delta_pct) > material_delta * 100.0:
            material = True

        if material:
            return DedupVerdict("update", prior_amt, prior_lm, delta_pct)
        return DedupVerdict("duplicate", prior_amt, prior_lm, delta_pct)

    def record(
        self,
        source: str,
        contract_id: str,
        last_modified: str,
        amount_usd: int | None,
        alerted: bool,
    ) -> None:
        key = f"{source}:{contract_id}"
        now = datetime.now(timezone.utc).isoformat()
        entry = self._data.get(key) or {"first_seen": now}
        entry["last_modified_seen"] = last_modified
        if amount_usd is not None:
            entry["last_amount_usd"] = amount_usd
        if alerted:
            entry["last_alerted"] = now
        self._data[key] = entry

    def flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        tmp.replace(self.path)
