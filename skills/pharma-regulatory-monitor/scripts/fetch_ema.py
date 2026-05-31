"""
Fetch recent EMA news items via the public EMA news RSS feed and post-filter
by alias match against the pharma watchlist.

The EMA site does not expose a structured medicines-decision API; the most
practical machine-readable surface is its RSS news feed, which covers CHMP
positive opinions, marketing-authorization decisions, safety alerts, and
press releases.

Default feed URL:
  https://www.ema.europa.eu/en/news-rss

This URL changes occasionally as EMA re-platforms its site; override with the
EMA_NEWS_RSS_URL environment variable if needed.

If the feed cannot be reached (network error, 404, HTML response), the fetcher
returns an empty list and logs a warning — the rest of the routine proceeds.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

DEFAULT_FEED_URL = "https://www.ema.europa.eu/news.xml"
FALLBACK_FEED_URLS = (
    "https://www.ema.europa.eu/en/news-rss",
    "https://www.ema.europa.eu/en/rss/news_en.xml",
)
TIMEOUT = 30


def _match_company(text: str, companies: list[dict[str, Any]]) -> tuple[dict[str, Any], str] | None:
    if not text:
        return None
    for c in companies:
        for alias in c.get("aliases", []):
            if re.search(rf"\b{re.escape(alias)}\b", text, re.IGNORECASE):
                return c, alias
    return None


def _try_feed(url: str) -> list[dict[str, Any]]:
    import feedparser  # imported lazily so the rest of the skill works without it

    headers = {"User-Agent": "pharma-regulatory-monitor/1.0 (+https://github.com/Dagrand99/unitrading)"}
    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    return list(parsed.entries or [])


def _parse_entry_date(entry: Any) -> str:
    # feedparser exposes published_parsed and updated_parsed as time.struct_time
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None) or (entry.get(attr) if isinstance(entry, dict) else None)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc).date().isoformat()
            except (TypeError, ValueError):
                continue
    return ""


def fetch_recent_events(watchlist: dict[str, Any], days_back: int = 7) -> list[dict[str, Any]]:
    companies = [c for c in watchlist["companies"] if c.get("sector") == "Pharma"]
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days_back)

    feed_url = os.environ.get("EMA_NEWS_RSS_URL") or DEFAULT_FEED_URL
    entries: list[Any] = []
    tried: list[str] = []

    for url in [feed_url, *FALLBACK_FEED_URLS]:
        if url in tried:
            continue
        tried.append(url)
        try:
            entries = _try_feed(url)
            if entries:
                feed_url = url
                break
        except Exception as e:
            print(f"  [warn] EMA feed {url} failed: {e}")
            continue

    if not entries:
        print(f"  [warn] EMA: no entries from any feed URL ({tried}); skipping")
        return []

    hits: list[dict[str, Any]] = []
    for entry in entries:
        title = (entry.get("title") if isinstance(entry, dict) else getattr(entry, "title", "")) or ""
        summary = (entry.get("summary") if isinstance(entry, dict) else getattr(entry, "summary", "")) or ""
        link = (entry.get("link") if isinstance(entry, dict) else getattr(entry, "link", "")) or feed_url
        entry_id_raw = (entry.get("id") if isinstance(entry, dict) else getattr(entry, "id", "")) or link or title
        pub_date = _parse_entry_date(entry)

        if pub_date:
            try:
                d = datetime.strptime(pub_date, "%Y-%m-%d").date()
                if d < start or d > end:
                    continue
            except ValueError:
                pass

        haystack = f"{title} {summary}"
        match = _match_company(haystack, companies)
        if not match:
            continue
        company, matched_alias = match

        event_id = f"EMA:{entry_id_raw[:200]}"
        plain_summary = re.sub(r"<[^>]+>", "", summary).strip()
        if len(plain_summary) > 700:
            plain_summary = plain_summary[:700].rsplit(" ", 1)[0] + "…"

        paragraph = (
            f"EMA news feed entry: \"{title}\". "
            f"Published: {pub_date or 'unknown'}. Matched alias: {matched_alias}. "
            f"Summary: {plain_summary or 'no summary provided'}"
        )

        hits.append({
            "ticker": company["ticker"],
            "company_name": company["name"],
            "sector": company.get("sector", ""),
            "country": company.get("country", ""),
            "event_id": event_id,
            "event_type": "ema_news",
            "event_subtype": "rss",
            "headline": title[:200],
            "paragraph": paragraph,
            "source_url": link,
            "announcement_date": pub_date,
            "last_modified_date": pub_date,
            "matched_alias": matched_alias,
        })

    return hits


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
    from runner import load_master_watchlist  # noqa: E402

    wl = load_master_watchlist()
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    print(f"[fetch] EMA lookback {days} day(s)", file=sys.stderr)
    out = fetch_recent_events(wl, days_back=days)
    print(f"[fetch] {len(out)} hit(s)", file=sys.stderr)
    print(json.dumps(out, indent=2, default=str))
