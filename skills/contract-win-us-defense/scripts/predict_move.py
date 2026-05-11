"""
Predict the expected next-trading-day stock price move for a contract win,
using OpenRouter (LLM) with company context fetched from FMP.
"""
from __future__ import annotations

import json
import os
from typing import Any

import requests

FMP_BASE = "https://financialmodelingprep.com/api/v3"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"
TIMEOUT = 60


def _fmp_get(path: str, params: dict[str, Any] | None = None) -> Any:
    key = os.environ["FMP_API_KEY"]
    params = {**(params or {}), "apikey": key}
    r = requests.get(f"{FMP_BASE}/{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def get_company_context(ticker: str) -> dict[str, Any]:
    profile = _fmp_get(f"profile/{ticker}")
    income = _fmp_get(f"income-statement/{ticker}", {"limit": 1})

    p = profile[0] if profile else {}
    i = income[0] if income else {}
    return {
        "ticker": ticker,
        "name": p.get("companyName"),
        "market_cap": p.get("mktCap"),
        "ttm_revenue": i.get("revenue"),
        "industry": p.get("industry"),
        "beta": p.get("beta"),
        "description": (p.get("description") or "")[:400],
    }


PROMPT_TEMPLATE = """You are a sell-side equity analyst covering US government services and defense contractors. A new DoD contract award was just announced. Estimate the expected NEXT-TRADING-DAY stock price move (% change in the close vs the prior close).

CONTRACT ANNOUNCEMENT (verbatim from defense.gov):
\"\"\"
{paragraph}
\"\"\"

CONTRACT VALUE (USD, extracted): {contract_value}

COMPANY CONTEXT:
- Ticker: {ticker}
- Name: {name}
- Market cap (USD): {market_cap}
- TTM revenue (USD): {ttm_revenue}
- Industry: {industry}
- Beta: {beta}

EVALUATION GUIDANCE:
- Materiality = contract_value / TTM revenue. <2% is typically immaterial. 2–10% noticeable. >10% material.
- Distinguish ceiling/IDIQ values from definite/funded amounts. Ceilings move stocks less.
- Recompete wins are roughly neutral; new wins (especially displacing an incumbent) are positive.
- Government services peers (CACI/BAH/SAIC/LDOS) typically move 0.5–3% on single contract wins, occasionally more for transformational deals.
- Negative predictions are valid when the announcement reveals a LOSS relative to expectations (rare in DoD daily wires).

Return STRICT JSON with these keys only — no commentary outside the JSON:
{{
  "predicted_pct": <float, signed>,
  "confidence": "low" | "medium" | "high",
  "materiality": "low" | "medium" | "high",
  "rationale": "<one or two sentences>"
}}"""


def predict_price_move(
    paragraph: str,
    contract_value_usd: int | None,
    company_context: dict[str, Any],
    model: str | None = None,
) -> dict[str, Any]:
    api_key = os.environ["OPENROUTER_API_KEY"]
    model = model or os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)

    prompt = PROMPT_TEMPLATE.format(
        paragraph=paragraph,
        contract_value=f"${contract_value_usd:,}" if contract_value_usd else "unknown",
        ticker=company_context.get("ticker"),
        name=company_context.get("name"),
        market_cap=f"${company_context['market_cap']:,}" if company_context.get("market_cap") else "unknown",
        ttm_revenue=f"${company_context['ttm_revenue']:,}" if company_context.get("ttm_revenue") else "unknown",
        industry=company_context.get("industry") or "unknown",
        beta=company_context.get("beta") or "unknown",
    )

    r = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/unitrading/contract-win-routine",
            "X-Title": "contract-win-us-defense",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json()
    content = payload["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    parsed["model"] = model
    return parsed
