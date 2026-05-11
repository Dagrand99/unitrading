"""
Measure realized price reaction. Baseline = last close strictly BEFORE the
announcement date. Current = latest available FMP quote.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import requests

FMP_BASE = "https://financialmodelingprep.com/api/v3"
TIMEOUT = 20


def _fmp_get(path: str, params: dict[str, Any] | None = None) -> Any:
    key = os.environ["FMP_API_KEY"]
    params = {**(params or {}), "apikey": key}
    r = requests.get(f"{FMP_BASE}/{path}", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_baseline_and_current_price(ticker: str, announcement_date: str) -> dict[str, Any] | None:
    """
    Returns dict with keys:
      baseline_close, baseline_date, current_price, current_timestamp, actual_pct
    or None if data is unavailable.
    """
    hist = _fmp_get(f"historical-price-full/{ticker}", {"timeseries": 30})
    bars = hist.get("historical", []) if isinstance(hist, dict) else []
    if not bars:
        return None

    ann_dt = datetime.strptime(announcement_date, "%Y-%m-%d").date()
    baseline_close = None
    baseline_date = None
    for bar in bars:  # newest first
        try:
            bar_date = datetime.strptime(bar["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if bar_date < ann_dt:
            baseline_close = bar["close"]
            baseline_date = bar["date"]
            break

    if baseline_close is None:
        return None

    quote = _fmp_get(f"quote/{ticker}")
    if not quote:
        return None
    current_price = quote[0].get("price")
    current_ts = quote[0].get("timestamp")
    if current_price is None:
        return None

    actual_pct = (current_price - baseline_close) / baseline_close * 100.0

    return {
        "baseline_close": float(baseline_close),
        "baseline_date": baseline_date,
        "current_price": float(current_price),
        "current_timestamp": current_ts,
        "actual_pct": actual_pct,
    }


def evaluate_reaction(predicted_pct: float, actual_pct: float, unreacted_ratio: float = 0.5) -> str:
    """
    Decision rule:
      |actual| >= |predicted|     → 'priced_in'
      |actual| <= ratio*|predicted| → 'alert'
      otherwise                   → 'partial'
    Sign alignment is required for 'alert' — a move in the wrong direction is
    not an unreacted opportunity, it's a contradicted prediction.
    """
    abs_p = abs(predicted_pct)
    abs_a = abs(actual_pct)
    if abs_p == 0:
        return "priced_in"
    if abs_a >= abs_p:
        return "priced_in"
    same_sign = (predicted_pct >= 0) == (actual_pct >= 0)
    if abs_a <= unreacted_ratio * abs_p and same_sign:
        return "alert"
    if abs_a <= unreacted_ratio * abs_p and not same_sign:
        return "contradicted"
    return "partial"
