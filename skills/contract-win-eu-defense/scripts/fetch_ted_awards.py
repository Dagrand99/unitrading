"""
Fetch recent contract-award notices from TED (Tenders Electronic Daily) for the
EU defense watchlist via the public v3 search API.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

TED_URL = "https://api.ted.europa.eu/v3/notices/search"
TIMEOUT = 30
PAGE_LIMIT = 25


@dataclass
class TedAwardHit:
    ticker: str
    company_name: str
    matched_winner_name: str
    publication_number: str
    publication_date: str  # YYYY-MM-DD
    title: str
    buyer_name: str
    contract_value_native: float | None
    contract_currency: str | None
    contract_value_usd: int | None
    notice_url: str

    def short_text(self) -> str:
        v = (
            f"{self.contract_value_native:,.0f} {self.contract_currency}"
            if self.contract_value_native is not None
            else "value undisclosed"
        )
        usd = f" (~${self.contract_value_usd:,} USD)" if self.contract_value_usd else ""
        return (
            f"{self.matched_winner_name} won a TED-notified contract awarded by "
            f"{self.buyer_name}. Title: {self.title}. Value: {v}{usd}. "
            f"Publication number {self.publication_number} dated {self.publication_date}. "
            f"Source: {self.notice_url}"
        )


def _ted_search(query: str, fields: list[str], limit: int = PAGE_LIMIT) -> list[dict[str, Any]]:
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
    r.raise_for_status()
    return r.json().get("notices", [])


def _pick_localized(value: Any, prefer: tuple[str, ...] = ("eng", "ita", "deu", "fra", "spa", "swe", "pol")) -> str:
    """TED returns multilingual fields as {lang_code: text-or-list}. Pick the
    preferred language present."""
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


def _normalize(notice: dict[str, Any], company: dict[str, Any], fx_to_usd: dict[str, float]) -> TedAwardHit | None:
    winner_raw = notice.get("winner-name") or {}
    winner_str = _pick_localized(winner_raw)
    if not winner_str:
        return None

    matched_alias = None
    for alias in company.get("winner_names", []):
        if alias.lower() in winner_str.lower():
            matched_alias = alias
            break
    if matched_alias is None:
        matched_alias = winner_str

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

    return TedAwardHit(
        ticker=company["ticker"],
        company_name=company["name"],
        matched_winner_name=matched_alias,
        publication_number=pub_num,
        publication_date=pub_date,
        title=combined_title,
        buyer_name=buyer,
        contract_value_native=float(value) if value is not None else None,
        contract_currency=currency,
        contract_value_usd=usd_value,
        notice_url=notice_url,
    )


def fetch_recent_awards(
    watchlist: dict[str, Any], days_back: int = 2
) -> list[TedAwardHit]:
    """Query TED award notices for each watchlist company over the last N days."""
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

    hits: list[TedAwardHit] = []
    for company in watchlist["companies"]:
        winners = company.get("winner_names", [])
        if not winners:
            continue
        winner_clause = " OR ".join(f'winner-name="{w}"' for w in winners)
        query = (
            f"({winner_clause}) "
            f"AND publication-date>={start_str} "
            f"AND notice-type=can-standard"
        )
        try:
            notices = _ted_search(query, fields)
        except requests.HTTPError as e:
            print(f"  [warn] TED query failed for {company['ticker']}: {e}")
            continue
        for notice in notices:
            hit = _normalize(notice, company, fx_to_usd)
            if hit is None:
                continue
            if hit.contract_value_usd is not None and hit.contract_value_usd < min_value_usd:
                continue
            hits.append(hit)

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
    print(f"[fetch] looking back {days} day(s)", file=sys.stderr)
    hits = fetch_recent_awards(wl, days_back=days)
    print(f"[fetch] {len(hits)} hit(s) above threshold", file=sys.stderr)
    print(json.dumps([h.__dict__ for h in hits], indent=2, default=str))
