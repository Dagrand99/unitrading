"""
Predict the expected next-trading-day stock price move for a pharma contract
win, using OpenRouter (LLM) with company context from yfinance.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import requests
import yfinance as yf

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL_CHAIN = (
    "anthropic/claude-sonnet-4",
    "anthropic/claude-3.5-sonnet",
)
TIMEOUT = 60
SYSTEM_PROMPT = (
    "You are a sell-side biotech equity analyst. Respond ONLY with a single "
    "JSON object matching the requested schema. No prose, no markdown code fence."
)


def get_company_context(ticker: str) -> dict[str, Any]:
    """Pull profile + financials via yfinance. Values in listing currency."""
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


PROMPT_TEMPLATE = """You are a sell-side biotech/pharma equity analyst covering listed mid-cap and large-cap drug developers (Moderna, Vertex, Arrowhead, BioNTech, Bavarian Nordic, Valneva, Genmab, Evotec, Novartis, Zealand Pharma, plus mega-cap peers Pfizer/Lilly/Roche/Novo). A new federal contract award was just published on USASpending.gov. Estimate the expected NEXT-TRADING-DAY stock price move (% change in close vs prior close) on the home exchange.

USASPENDING NOTICE:
\"\"\"
{paragraph}
\"\"\"

CONTRACT VALUE: ${contract_value_usd}

COMPANY CONTEXT:
- Ticker: {ticker} (exchange: {exchange})
- Name: {name}
- Country: {country}
- Market cap: {market_cap} {financial_currency}
- TTM revenue: {ttm_revenue} {financial_currency}
- Industry: {industry}
- Beta: {beta}

EVALUATION GUIDANCE for biotech/pharma:
- Materiality differs from defense: a $200M BARDA award is transformational for a $5B biotech but immaterial for a $300B pharma.
- Materiality threshold = contract_value / TTM revenue (or contract_value / market_cap for pre-revenue biotechs).
- HHS/BARDA/DTRA prime contracts on novel programs are HIGH impact (+3–10% common). Stockpile renewals / option exercises are LOW (0–1%).
- Pure modifications adding small obligated funds to existing IDIQs are usually noise.
- IMPORTANT: USASpending data is LAGGED. The actual press release and 8-K may have hit weeks ago. If the program is one a market would have followed closely (Moderna COVID, Bavarian Nordic Mpox, etc.), assume much of the move has already happened — predict a SMALLER residual move.
- If the contract appears to be a brand-new disclosure (recent Start Date AND specific new scope), predict a LARGER move.
- For US-listed names with significant institutional coverage, mega-cap pharma (Novartis, Pfizer-like) rarely moves > 1% on a single contract.

Return STRICT JSON with these keys only — no commentary outside the JSON:
{{
  "predicted_pct": <float, signed>,
  "confidence": "low" | "medium" | "high",
  "materiality": "low" | "medium" | "high",
  "rationale": "<one or two sentences>"
}}"""


def _parse_json_loose(content: str) -> dict[str, Any]:
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
    paragraph: str,
    contract_value_usd: int,
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
        paragraph=paragraph,
        contract_value_usd=f"{contract_value_usd:,}",
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
            parsed = _call_openrouter(try_model, prompt, api_key, "contract-win-pharma")
            parsed["model"] = try_model
            return parsed
        except (ValueError, json.JSONDecodeError, requests.RequestException) as e:
            print(f"  [warn] OpenRouter model {try_model} failed: {e}")
            last_err = e
            continue
    raise RuntimeError(f"All OpenRouter models exhausted. Last error: {last_err}")
