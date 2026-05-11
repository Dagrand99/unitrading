"""
Fetch the latest US DoD daily contracts page via the public RSS feed (discovery)
and curl_cffi (article body) to bypass Akamai bot-detection on war.gov.

Returns the latest article text and a list of watchlist mentions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

RSS_URL = (
    "https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx"
    "?ContentType=400&Site=945&max=10"
)
IMPERSONATE = "chrome124"
TIMEOUT = 30


@dataclass
class ContractHit:
    ticker: str
    company_name: str
    matched_alias: str
    paragraph: str
    contract_value_usd: int | None
    source_url: str
    announcement_date: str


@dataclass
class FetchedArticle:
    announcement_date: str
    text: str
    article_url: str


def _http_get(url: str) -> str:
    r = cffi_requests.get(url, impersonate=IMPERSONATE, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def _list_rss_articles() -> list[dict[str, str]]:
    xml = _http_get(RSS_URL)
    soup = BeautifulSoup(xml, "lxml-xml")
    out: list[dict[str, str]] = []
    for item in soup.find_all("item"):
        title = item.title.get_text(strip=True) if item.title else ""
        link = item.link.get_text(strip=True) if item.link else ""
        pub_date = item.pubDate.get_text(strip=True) if item.pubDate else ""
        if not link:
            continue
        out.append({"title": title, "link": link, "pubDate": pub_date})
    return out


def _extract_date_from_title(title: str) -> str | None:
    """Parse 'Contracts for May 8, 2026' → '2026-05-08'."""
    m = re.search(r"for\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})", title)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%B %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _fetch_article_body(url: str) -> str:
    html = _http_get(url)
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("main") or soup.body
    if not main:
        raise RuntimeError(f"No <main> body in article {url}")
    return main.get_text(separator="\n", strip=True)


def fetch_latest_dod_page() -> FetchedArticle:
    """Return the most recent DoD daily-contracts article from the RSS feed."""
    items = _list_rss_articles()
    if not items:
        raise RuntimeError("DoD RSS feed returned no items")
    latest = items[0]
    announcement_date = _extract_date_from_title(latest["title"]) or datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")
    text = _fetch_article_body(latest["link"])
    return FetchedArticle(announcement_date=announcement_date, text=text, article_url=latest["link"])


_VALUE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*(billion|million|thousand)?",
    re.IGNORECASE,
)


def extract_contract_value(text: str) -> int | None:
    """Return the FIRST plausible USD value mentioned. DoD paragraphs lead with the
    contract's own value (e.g. 'is awarded a $113,760,906 ... contract'); later
    values are typically cumulative totals or per-task obligations."""
    m = _VALUE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    if unit == "billion":
        value *= 1_000_000_000
    elif unit == "million":
        value *= 1_000_000
    elif unit == "thousand":
        value *= 1_000
    return int(value)


_NAV_NOISE = {
    "Share", "Copy Link", "Email", "Facebook", "X", "LinkedIn", "WhatsApp",
    "|", "×", "Subscribe", "Contracts",
    "Subscribe to War.gov Products",
    "Choose which War.gov products you want delivered to your inbox.",
}

_CONTRACT_LEAD_RE = re.compile(
    r"^[A-Z][^,]{1,80},.+?(?:awarded|modif)",
    re.IGNORECASE,
)


def _split_paragraphs(text: str) -> list[str]:
    """The DoD article uses one line per contract paragraph (BeautifulSoup inserts
    newlines between <p> elements). Filter navigation noise and service-branch
    headers, keep only contract paragraphs."""
    out: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line in _NAV_NOISE:
            continue
        if line.startswith("Contracts for "):
            continue
        # Service-branch headers: all caps, short, no digits (ARMY, NAVY, etc.)
        if line == line.upper() and len(line) < 60 and not re.search(r"\d", line):
            continue
        # Real contract paragraphs are long, start with a capitalized company name
        if len(line) < 80:
            continue
        out.append(line)
    return out


def find_company_mentions(
    text: str,
    watchlist: dict[str, Any],
    announcement_date: str,
    source_url: str,
) -> list[ContractHit]:
    """Scan paragraphs for watchlist alias matches above the contract value floor."""
    thresholds = watchlist.get("alert_thresholds", {})
    min_value = thresholds.get("min_contract_value_usd", 0)

    hits: list[ContractHit] = []
    seen: set[tuple[str, str]] = set()

    for para in _split_paragraphs(text):
        for company in watchlist["companies"]:
            for alias in company["aliases"]:
                pattern = rf"\b{re.escape(alias)}\b"
                if re.search(pattern, para, re.IGNORECASE):
                    key = (company["ticker"], para[:120])
                    if key in seen:
                        break
                    contract_value = extract_contract_value(para)
                    if contract_value is None or contract_value < min_value:
                        break
                    hits.append(
                        ContractHit(
                            ticker=company["ticker"],
                            company_name=company["name"],
                            matched_alias=alias,
                            paragraph=para,
                            contract_value_usd=contract_value,
                            source_url=source_url,
                            announcement_date=announcement_date,
                        )
                    )
                    seen.add(key)
                    break
    return hits


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    import yaml

    here = Path(__file__).resolve().parent
    with open(here.parent / "references" / "watchlist.yaml") as f:
        watchlist = yaml.safe_load(f)

    article = fetch_latest_dod_page()
    print(
        f"[fetch] date={article.announcement_date} "
        f"chars={len(article.text)} url={article.article_url}",
        file=sys.stderr,
    )
    hits = find_company_mentions(article.text, watchlist, article.announcement_date, article.article_url)
    print(json.dumps([h.__dict__ for h in hits], indent=2, default=str))
