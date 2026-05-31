"""
LLM predictor for regulatory pharma events (FDA / EMA / ClinicalTrials.gov).

The contract-routine prompt assumes a single dollar-denominated award; that
framing doesn't fit drug approvals, recalls, or trial-status changes. This
prompt instead asks the model to weigh the regulatory event in the context of
the company's pipeline, market cap, and typical sector reaction patterns.

Returns dict: {predicted_pct, confidence, materiality, rationale, model}.
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
    "You are a sell-side biotech / pharma equity analyst evaluating the "
    "immediate stock-price impact of a freshly-published regulatory event "
    "(FDA action, EMA news, or ClinicalTrials.gov status change). Respond "
    "ONLY with one JSON object matching the requested schema. No prose, "
    "no fences."
)

PROMPT_TEMPLATE = """A regulatory event was just published for a pharma / biotech company. Estimate the expected NEXT-TRADING-DAY stock price move (signed % change vs prior close) on the company's home exchange.

SOURCE: {source_name}
EVENT TYPE: {event_type}
EVENT SUBTYPE: {event_subtype}
HEADLINE: {headline}

EVENT TEXT:
\"\"\"
{paragraph}
\"\"\"

COMPANY CONTEXT:
- Ticker: {ticker} (exchange: {exchange})
- Name: {name}
- Country: {country}
- Market cap: {market_cap} {financial_currency}
- TTM revenue: {ttm_revenue} {financial_currency}
- Industry: {industry}
- Beta: {beta}
- Business summary: {description}

EVALUATION GUIDANCE:
- FDA APPROVAL (event_type=fda_approval): typically a strong positive for mid-caps if the drug is a major pipeline asset and the approval was not pre-priced (Phase 3 readouts and PDUFA date set expectations). For mega-caps (Novartis), one approval is rarely >1%. For Moderna / BioNTech / Vertex sized names, a novel-indication approval can move 3-15%; a previously-anticipated supplemental approval usually 1-3%. If the approval was widely expected (PDUFA date passed without surprise), discount toward zero.
- FDA TENTATIVE APPROVAL: usually neutral-to-slightly-positive (≤1%) for the sponsor.
- FDA RECALL (event_type=fda_recall): Class I = serious/life-threatening — 2-8% drawdown for mid-caps depending on revenue exposure. Class II = moderate, 1-3%. Class III = minor, near-zero.
- TRIAL STATUS = COMPLETED: typically already known to the market via prior guidance; small positive (0-2%) unless completion is unusually early or the readout is imminent.
- TRIAL STATUS = TERMINATED / WITHDRAWN: very negative for the sponsor if a Phase 3 pivotal trial. Mid-cap, single-asset companies (Arrowhead, Zealand, Valneva) can drop 10-30%. Mega-caps with deep pipelines usually 1-3%.
- TRIAL STATUS = SUSPENDED: often safety-driven; -3% to -15% for mid-caps depending on the asset's pipeline weight.
- EMA NEWS: CHMP positive opinion is usually pre-priced in US trading but can move EU-listed names 2-5% on first publication. Safety review / referral can move -2% to -8%.
- Read the EVENT TEXT carefully: the paragraph contains structured details (which application, which trial, why_stopped, etc.) that materially change the prediction.
- ClinicalTrials.gov last-update dates can LAG the sponsor's own press release by hours-to-weeks. If the paragraph suggests this is stale news already digested, predict a SMALLER residual move with lower confidence.
- Mega-caps (Novartis NOVN.SW) with broad pipelines rarely move >1.5% on a single regulatory event.

Return STRICT JSON, no commentary:
{{
  "predicted_pct": <signed float, e.g. 4.5 or -2.7>,
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


def predict_regulatory_move(
    *,
    source_name: str,
    hit: dict[str, Any],
    company_context: dict[str, Any],
    title: str = "pharma-regulatory-monitor",
) -> dict[str, Any]:
    api_key = os.environ["OPENROUTER_API_KEY"]
    chain: list[str] = []
    user_override = os.environ.get("OPENROUTER_MODEL")
    if user_override:
        chain.append(user_override)
    for m in DEFAULT_MODEL_CHAIN:
        if m not in chain:
            chain.append(m)

    prompt = PROMPT_TEMPLATE.format(
        source_name=source_name,
        event_type=hit.get("event_type", "unknown"),
        event_subtype=hit.get("event_subtype", "n/a"),
        headline=hit.get("headline", ""),
        paragraph=hit["paragraph"],
        ticker=company_context.get("ticker"),
        exchange=company_context.get("exchange") or "unknown",
        name=company_context.get("name"),
        country=company_context.get("country") or "unknown",
        market_cap=f"{company_context['market_cap']:,}" if company_context.get("market_cap") else "unknown",
        ttm_revenue=f"{company_context['ttm_revenue']:,}" if company_context.get("ttm_revenue") else "unknown",
        financial_currency=company_context.get("currency") or "",
        industry=company_context.get("industry") or "unknown",
        beta=company_context.get("beta") or "unknown",
        description=(company_context.get("description") or "")[:400],
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
