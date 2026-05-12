"""
Fetch recently-modified federal contract awards to pharma watchlist recipients
via USASpending.gov. Used for HHS, BARDA, DoD/DTRA, and other federal
procurement deals which are the highest-impact contract events for biotech
stocks (e.g. Moderna mRNA-1273 BARDA, Bavarian Nordic Mpox/smallpox).

Differs from the US Defense version in one important way:
  announcement_date = award Start Date  (when the contract was signed / went
  into effect — typically when the press release dropped). NOT Last Modified
  Date, which reflects when USASpending received the record and may be 1-4
  weeks after the actual announcement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

USA_SPENDING_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
TIMEOUT = 30
AWARD_TYPE_CODES = ["A", "B", "C", "D"]  # contracts (excludes grants/loans)
RESULT_PAGE_LIMIT = 100


@dataclass
class PharmaContractHit:
    ticker: str
    company_name: str
    matched_alias: str
    paragraph: str
    contract_value_usd: int
    source_url: str
    announcement_date: str  # award Start Date
    award_id: str
    last_modified_date: str  # when USASpending recorded it


def _query(recipient_term: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    body = {
        "filters": {
            "recipient_search_text": [recipient_term],
            "award_type_codes": AWARD_TYPE_CODES,
            # Filter on LAST MODIFIED DATE, not action_date — pharma awards are
            # often modifications to existing IDIQs, so action_date can be years
            # old while the modification (extra funding / option exercise) is
            # what we want to catch.
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


def _recipient_matches_company(recipient_name: str, aliases: list[str]) -> str | None:
    """Word-boundary case-insensitive match. Returns matched alias or None."""
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
        f"{recipient} was awarded a {amount_str} federal contract by {customer}. "
        f"Scope: {desc or 'description not provided'}.{pop}{typ}{aid}{mod} "
        f"(Source: USASpending.gov. Federal procurement records — may be a new "
        f"award, an option exercise, or a modification adding obligated funds.)"
    )


def fetch_recent_awards(watchlist: dict[str, Any], days_back: int = 2) -> list[PharmaContractHit]:
    """Return awards where:
      - Last Modified Date is within the last `days_back` days (API-side filter)
      - Recipient name matches a watchlist alias on a word boundary
      - Award Amount >= min_contract_value_usd
    """
    thresholds = watchlist.get("alert_thresholds", {})
    min_value = thresholds.get("min_contract_value_usd", 0)

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days_back)

    hits: list[PharmaContractHit] = []
    seen_ids: set[str] = set()

    for company in watchlist["companies"]:
        search_term = company.get("usaspending_search_term") or company.get("aliases", [None])[0]
        if not search_term:
            continue
        try:
            results = _query(
                search_term,
                start.strftime("%Y-%m-%d"),
                today.strftime("%Y-%m-%d"),
            )
        except Exception as e:
            print(f"  [warn] USASpending query failed for {company['ticker']}: {e}")
            continue

        for r in results:
            matched_alias = _recipient_matches_company(
                r.get("Recipient Name", ""), company["aliases"]
            )
            if not matched_alias:
                continue
            last_mod = (r.get("Last Modified Date") or "")[:10]
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
            source_url = (
                f"https://www.usaspending.gov/award/{gid}" if gid else "https://www.usaspending.gov/"
            )

            # Use Start Date (contract effective date) as announcement anchor,
            # not Last Modified Date. Falls back to last_mod if Start Date missing.
            announcement_date = (r.get("Start Date") or "")[:10] or last_mod

            hits.append(
                PharmaContractHit(
                    ticker=company["ticker"],
                    company_name=company["name"],
                    matched_alias=matched_alias,
                    paragraph=_synthesize_paragraph(r),
                    contract_value_usd=int(amount),
                    source_url=source_url,
                    announcement_date=announcement_date,
                    award_id=award_id,
                    last_modified_date=last_mod,
                )
            )

    return hits


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    import yaml

    here = Path(__file__).resolve().parent
    with open(here.parent / "references" / "watchlist.yaml") as f:
        wl = yaml.safe_load(f)

    days = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    min_v = wl.get("alert_thresholds", {}).get("min_contract_value_usd", 0)
    print(f"[fetch] USASpending lookback {days} day(s) on Last Modified Date", file=sys.stderr)
    hits = fetch_recent_awards(wl, days_back=days)
    print(f"[fetch] {len(hits)} hit(s) above ${min_v:,} threshold", file=sys.stderr)
    print(json.dumps([h.__dict__ for h in hits], indent=2, default=str))
