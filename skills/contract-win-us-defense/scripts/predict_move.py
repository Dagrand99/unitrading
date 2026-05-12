"""
Predict the expected next-trading-day stock price move for a contract win,
using OpenRouter (LLM) with company context fetched from FMP.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import requests

FMP_BASE = "https://financialmodelingprep.com/api/v3"
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
    paragraph: str,
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
        paragraph=paragraph,
        contract_value=f"${contract_value_usd:,}" if contract_value_usd else "unknown",
        ticker=company_context.get("ticker"),
        name=company_context.get("name"),
        market_cap=f"${company_context['market_cap']:,}" if company_context.get("market_cap") else "unknown",
        ttm_revenue=f"${company_context['ttm_revenue']:,}" if company_context.get("ttm_revenue") else "unknown",
        industry=company_context.get("industry") or "unknown",
        beta=company_context.get("beta") or "unknown",
    )

    last_err: Exception | None = None
    for try_model in chain:
        try:
            parsed = _call_openrouter(try_model, prompt, api_key, "contract-win-us-defense")
            parsed["model"] = try_model
            return parsed
        except (ValueError, json.JSONDecodeError, requests.RequestException) as e:
            print(f"  [warn] OpenRouter model {try_model} failed: {e}")
            last_err = e
            continue
    raise RuntimeError(f"All OpenRouter models exhausted. Last error: {last_err}")
