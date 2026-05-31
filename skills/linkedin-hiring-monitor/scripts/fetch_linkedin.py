"""
SerpAPI-backed LinkedIn job-posting fetcher.

LinkedIn does not expose a free public jobs API and aggressively rate-limits
unauthenticated scrapers. We side-step both by querying Google (via SerpAPI)
scoped to `site:linkedin.com/jobs/view` URLs with the `qdr:w` (past-week)
recency filter. Each `organic_results[]` entry is a LinkedIn job listing
indexed by Google in the last 7 days.

Returns one normalized dict per posting:
    {
      "company_ticker": "INTC",
      "company_name":   "Intel",
      "sector":         "IT",
      "country":        "US",
      "search_term":    "Intel Corporation",
      "title":          "Senior Software Engineer - Foundry Services",
      "url":            "https://www.linkedin.com/jobs/view/3812345678",
      "snippet":        "Intel Corporation · Hillsboro, OR · 2 days ago ...",
      "job_id":         "3812345678",            # extracted from URL
    }

Dedup downstream uses `job_id` (stable per LinkedIn posting). When the URL
isn't a /jobs/view/<id> form we fall back to the full URL as the dedup key.
"""
from __future__ import annotations

import os
import re
import sys
import time
from typing import Any
from urllib.parse import urlparse

import requests

SERPAPI_URL = "https://serpapi.com/search"
TIMEOUT = 60
MAX_RESULTS_PER_QUERY = 100  # Google caps practical depth around ~100
JOB_ID_RE = re.compile(r"/jobs/view/(\d+)")


class SerpAPIError(RuntimeError):
    pass


def _extract_job_id(url: str) -> str:
    m = JOB_ID_RE.search(url or "")
    if m:
        return m.group(1)
    # Fallback: use the path so dedup still works on non-canonical URLs.
    parsed = urlparse(url or "")
    return f"{parsed.netloc}{parsed.path}" or (url or "")


def _is_linkedin_job(url: str) -> bool:
    return "linkedin.com/jobs/view" in (url or "")


def _serpapi_search(query: str, api_key: str) -> list[dict[str, Any]]:
    """Single SerpAPI Google search with past-week recency filter."""
    params = {
        "engine": "google",
        "q": query,
        "tbs": "qdr:w",  # past 7 days
        "num": MAX_RESULTS_PER_QUERY,
        "hl": "en",
        "gl": "us",
        "api_key": api_key,
    }
    r = requests.get(SERPAPI_URL, params=params, timeout=TIMEOUT)
    if r.status_code == 429:
        raise SerpAPIError("SerpAPI rate-limited (HTTP 429) — quota likely exhausted")
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise SerpAPIError(f"SerpAPI error: {data['error']}")
    return data.get("organic_results", []) or []


def fetch_company_jobs(company: dict[str, Any], api_key: str) -> list[dict[str, Any]]:
    """Try each search term in order, return on the first one that yields hits."""
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for term in company.get("search_terms", []) or [company["name"]]:
        query = f'"{term}" site:linkedin.com/jobs/view'
        try:
            raw = _serpapi_search(query, api_key)
        except SerpAPIError as e:
            print(f"  [warn] {company['ticker']} ({term!r}): {e}", file=sys.stderr)
            continue
        except requests.RequestException as e:
            print(f"  [warn] {company['ticker']} ({term!r}) HTTP: {e}", file=sys.stderr)
            continue

        for r in raw:
            url = r.get("link") or ""
            if not _is_linkedin_job(url) or url in seen_urls:
                continue
            seen_urls.add(url)
            results.append({
                "company_ticker": company["ticker"],
                "company_name":   company["name"],
                "sector":         company["sector"],
                "country":        company["country"],
                "search_term":    term,
                "title":          (r.get("title") or "").strip(),
                "url":            url,
                "snippet":        (r.get("snippet") or "").strip(),
                "job_id":         _extract_job_id(url),
            })

        if results:
            # First term that returned matches wins — saves SerpAPI quota.
            break

        # Tiny pause between alt-term retries to be polite.
        time.sleep(0.3)

    return results


def fetch_all(watchlist: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Return {ticker: [job, ...]} for the full watchlist."""
    api_key = os.environ.get("SERPAPI_KEY") or os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        raise SerpAPIError(
            "SERPAPI_KEY env var is required (set it in the Trader environment)."
        )

    per_company: dict[str, list[dict[str, Any]]] = {}
    for c in watchlist:
        ticker = c["ticker"]
        try:
            jobs = fetch_company_jobs(c, api_key)
        except Exception as e:  # noqa: BLE001
            print(f"  [error] {ticker}: {e}", file=sys.stderr)
            jobs = []
        per_company[ticker] = jobs
        print(f"  {ticker:<10} {c['name']:<28} {len(jobs):>3} posting(s)", flush=True)
        # Small inter-company pause — SerpAPI rate limit is per-plan; this
        # keeps bursts well below the typical 100 req/min cap.
        time.sleep(0.4)
    return per_company


if __name__ == "__main__":
    # CLI smoke test:
    #   SERPAPI_KEY=... python scripts/fetch_linkedin.py
    import json
    from pathlib import Path

    import yaml

    wl_path = Path(__file__).resolve().parent.parent / "watchlist.yaml"
    wl = yaml.safe_load(wl_path.read_text())["companies"]
    out = fetch_all(wl)
    counts = {t: len(j) for t, j in out.items()}
    print(json.dumps(counts, indent=2))
