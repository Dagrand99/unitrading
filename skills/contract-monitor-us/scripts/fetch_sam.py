"""
Fetch SAM.gov Award Notices (ptype=a) for the last N days, then post-filter
by alias match against the master watchlist.

Endpoint: GET https://api.sam.gov/opportunities/v2/search
Date format: MM/dd/yyyy
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

SAM_URL = "https://api.sam.gov/opportunities/v2/search"
TIMEOUT = 60
PAGE_LIMIT = 1000


def _query(start_mmddyyyy: str, end_mmddyyyy: str, offset: int = 0) -> dict[str, Any]:
    api_key = os.environ["SAM_API_KEY"]
    r = requests.get(
        SAM_URL,
        params={
            "api_key": api_key,
            "postedFrom": start_mmddyyyy,
            "postedTo": end_mmddyyyy,
            "ptype": "a",  # Award Notice
            "limit": PAGE_LIMIT,
            "offset": offset,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _awardee_name(notice: dict[str, Any]) -> str:
    award = notice.get("award") or {}
    awardee = award.get("awardee") or {}
    if isinstance(awardee, dict):
        return awardee.get("name") or ""
    # Some payloads return awardee as a string
    return str(awardee or "")


def _award_amount(notice: dict[str, Any]) -> float:
    award = notice.get("award") or {}
    val = award.get("amount")
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _match_company(awardee: str, companies: list[dict[str, Any]]) -> tuple[dict[str, Any], str] | None:
    if not awardee:
        return None
    for c in companies:
        for alias in c.get("aliases", []):
            if re.search(rf"\b{re.escape(alias)}\b", awardee, re.IGNORECASE):
                return c, alias
    return None


def _synthesize_paragraph(notice: dict[str, Any], awardee: str, amount: float) -> str:
    title = notice.get("title") or "Untitled"
    desc = (notice.get("description") or "").strip()
    if desc and len(desc) > 600:
        desc = desc[:600].rsplit(" ", 1)[0] + "…"
    posted = (notice.get("postedDate") or "")[:10]
    naics = notice.get("naicsCode") or ""
    classif = notice.get("classificationCode") or ""
    office = notice.get("fullParentPathName") or notice.get("officeAddress", {}).get("name") or ""
    sol = notice.get("solicitationNumber") or ""
    award = notice.get("award") or {}
    award_no = award.get("number") or ""
    award_date = award.get("date") or ""
    amount_str = f"${amount:,.0f}" if amount else "an undisclosed value"

    return (
        f"{awardee} was named the awardee on SAM.gov Award Notice \"{title}\" "
        f"by {office or 'unspecified contracting office'}. Award amount: {amount_str}. "
        f"Posted: {posted}. Award date: {award_date or 'n/a'}. "
        f"Solicitation: {sol or 'n/a'}. Award number: {award_no or 'n/a'}. "
        f"NAICS: {naics or 'n/a'}. PSC: {classif or 'n/a'}. "
        f"Description: {desc or 'not provided'}"
    )


def fetch_recent_awards(watchlist: dict[str, Any], days_back: int = 1) -> list[dict[str, Any]]:
    thresholds = watchlist.get("alert_thresholds", {})
    min_value = thresholds.get("min_contract_value_usd", 0)
    companies = watchlist["companies"]

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days_back)
    start_str = start.strftime("%m/%d/%Y")
    end_str = end.strftime("%m/%d/%Y")

    hits: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    offset = 0
    while True:
        try:
            payload = _query(start_str, end_str, offset=offset)
        except Exception as e:
            print(f"  [warn] SAM.gov query failed at offset={offset}: {e}")
            break

        notices = payload.get("opportunitiesData") or payload.get("data") or []
        if not notices:
            break

        for n in notices:
            awardee = _awardee_name(n)
            match = _match_company(awardee, companies)
            if not match:
                continue
            company, matched_alias = match

            amount = _award_amount(n)
            if amount and amount < min_value:
                continue

            notice_id = n.get("noticeId") or n.get("solicitationNumber") or ""
            if not notice_id or notice_id in seen_ids:
                continue
            seen_ids.add(notice_id)

            posted = (n.get("postedDate") or "")[:10]
            award = n.get("award") or {}
            award_date = (award.get("date") or "")[:10] or posted
            ui_link = n.get("uiLink") or f"https://sam.gov/opp/{notice_id}/view"

            hits.append({
                "ticker": company["ticker"],
                "company_name": company["name"],
                "sector": company.get("sector", ""),
                "country": company.get("country", ""),
                "contract_id": notice_id,
                "paragraph": _synthesize_paragraph(n, awardee, amount),
                "contract_value_usd": int(amount),
                "source_url": ui_link,
                "announcement_date": award_date,
                "last_modified_date": posted,  # SAM.gov v2 doesn't expose a separate modifiedDate
                "matched_alias": matched_alias,
            })

        total = int(payload.get("totalRecords", 0) or 0)
        offset += len(notices)
        if offset >= total or len(notices) < PAGE_LIMIT:
            break

    return hits


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
    from runner import load_master_watchlist

    wl = load_master_watchlist()
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    print(f"[fetch] SAM.gov lookback {days} day(s)", file=sys.stderr)
    hits = fetch_recent_awards(wl, days_back=days)
    print(f"[fetch] {len(hits)} hit(s)", file=sys.stderr)
    print(json.dumps(hits, indent=2, default=str))
