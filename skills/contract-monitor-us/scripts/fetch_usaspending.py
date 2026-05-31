"""
Query USASpending.gov for federal contract awards matching any company on the
master watchlist. Cross-sector: Auto, IT, Defence, Pharma, Energy.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

USA_SPENDING_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
TIMEOUT = 30
AWARD_TYPE_CODES = ["A", "B", "C", "D"]  # contracts only (no grants/loans)
RESULT_PAGE_LIMIT = 100


def _query(recipient_term: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    body = {
        "filters": {
            "recipient_search_text": [recipient_term],
            "award_type_codes": AWARD_TYPE_CODES,
            "time_period": [
                {
                    "start_date": start_date,
                    "end_date": end_date,
                    "date_type": "last_modified_date",
                }
            ],
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


def _recipient_matches(recipient_name: str, aliases: list[str]) -> str | None:
    if not recipient_name:
        return None
    for alias in aliases:
        if re.search(rf"\b{re.escape(alias)}\b", recipient_name, re.IGNORECASE):
            return alias
    return None


def _synthesize_paragraph(award: dict[str, Any]) -> str:
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
    mod = f" Last modified in USASpending: {last_mod}." if last_mod else ""

    return (
        f"{recipient} was awarded a {amount_str} US federal contract by {customer}. "
        f"Scope: {desc or 'description not provided'}.{pop}{typ}{aid}{mod}"
    )


def fetch_recent_awards(watchlist: dict[str, Any], days_back: int = 2) -> list[dict[str, Any]]:
    thresholds = watchlist.get("alert_thresholds", {})
    min_value = thresholds.get("min_contract_value_usd", 0)

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days_back)

    hits: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for company in watchlist["companies"]:
        terms = company.get("search_terms") or [company["aliases"][0]]
        for term in terms:
            try:
                results = _query(
                    term,
                    start.strftime("%Y-%m-%d"),
                    today.strftime("%Y-%m-%d"),
                )
            except Exception as e:
                print(f"  [warn] USASpending query failed for {company['ticker']} term='{term}': {e}")
                continue
            for r in results:
                matched_alias = _recipient_matches(
                    r.get("Recipient Name", ""), company["aliases"]
                )
                if not matched_alias:
                    continue
                try:
                    amount = float(r.get("Award Amount") or 0)
                except (TypeError, ValueError):
                    continue
                if amount < min_value:
                    continue
                award_id = r.get("Award ID") or ""
                if not award_id or award_id in seen_ids:
                    continue
                seen_ids.add(award_id)

                gid = r.get("generated_internal_id") or ""
                source_url = (
                    f"https://www.usaspending.gov/award/{gid}"
                    if gid else "https://www.usaspending.gov/"
                )
                announcement_date = (r.get("Start Date") or "")[:10] or (r.get("Last Modified Date") or "")[:10]
                last_mod = (r.get("Last Modified Date") or "")[:10]

                hits.append({
                    "ticker": company["ticker"],
                    "company_name": company["name"],
                    "sector": company.get("sector", ""),
                    "country": company.get("country", ""),
                    "contract_id": award_id,
                    "paragraph": _synthesize_paragraph(r),
                    "contract_value_usd": int(amount),
                    "source_url": source_url,
                    "announcement_date": announcement_date,
                    "last_modified_date": last_mod,
                    "matched_alias": matched_alias,
                })

    return hits


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
    from runner import load_master_watchlist

    wl = load_master_watchlist()
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    print(f"[fetch] USASpending lookback {days} day(s)", file=sys.stderr)
    hits = fetch_recent_awards(wl, days_back=days)
    print(f"[fetch] {len(hits)} hit(s)", file=sys.stderr)
    print(json.dumps(hits, indent=2, default=str))
