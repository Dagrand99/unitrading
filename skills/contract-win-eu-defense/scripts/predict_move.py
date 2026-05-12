"""
Predict the expected next-trading-day stock price move for an EU defense
contract win, using OpenRouter (LLM) with company context from yfinance.

Note: yfinance (Yahoo Finance) is used instead of FMP because FMP's free
tier returns 403 for non-US tickers (HAG.DE, SOP.PA, etc.).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import requests
import yfinance as yf

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# OpenRouter's Anthropic 4.x IDs use hyphens, not periods. Try newer first,
# then fall back to long-stable 3.5.
DEFAULT_MODEL_CHAIN = (
    "anthropic/claude-sonnet-4",
    "anthropic/claude-3.5-sonnet",
)
TIMEOUT = 60
SYSTEM_PROMPT = (
    "You are a sell-side equity analyst. Respond ONLY with a single JSON "
    "object matching the requested schema. No prose, no markdown code fence."
)


def get_company_context(ticker: str) -> dict[str, Any]:
    """Pull profile + financials via yfinance. Values are in the listing
    currency (e.g. EUR for HAG.DE, SEK for SAAB-B.ST); the currency itself
    is included so the LLM can do materiality math in-context."""
    t = yf.Ticker(ticker)
    info = t.info or {}
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


PROMPT_TEMPLATE = """You are a European-equity sell-side analyst covering listed defense and government-IT contractors (Hensoldt, Indra, Sopra Steria, Saab, Leonardo, plus peers Rheinmetall, BAE, Dassault, Thales). A new contract award was just published on TED (Tenders Electronic Daily, the EU public-procurement journal). Estimate the expected NEXT-TRADING-DAY stock price move (% change in close vs prior close) on the home exchange.

CONTRACT NOTICE (from TED):
\"\"\"
{notice_text}
\"\"\"

CONTRACT VALUE (native): {value_native} {currency}
CONTRACT VALUE (~USD): {value_usd}

COMPANY CONTEXT:
- Ticker: {ticker} (exchange: {exchange})
- Name: {name}
- Country: {country}
- Market cap: {market_cap} {financial_currency}
- TTM revenue: {ttm_revenue} {financial_currency}
- Industry: {industry}
- Beta: {beta}

EVALUATION GUIDANCE:
- Materiality = contract_value / TTM revenue. <2% typically immaterial. 2–10% noticeable. >10% material.
- TED notices come AFTER the award decision and often AFTER a press release — much of the news may already be priced in.
- Single-country MoD recompetes (e.g. Italian MoD → Leonardo) typically move stocks 0–1%.
- Cross-border wins or NATO/foreign-MoD wins typically move stocks more.
- Framework agreements / IDIQ-style ceilings move stocks less than definite-quantity awards.
- Sovereignty-linked or symbolic wins (e.g. national champion winning a strategic system abroad) can move 2–5%.

Return STRICT JSON with these keys only — no commentary outside the JSON:
{{
  "predicted_pct": <float, signed>,
  "confidence": "low" | "medium" | "high",
  "materiality": "low" | "medium" | "high",
  "rationale": "<one or two sentences>"
}}"""


def _parse_json_loose(content: str) -> dict[str, Any]:
    """Parse JSON from a model response, tolerating markdown code fences and
    surrounding prose."""
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*\n?", "", content)
        content = re.sub(r"\n?```\s*$", "", content)
        content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def _call_openrouter(model: str, prompt: str, api_key: str, title: str) -> dict[str, Any]:
    r = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Dagrand99/unitrading",
            "X-Title": title,
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, dict) and "error" in payload:
        raise ValueError(f"OpenRouter error for model {model}: {payload['error']}")
    choices = payload.get("choices") or []
    if not choices:
        raise ValueError(
            f"OpenRouter returned no choices for model {model}. "
            f"Payload keys: {list(payload.keys())}. Body snippet: {json.dumps(payload)[:400]}"
        )
    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    if not content:
        finish = choices[0].get("finish_reason")
        raise ValueError(
            f"OpenRouter returned empty content for model {model} "
            f"(finish_reason={finish}). Body snippet: {json.dumps(payload)[:400]}"
        )
    return _parse_json_loose(content)


def predict_price_move(
    notice_text: str,
    contract_value_native: float | None,
    contract_currency: str | None,
    contract_value_usd: int | None,
    company_context: dict[str, Any],
    model: str | None = None,
) -> dict[str, Any]:
    api_key = os.environ["OPENROUTER_API_KEY"]
    user_override = model or os.environ.get("OPENROUTER_MODEL")
    chain: list[str] = []
    if user_override:
        chain.append(user_override)
    for m in DEFAULT_MODEL_CHAIN:
        if m not in chain:
            chain.append(m)

    prompt = PROMPT_TEMPLATE.format(
        notice_text=notice_text,
        value_native=f"{contract_value_native:,.0f}" if contract_value_native else "undisclosed",
        currency=contract_currency or "N/A",
        value_usd=f"${contract_value_usd:,}" if contract_value_usd else "unknown",
        ticker=company_context.get("ticker"),
        exchange=company_context.get("exchange") or "unknown",
        name=company_context.get("name"),
        country=company_context.get("country") or "unknown",
        market_cap=f"{company_context['market_cap']:,}" if company_context.get("market_cap") else "unknown",
        ttm_revenue=f"{company_context['ttm_revenue']:,}" if company_context.get("ttm_revenue") else "unknown",
        financial_currency=company_context.get("currency") or "",
        industry=company_context.get("industry") or "unknown",
        beta=company_context.get("beta") or "unknown",
    )

    last_err: Exception | None = None
    for try_model in chain:
        try:
            parsed = _call_openrouter(try_model, prompt, api_key, "contract-win-eu-defense")
            parsed["model"] = try_model
            return parsed
        except (ValueError, json.JSONDecodeError, requests.RequestException) as e:
            print(f"  [warn] OpenRouter model {try_model} failed: {e}")
            last_err = e
            continue
    raise RuntimeError(f"All OpenRouter models exhausted. Last error: {last_err}")
