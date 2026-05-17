"""
Shared LLM predictor for contract-monitor-* skills.

Generic across sectors — the prompt receives the company sector so the model can
calibrate. Returns JSON {predicted_pct, confidence, materiality, rationale, model}.

Default model is perplexity/sonar (user has subscription via OpenRouter).
Falls back through anthropic models if sonar returns empty/errors.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import requests

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL_CHAIN = (
    "perplexity/sonar",
    "anthropic/claude-sonnet-4",
    "anthropic/claude-3.5-sonnet",
)
TIMEOUT = 60

SYSTEM_PROMPT = (
    "You are a sell-side equity analyst evaluating the immediate stock-price "
    "impact of a freshly-announced government contract award. Respond ONLY "
    "with one JSON object matching the requested schema. No prose, no fences."
)

PROMPT_TEMPLATE = """A government contract was just published. Estimate the expected NEXT-TRADING-DAY stock price move (signed % change vs prior close) on the company's home exchange.

SOURCE: {source_name}
EVENT TYPE: {event_type}   (new = first time we see this contract; update = the contract record existed and material change was detected)
SECTOR: {sector}

CONTRACT TEXT:
\"\"\"
{paragraph}
\"\"\"

CONTRACT VALUE: ${contract_value_usd} USD
{update_block}

COMPANY CONTEXT:
- Ticker: {ticker} (exchange: {exchange})
- Name: {name}
- Country: {country}
- Market cap: {market_cap} {financial_currency}
- TTM revenue: {ttm_revenue} {financial_currency}
- Industry: {industry}
- Beta: {beta}

EVALUATION GUIDANCE:
- Materiality = contract value / TTM revenue (or / market cap for pre-revenue companies).
- A $200M award is transformational for a $5B company, immaterial for a $300B mega-cap.
- Defense daily-wire awards >$100M to mid-caps (CACI, BAH, Hensoldt) commonly move 1–4%.
- IT cloud deals (AWS/Azure/Google JWCC-style) are usually pre-priced for mega-caps (<1%) but a sole-source data-center build at a focused name can move 3–8%.
- Pharma BARDA / stockpile awards on novel programs are HIGH impact (3–10% for mid-caps).
- Auto fleet awards (postal, military) tend to move the OEM <1% unless multi-year & headline-grabbing.
- Energy: large wind/hydrogen project awards to project-developers (Vestas, Nordex, Nel) on a multi-GW basis can move 3–6%.
- Data source LATENCY matters: USASpending and SAM.gov often LAG the press release / 8-K by days-to-weeks. If the rationale concludes the news is likely already in the price, predict a SMALLER residual move and lower confidence.
- Mega-caps (Apple, Microsoft, Amazon, Google, Meta, Nvidia, Novartis) rarely move >1% on a single contract.
- For an "update" event with a small amount delta, predict near zero unless the delta itself is huge.

Return STRICT JSON, no commentary:
{{
  "predicted_pct": <signed float, e.g. 2.5 or -0.3>,
  "confidence": "low" | "medium" | "high",
  "materiality": "low" | "medium" | "high",
  "rationale": "<one or two sentences>"
}}"""


def _parse_json_loose(content: str) -> dict[str, Any]:
    content = (content or "").strip()
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
            f"Body snippet: {json.dumps(payload)[:400]}"
        )
    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    if not content:
        finish = choices[0].get("finish_reason")
        raise ValueError(
            f"OpenRouter empty content for model {model} (finish_reason={finish}). "
            f"Body snippet: {json.dumps(payload)[:400]}"
        )
    return _parse_json_loose(content)


def predict_price_move(
    *,
    source_name: str,
    paragraph: str,
    contract_value_usd: int,
    company_context: dict[str, Any],
    sector: str,
    event_type: str = "new",
    update_context: dict[str, Any] | None = None,
    title: str = "contract-monitor",
) -> dict[str, Any]:
    api_key = os.environ["OPENROUTER_API_KEY"]
    chain: list[str] = []
    user_override = os.environ.get("OPENROUTER_MODEL")
    if user_override:
        chain.append(user_override)
    for m in DEFAULT_MODEL_CHAIN:
        if m not in chain:
            chain.append(m)

    update_block = ""
    if event_type == "update" and update_context:
        prior_amt = update_context.get("prior_amount_usd")
        delta = update_context.get("amount_delta_pct")
        update_block = (
            f"\nPRIOR RECORD: amount was ${prior_amt:,} USD; "
            f"current amount delta = {delta:+.1f}% vs prior.\n"
        )

    prompt = PROMPT_TEMPLATE.format(
        source_name=source_name,
        event_type=event_type,
        sector=sector,
        paragraph=paragraph,
        contract_value_usd=f"{contract_value_usd:,}",
        update_block=update_block,
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
            parsed = _call_openrouter(try_model, prompt, api_key, title)
            parsed["model"] = try_model
            return parsed
        except (ValueError, json.JSONDecodeError, requests.RequestException) as e:
            print(f"  [warn] OpenRouter model {try_model} failed: {e}")
            last_err = e
            continue
    raise RuntimeError(f"All OpenRouter models exhausted. Last error: {last_err}")
