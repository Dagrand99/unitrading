"""
Measure realized price reaction. Baseline = last close strictly BEFORE the
announcement date. Current = latest available quote.

Uses yfinance (Yahoo Finance) — FMP's free tier returns 403 for EU tickers
on Xetra / Euronext / BME / Borsa Italiana / Nasdaq Stockholm.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import yfinance as yf


def get_baseline_and_current_price(ticker: str, announcement_date: str) -> dict[str, Any] | None:
    """
    Returns dict with keys:
      baseline_close, baseline_date, current_price, current_timestamp, actual_pct
    or None if data is unavailable.
    """
    t = yf.Ticker(ticker)
    hist = t.history(period="2mo", auto_adjust=False)
    if hist is None or hist.empty:
        return None

    try:
        ann_dt = datetime.strptime(announcement_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None

    pre = hist[hist.index.date < ann_dt]
    if pre.empty:
        return None

    baseline_close = float(pre["Close"].iloc[-1])
    baseline_date = pre.index[-1].strftime("%Y-%m-%d")

    info = t.info or {}
    current_price = info.get("currentPrice") or info.get("regularMarketPrice")
    if current_price is None:
        current_price = float(hist["Close"].iloc[-1])
    current_price = float(current_price)

    actual_pct = (current_price - baseline_close) / baseline_close * 100.0

    return {
        "baseline_close": baseline_close,
        "baseline_date": baseline_date,
        "current_price": current_price,
        "current_timestamp": info.get("regularMarketTime"),
        "actual_pct": actual_pct,
    }


def evaluate_reaction(predicted_pct: float, actual_pct: float, unreacted_ratio: float = 0.5) -> str:
    """
    Decision rule:
      |actual| >= |predicted|     → 'priced_in'
      |actual| <= ratio*|predicted| → 'alert' (only if signs agree)
      |actual| <= ratio*|predicted| but signs disagree → 'contradicted'
      otherwise                   → 'partial'
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
