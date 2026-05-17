"""
Shared price helper for contract-monitor-* skills. yfinance-only (no API key).
Returns the most recent close and a previous-session close for context emails.
"""
from __future__ import annotations

from typing import Any

import yfinance as yf


def get_latest_quote(ticker: str) -> dict[str, Any] | None:
    """Return current price + prior close. None if yfinance has no data."""
    t = yf.Ticker(ticker)
    hist = t.history(period="10d", auto_adjust=False)
    if hist is None or hist.empty:
        return None

    closes = hist["Close"].dropna()
    if closes.empty:
        return None
    current = float(closes.iloc[-1])
    prior = float(closes.iloc[-2]) if len(closes) >= 2 else current

    info = {}
    try:
        info = t.info or {}
    except Exception:
        info = {}
    intraday = info.get("currentPrice") or info.get("regularMarketPrice")
    if intraday:
        try:
            current = float(intraday)
        except (TypeError, ValueError):
            pass

    return {
        "current_price": current,
        "prior_close": prior,
        "prior_date": closes.index[-2].strftime("%Y-%m-%d") if len(closes) >= 2 else closes.index[-1].strftime("%Y-%m-%d"),
        "currency": info.get("financialCurrency") or info.get("currency"),
    }


def get_company_context(ticker: str) -> dict[str, Any]:
    """Profile + financials. Used by predict_move LLM prompt."""
    t = yf.Ticker(ticker)
    try:
        info = t.info or {}
    except Exception:
        info = {}
    return {
        "ticker": ticker,
        "name": info.get("longName") or info.get("shortName"),
        "market_cap": info.get("marketCap"),
        "ttm_revenue": info.get("totalRevenue"),
        "industry": info.get("industry"),
        "country": info.get("country"),
        "exchange": info.get("exchange"),
        "beta": info.get("beta"),
        "currency": info.get("financialCurrency") or info.get("currency"),
        "description": (info.get("longBusinessSummary") or "")[:400],
    }
