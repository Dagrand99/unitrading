"""
Fallback fetcher: query USASpending.gov for recently-modified federal contract
awards to watchlist recipients. Used when the DoD daily wire (defense.gov /
war.gov) is unreachable due to Akamai bot detection from cloud datacenter IPs.

USASpending lags the DoD daily wire by ~24h but covers the same data, has no
bot detection, requires no API key, and returns structured JSON.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from fetch_dod_contracts import ContractHit

USA_SPENDING_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
TIMEOUT = 30
AWARD_TYPE_CODES = ["A", "B", "C", "D"]  # contracts (excludes grants/loans)
AWARD_WINDOW_DAYS = 14  # how far back to search for awards
RESULT_PAGE_LIMIT = 100


def _query(recipient_term: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    body = {
        "filters": {
            "recipient_search_text": [recipient_term],
            "award_type_codes": AWARD_TYPE_CODES,
            "time_period": [{"start_date": start_date, "end_date": end_date}],
        },
        "fields": [
            "Award ID",
            "Recipient Name",
            "Award Amount",
            "Description",
            "Start Date",
            "End Date",
            "Awarding Agency",
            "Awarding Sub Agency",
            "Contract Award Type",
            "Last Modified Date",
            "generated_internal_id",
        ],
        "sort": "Last Modified Date",
        "order": "desc",
        "page": 1,
        "limit": RESULT_PAGE_LIMIT,
    }
    r = requests.post(USA_SPENDING_URL, json=body, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("results", [])


def _recipient_matches_company(recipient_name: str, aliases: list[str]) -> str | None:
    """Word-boundary case-insensitive match. Returns the matched alias or None."""
    if not recipient_name:
        return None
    for alias in aliases:
        if re.search(rf"\b{re.escape(alias)}\b", recipient_name, re.IGNORECASE):
            return alias
    return None


def _synthesize_paragraph(award: dict[str, Any]) -> str:
    """Build a contract-paragraph-like string the LLM can reason over,
    matching the rough shape of a DoD daily-wire entry."""
    recipient = award.get("Recipient Name", "Unknown recipient")
    amount = award.get("Award Amount")
    desc = (award.get("Description") or "").strip().rstrip(".")
    sub_agency = award.get("Awarding Sub Agency") or ""
    agency = award.get("Awarding Agency") or ""
    award_type = award.get("Contract Award Type") or ""
    start = award.get("Start Date") or ""
    end = award.get("End Date") or ""
    award_id = award.get("Award ID") or ""
    last_mod = (award.get("Last Modified Date") or "")[:10]

    customer = sub_agency if sub_agency else agency
    if sub_agency and agency and sub_agency != agency:
        customer = f"{sub_agency} ({agency})"

    amount_str = f"${amount:,.0f}" if amount else "an undisclosed value"
    pop = f" Period of performance: {start} through {end}." if start and end else ""
    typ = f" Contract type: {award_type}." if award_type else ""
    aid = f" Award ID: {award_id}." if award_id else ""
    mod = f" Last modified: {last_mod}." if last_mod else ""

    return (
        f"{recipient} was awarded a {amount_str} federal contract by {customer}. "
        f"Scope: {desc or 'description not provided'}.{pop}{typ}{aid}{mod} "
        f"(Source: USASpending.gov. This record may represent a modification or "
        f"new task order under an existing IDIQ rather than a fresh prime award.)"
    )


def fetch_recent_awards(watchlist: dict[str, Any], days_back: int = 2) -> tuple[list[ContractHit], str]:
    """Return (hits, source_label). One query per company using its search term;
    post-filter by alias with word boundaries to drop substring noise."""
    thresholds = watchlist.get("alert_thresholds", {})
    min_value = thresholds.get("min_contract_value_usd", 0)

    today = datetime.now(timezone.utc).date()
    award_start = today - timedelta(days=AWARD_WINDOW_DAYS)
    fresh_cutoff = today - timedelta(days=days_back)

    hits: list[ContractHit] = []
    seen_ids: set[str] = set()

    for company in watchlist["companies"]:
        search_term = company.get("usaspending_search_term") or company.get("aliases", [None])[0]
        if not search_term:
            continue
        try:
            results = _query(
                search_term,
                award_start.strftime("%Y-%m-%d"),
                today.strftime("%Y-%m-%d"),
            )
        except Exception as e:
            print(f"  [warn] USASpending query failed for {company['ticker']}: {e}")
            continue

        for r in results:
            matched_alias = _recipient_matches_company(r.get("Recipient Name", ""), company["aliases"])
            if not matched_alias:
                continue
            last_mod = (r.get("Last Modified Date") or "")[:10]
            if not last_mod or last_mod < fresh_cutoff.strftime("%Y-%m-%d"):
                continue
            try:
                amount = float(r.get("Award Amount") or 0)
            except (TypeError, ValueError):
                continue
            if amount < min_value:
                continue
            award_id = r.get("Award ID") or ""
            if award_id in seen_ids:
                continue
            seen_ids.add(award_id)

            gid = r.get("generated_internal_id") or ""
            source_url = f"https://www.usaspending.gov/award/{gid}" if gid else "https://www.usaspending.gov/"

            hits.append(
                ContractHit(
                    ticker=company["ticker"],
                    company_name=company["name"],
                    matched_alias=matched_alias,
                    paragraph=_synthesize_paragraph(r),
                    contract_value_usd=int(amount),
                    source_url=source_url,
                    announcement_date=last_mod,
                )
            )

    return hits, "USASpending"


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    import yaml

    here = Path(__file__).resolve().parent
    with open(here.parent / "references" / "watchlist.yaml") as f:
        wl = yaml.safe_load(f)

    days = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    print(f"[fetch] USASpending lookback {days} day(s)", file=sys.stderr)
    hits, source = fetch_recent_awards(wl, days_back=days)
    print(f"[fetch] {len(hits)} hit(s) from {source}", file=sys.stderr)
    print(json.dumps([h.__dict__ for h in hits], indent=2, default=str))
