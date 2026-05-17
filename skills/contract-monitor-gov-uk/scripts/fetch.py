"""
Fetch UK Contracts Finder OCDS award releases for the last N days and match
suppliers against the master watchlist.

Endpoint: GET https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search
Filter:   stages=award, publishedFrom/publishedTo in ISO 8601.
Pagination: cursor-based via links.next.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

CF_URL = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
TIMEOUT = 30
PAGE_LIMIT = 100
MAX_PAGES = 50  # safety cap → up to 5000 releases per run


def _iter_release_packages(start_iso: str, end_iso: str):
    url = CF_URL
    params: dict[str, Any] | None = {
        "stages": "award",
        "publishedFrom": start_iso,
        "publishedTo": end_iso,
        "limit": PAGE_LIMIT,
    }
    for _ in range(MAX_PAGES):
        r = requests.get(url, params=params, timeout=TIMEOUT, headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
        yield data
        next_url = (data.get("links") or {}).get("next")
        if not next_url:
            return
        url = next_url
        params = None  # next_url contains its own params


def _match_supplier(suppliers: list[dict[str, Any]], companies: list[dict[str, Any]]) -> tuple[dict[str, Any], str, str] | None:
    """Return (company, matched_alias, supplier_name) or None."""
    for s in suppliers or []:
        name = (s.get("name") or "").strip()
        if not name:
            continue
        for c in companies:
            for alias in c.get("aliases", []):
                if re.search(rf"\b{re.escape(alias)}\b", name, re.IGNORECASE):
                    return c, alias, name
    return None


def _synthesize_paragraph(release: dict[str, Any], award: dict[str, Any], supplier_name: str, amount_usd: int | None) -> str:
    tender = release.get("tender") or {}
    title = tender.get("title") or award.get("title") or "Untitled UK contract"
    desc = (tender.get("description") or "").strip()
    if desc and len(desc) > 600:
        desc = desc[:600].rsplit(" ", 1)[0] + "…"
    buyer = (release.get("buyer") or {}).get("name") or "unspecified UK public body"
    award_date = (award.get("date") or release.get("date") or "")[:10]
    value = award.get("value") or tender.get("value") or {}
    amt = value.get("amount")
    cur = value.get("currency") or "GBP"
    v_str = f"{amt:,.0f} {cur}" if amt is not None else "value undisclosed"
    usd_str = f" (~${amount_usd:,} USD)" if amount_usd else ""
    ocid = release.get("ocid") or ""

    return (
        f"{supplier_name} was named the awarded supplier on a UK Contracts Finder "
        f"award notice published by {buyer}. Title: {title}. Value: {v_str}{usd_str}. "
        f"Award date: {award_date or 'n/a'}. OCID: {ocid}. "
        f"Scope: {desc or 'not provided'}"
    )


def fetch_recent_awards(watchlist: dict[str, Any], days_back: int = 2) -> list[dict[str, Any]]:
    fx_to_usd: dict[str, float] = watchlist.get("fx_to_usd", {})
    thresholds = watchlist.get("alert_thresholds", {})
    min_value_usd = thresholds.get("min_contract_value_usd", 0)
    companies = watchlist["companies"]

    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days_back)
    start_iso = start.strftime("%Y-%m-%dT00:00:00")
    end_iso = end.strftime("%Y-%m-%dT23:59:59")

    hits: list[dict[str, Any]] = []
    seen_ocids: set[str] = set()
    page_count = 0

    try:
        for pkg in _iter_release_packages(start_iso, end_iso):
            page_count += 1
            for release in pkg.get("releases", []):
                if "award" not in (release.get("tag") or []):
                    continue
                awards = release.get("awards") or []
                if not awards:
                    continue
                for award in awards:
                    match = _match_supplier(award.get("suppliers") or [], companies)
                    if not match:
                        continue
                    company, matched_alias, supplier_name = match

                    value = award.get("value") or {}
                    amt = value.get("amount")
                    cur = (value.get("currency") or "GBP").upper()
                    amount_usd: int | None = None
                    if amt is not None and cur in fx_to_usd:
                        try:
                            amount_usd = int(float(amt) * fx_to_usd[cur])
                        except (TypeError, ValueError):
                            amount_usd = None
                    if amount_usd and amount_usd < min_value_usd:
                        continue

                    ocid = release.get("ocid") or release.get("id") or ""
                    if not ocid or ocid in seen_ocids:
                        continue
                    seen_ocids.add(ocid)

                    release_date = (release.get("date") or "")[:10]
                    award_date = (award.get("date") or "")[:10] or release_date
                    notice_url = f"https://www.contractsfinder.service.gov.uk/Notice/{ocid}"

                    hits.append({
                        "ticker": company["ticker"],
                        "company_name": company["name"],
                        "sector": company.get("sector", ""),
                        "country": company.get("country", ""),
                        "contract_id": ocid,
                        "paragraph": _synthesize_paragraph(release, award, supplier_name, amount_usd),
                        "contract_value_usd": int(amount_usd or 0),
                        "source_url": notice_url,
                        "announcement_date": award_date,
                        "last_modified_date": release_date,
                        "matched_alias": matched_alias,
                    })
    except requests.HTTPError as e:
        print(f"  [warn] Contracts Finder page request failed at page {page_count + 1}: {e}")

    return hits


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
    from runner import load_master_watchlist

    wl = load_master_watchlist()
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    print(f"[fetch] gov.uk Contracts Finder lookback {days} day(s)", file=sys.stderr)
    hits = fetch_recent_awards(wl, days_back=days)
    print(f"[fetch] {len(hits)} hit(s)", file=sys.stderr)
    print(json.dumps(hits, indent=2, default=str))
