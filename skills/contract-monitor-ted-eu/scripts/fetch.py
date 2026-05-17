"""
Fetch TED Contract Award Notices (can-standard) for every company on the master
watchlist. Cross-sector — Auto, IT, Defence, Pharma, Energy.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

TED_URL = "https://api.ted.europa.eu/v3/notices/search"
TIMEOUT = 30
PAGE_LIMIT = 250
INTER_REQUEST_SLEEP = 3.0  # seconds; TED throttles aggressively for anon callers
MAX_RETRIES = 3
COMPANIES_PER_QUERY = 5  # batch this many companies into one OR query


def _ted_search(query: str, fields: list[str], limit: int = PAGE_LIMIT) -> list[dict[str, Any]]:
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.post(
                TED_URL,
                json={
                    "query": query,
                    "fields": fields,
                    "limit": limit,
                    "paginationMode": "PAGE_NUMBER",
                    "page": 1,
                },
                timeout=TIMEOUT,
            )
            if r.status_code == 429:
                wait = INTER_REQUEST_SLEEP * (attempt + 2)
                print(f"  [warn] TED 429 — backing off {wait}s (attempt {attempt+1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json().get("notices", [])
        except requests.HTTPError as e:
            last_err = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(INTER_REQUEST_SLEEP * (attempt + 1))
                continue
            raise
    if last_err:
        raise last_err
    return []


def _pick_localized(
    value: Any,
    prefer: tuple[str, ...] = ("eng", "ita", "deu", "fra", "spa", "swe", "pol", "por", "dan", "fin", "nld"),
) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        for lang in prefer:
            if lang in value and value[lang]:
                v = value[lang]
                if isinstance(v, list):
                    return ", ".join(str(x) for x in v)
                return str(v)
        for v in value.values():
            if v:
                if isinstance(v, list):
                    return ", ".join(str(x) for x in v)
                return str(v)
    return str(value)


def _normalize(notice: dict[str, Any], company: dict[str, Any], fx_to_usd: dict[str, float]) -> dict[str, Any] | None:
    winner_raw = notice.get("winner-name") or {}
    winner_str = _pick_localized(winner_raw)
    if not winner_str:
        return None

    matched_alias = None
    for alias in company.get("aliases", []):
        if alias.lower() in winner_str.lower():
            matched_alias = alias
            break
    if matched_alias is None:
        return None  # avoid false positives — require alias hit

    pub_date_raw = notice.get("publication-date") or ""
    pub_date = pub_date_raw[:10] if len(pub_date_raw) >= 10 else pub_date_raw

    value = notice.get("total-value")
    currency_list = notice.get("total-value-cur") or []
    currency = currency_list[0] if isinstance(currency_list, list) and currency_list else None
    usd_value: int | None = None
    if value is not None and currency:
        rate = fx_to_usd.get(currency.upper())
        if rate:
            try:
                usd_value = int(float(value) * rate)
            except (TypeError, ValueError):
                usd_value = None

    title = _pick_localized(notice.get("notice-title"))
    contract_title = _pick_localized(notice.get("BT-721-Contract"))
    combined_title = " — ".join(t for t in [title, contract_title] if t)

    buyer = _pick_localized(notice.get("buyer-name"))
    pub_num = notice.get("publication-number") or ""
    notice_url = (
        notice.get("links", {}).get("html", {}).get("ENG")
        or notice.get("links", {}).get("html", {}).get("MUL")
        or f"https://ted.europa.eu/en/notice/-/detail/{pub_num}"
    )

    v_str = (
        f"{value:,.0f} {currency}" if value is not None and currency else "value undisclosed"
    )
    usd_str = f" (~${usd_value:,} USD)" if usd_value else ""
    paragraph = (
        f"{winner_str} won a TED-published EU public-procurement contract awarded by "
        f"{buyer}. Title: {combined_title}. Value: {v_str}{usd_str}. "
        f"Publication number {pub_num} dated {pub_date}."
    )

    return {
        "ticker": company["ticker"],
        "company_name": company["name"],
        "sector": company.get("sector", ""),
        "country": company.get("country", ""),
        "contract_id": pub_num,
        "paragraph": paragraph,
        "contract_value_usd": int(usd_value or 0),
        "source_url": notice_url,
        "announcement_date": pub_date,
        "last_modified_date": pub_date,  # TED doesn't expose a separate mod date
        "matched_alias": matched_alias,
    }


def fetch_recent_awards(watchlist: dict[str, Any], days_back: int = 2) -> list[dict[str, Any]]:
    fx_to_usd: dict[str, float] = watchlist.get("fx_to_usd", {})
    thresholds = watchlist.get("alert_thresholds", {})
    min_value_usd = thresholds.get("min_contract_value_usd", 0)

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days_back)
    start_str = start.strftime("%Y%m%d")

    fields = [
        "publication-number",
        "publication-date",
        "notice-type",
        "notice-title",
        "buyer-name",
        "winner-name",
        "total-value",
        "total-value-cur",
        "BT-721-Contract",
        "links",
    ]

    hits: list[dict[str, Any]] = []
    seen_pubs: set[str] = set()
    companies = watchlist["companies"]

    # Batch companies into chunks; each chunk issues ONE TED query with all
    # aliases OR'd together. Result rows are then routed back to the right
    # company via the existing alias-match in _normalize().
    for i in range(0, len(companies), COMPANIES_PER_QUERY):
        chunk = companies[i:i + COMPANIES_PER_QUERY]
        alias_clauses: list[str] = []
        for c in chunk:
            for a in c.get("aliases", [])[:3]:  # cap aliases/company to limit query length
                alias_clauses.append(f'winner-name="{a}"')
        if not alias_clauses:
            continue
        query = (
            f"({' OR '.join(alias_clauses)}) "
            f"AND publication-date>={start_str} "
            f"AND notice-type=can-standard"
        )
        try:
            notices = _ted_search(query, fields)
        except requests.HTTPError as e:
            print(f"  [warn] TED chunk {i//COMPANIES_PER_QUERY} failed: {e}")
            time.sleep(INTER_REQUEST_SLEEP)
            continue

        for notice in notices:
            for company in chunk:
                hit = _normalize(notice, company, fx_to_usd)
                if hit is None:
                    continue
                if hit["contract_value_usd"] and hit["contract_value_usd"] < min_value_usd:
                    break
                if hit["contract_id"] in seen_pubs:
                    break
                seen_pubs.add(hit["contract_id"])
                hits.append(hit)
                break  # one company match per notice is enough

        time.sleep(INTER_REQUEST_SLEEP)

    return hits


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
    from runner import load_master_watchlist

    wl = load_master_watchlist()
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    print(f"[fetch] TED lookback {days} day(s)", file=sys.stderr)
    hits = fetch_recent_awards(wl, days_back=days)
    print(f"[fetch] {len(hits)} hit(s)", file=sys.stderr)
    print(json.dumps(hits, indent=2, default=str))
