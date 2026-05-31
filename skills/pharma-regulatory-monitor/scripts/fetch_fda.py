"""
Fetch FDA regulatory events for the pharma watchlist.

Three sub-sources are queried:

1. **Drugs@FDA (CDER)** — `https://api.fda.gov/drug/drugsfda.json`
   Application records by sponsor. `sponsor_name` is indexed as a single
   uppercase token (e.g. "NOVARTIS", "VERTEX"), so we use a curated
   `ticker → sponsor_tokens` map below. Each matching submission becomes
   one hit keyed by `<application_no>:<submission_type>:<submission_no>:<status>:<date>`.

   Note: Drugs@FDA covers CDER (small-molecule + some biologics) only.
   mRNA vaccines and many novel biologics are filed at CBER and do NOT
   appear here. The FDA press release RSS below catches them.

2. **Drug Enforcement (recalls)** — `https://api.fda.gov/drug/enforcement.json`
   Drug recalls by recalling firm. `recalling_firm` is mixed-case (e.g.
   "Novartis Consumer Health"), so we use a curated `ticker → firm_tokens`
   map keyed by the firm name root. Each recall becomes one hit keyed by
   `recall_number`.

3. **FDA Press Release RSS** — `https://www.fda.gov/.../press-releases/rss.xml`
   Catches CBER vaccine approvals, drug news, and warning letters for any
   watchlist company mentioned in the title or description. Matching is on
   the master watchlist aliases (word-boundary, case-insensitive).

openFDA is free; an `OPENFDA_API_KEY` raises the limit from 240/day to 240/min.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

DRUGSFDA_URL = "https://api.fda.gov/drug/drugsfda.json"
ENFORCEMENT_URL = "https://api.fda.gov/drug/enforcement.json"
FDA_RSS_URL = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"
TIMEOUT = 60
PAGE_LIMIT = 100

# Status codes we treat as material regulatory decisions. We skip administrative
# actions like withdrawal-of-submission or change-in-corporate-name.
APPROVAL_STATUSES = {"AP", "TA", "AP/AC"}

STATUS_DESCRIPTIONS = {
    "AP": "Approval",
    "TA": "Tentative Approval",
    "AP/AC": "Approval / Action",
}

# Per-ticker sponsor tokens for Drugs@FDA. `sponsor_name` is single-token
# UPPERCASE; we use unquoted Lucene tokens (with `*` wildcards) so they
# match all corporate-name variants ("VERTEX PHARMS", "VERTEX PHARMS INC").
# Tickers missing from this map have no CDER applications (vaccines etc).
FDA_DRUGSFDA_SPONSORS: dict[str, list[str]] = {
    "VRTX": ["VERTEX"],
    "ARWR": ["ARROWHEAD"],
    "GMAB.CO": ["GENMAB"],
    "EVT.DE": ["EVOTEC"],
    "NOVN.SW": ["NOVARTIS", "SANDOZ"],
    "ZEAL.CO": ["ZEALAND"],
}

# Per-ticker recalling firm name tokens for Drug Enforcement.
# `recalling_firm` is mixed-case; bare unquoted tokens act as case-insensitive
# substrings inside the indexed phrase.
FDA_ENFORCEMENT_FIRMS: dict[str, list[str]] = {
    "MRNA": ["Moderna"],
    "VRTX": ["Vertex"],
    "BNTX": ["BioNTech"],
    "ARWR": ["Arrowhead"],
    "BAVA.CO": ["Bavarian"],
    "VLA.PA": ["Valneva"],
    "GMAB.CO": ["Genmab"],
    "EVT.DE": ["Evotec"],
    "NOVN.SW": ["Novartis", "Sandoz"],
    "ZEAL.CO": ["Zealand"],
}


def _yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _parse_yyyymmdd(s: str) -> str:
    if not s or len(s) < 8:
        return ""
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def _match_company(name: str, companies: list[dict[str, Any]]) -> tuple[dict[str, Any], str] | None:
    if not name:
        return None
    for c in companies:
        for alias in c.get("aliases", []):
            if re.search(rf"\b{re.escape(alias)}\b", name, re.IGNORECASE):
                return c, alias
    return None


def _request(url: str, params: dict[str, Any]) -> dict[str, Any]:
    api_key = os.environ.get("OPENFDA_API_KEY")
    if api_key:
        params = {**params, "api_key": api_key}
    r = requests.get(url, params=params, timeout=TIMEOUT)
    # 404 from openFDA means "no results" — treat as empty payload.
    if r.status_code == 404:
        return {"results": []}
    r.raise_for_status()
    return r.json()


def _drugsfda_search(token: str, start: date, end: date) -> str:
    # IMPORTANT: use literal spaces here, not `+`. The requests library
    # URL-encodes literal `+` as `%2B`, which openFDA rejects with 500.
    return (
        f"sponsor_name:{token} AND "
        f"submissions.submission_status_date:[{_yyyymmdd(start)} TO {_yyyymmdd(end)}]"
    )


def _enforcement_search(token: str, start: date, end: date) -> str:
    return (
        f"recalling_firm:{token} AND "
        f"report_date:[{_yyyymmdd(start)} TO {_yyyymmdd(end)}]"
    )


def _fetch_drugsfda(ticker: str, start: date, end: date) -> list[dict[str, Any]]:
    tokens = FDA_DRUGSFDA_SPONSORS.get(ticker) or []
    out: list[dict[str, Any]] = []
    for token in tokens:
        try:
            payload = _request(
                DRUGSFDA_URL,
                {"search": _drugsfda_search(token, start, end), "limit": PAGE_LIMIT},
            )
        except Exception as e:
            print(f"  [warn] openFDA Drugs@FDA failed for {ticker} ({token}): {e}")
            continue
        out.extend(payload.get("results") or [])
    return out


def _drugsfda_hits(
    company: dict[str, Any],
    matched_alias: str,
    apps: list[dict[str, Any]],
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for app in apps:
        app_no = app.get("application_number") or ""
        sponsor = app.get("sponsor_name") or ""
        products = app.get("products") or []
        product_names = sorted({
            (p.get("brand_name") or (p.get("active_ingredients") or [{}])[0].get("name") or "").strip()
            for p in products if p
        })
        product_names = [p for p in product_names if p]
        product_str = ", ".join(product_names) or "unspecified product"

        for sub in app.get("submissions", []) or []:
            status = sub.get("submission_status") or ""
            if status not in APPROVAL_STATUSES:
                continue
            ssd = sub.get("submission_status_date") or ""
            ssd_iso = _parse_yyyymmdd(ssd)
            if not ssd_iso:
                continue
            try:
                ssd_date = datetime.strptime(ssd_iso, "%Y-%m-%d").date()
            except ValueError:
                continue
            if ssd_date < start or ssd_date > end:
                continue

            sub_type = sub.get("submission_type") or ""
            sub_no = sub.get("submission_number") or ""
            review_priority = sub.get("review_priority") or ""

            event_id = f"FDA-Drugs:{app_no}:{sub_type}:{sub_no}:{status}:{ssd_iso}"
            paragraph = (
                f"openFDA Drugs@FDA: {STATUS_DESCRIPTIONS.get(status, status)} action on "
                f"application {app_no} ({sub_type or 'submission'} {sub_no}) "
                f"sponsored by {sponsor}. Product(s): {product_str}. "
                f"Status date: {ssd_iso}. Review priority: {review_priority or 'standard'}. "
                f"This is the FDA's structured record of the application action; refer to the "
                f"Drugs@FDA page for the underlying approval order or letter."
            )

            source_url = (
                f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={app_no}"
                if app_no else "https://www.accessdata.fda.gov/scripts/cder/daf/"
            )

            out.append({
                "ticker": company["ticker"],
                "company_name": company["name"],
                "sector": company.get("sector", ""),
                "country": company.get("country", ""),
                "event_id": event_id,
                "event_type": "fda_approval" if status in {"AP", "AP/AC"} else "fda_tentative_approval",
                "event_subtype": status,
                "headline": f"FDA {STATUS_DESCRIPTIONS.get(status, status)}: {sponsor} — {product_str}",
                "paragraph": paragraph,
                "source_url": source_url,
                "announcement_date": ssd_iso,
                "last_modified_date": ssd_iso,
                "matched_alias": matched_alias,
            })
    return out


def _fetch_enforcement(ticker: str, start: date, end: date) -> list[dict[str, Any]]:
    tokens = FDA_ENFORCEMENT_FIRMS.get(ticker) or []
    out: list[dict[str, Any]] = []
    for token in tokens:
        try:
            payload = _request(
                ENFORCEMENT_URL,
                {"search": _enforcement_search(token, start, end), "limit": PAGE_LIMIT},
            )
        except Exception as e:
            print(f"  [warn] openFDA Enforcement failed for {ticker} ({token}): {e}")
            continue
        out.extend(payload.get("results") or [])
    return out


def _enforcement_hits(
    company: dict[str, Any],
    matched_alias: str,
    recalls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in recalls:
        firm = rec.get("recalling_firm") or ""
        classification = rec.get("classification") or "unknown"
        product = rec.get("product_description") or "unspecified product"
        if len(product) > 400:
            product = product[:400].rsplit(" ", 1)[0] + "…"
        reason = rec.get("reason_for_recall") or "no reason given"
        if len(reason) > 400:
            reason = reason[:400].rsplit(" ", 1)[0] + "…"
        report_date = _parse_yyyymmdd(rec.get("report_date") or "")
        init_date = _parse_yyyymmdd(rec.get("recall_initiation_date") or "")
        status = rec.get("status") or ""
        recall_no = rec.get("recall_number") or rec.get("event_id") or ""
        if not recall_no:
            continue

        event_id = f"FDA-Recall:{recall_no}"
        paragraph = (
            f"openFDA Drug Enforcement Report: {firm} initiated a {classification} drug recall "
            f"for \"{product}\". Reason: {reason}. "
            f"Recall initiation date: {init_date or 'n/a'}. Report date: {report_date or 'n/a'}. "
            f"Current status: {status or 'unknown'}."
        )

        out.append({
            "ticker": company["ticker"],
            "company_name": company["name"],
            "sector": company.get("sector", ""),
            "country": company.get("country", ""),
            "event_id": event_id,
            "event_type": "fda_recall",
            "event_subtype": classification,
            "headline": f"FDA Recall ({classification}): {firm}",
            "paragraph": paragraph,
            "source_url": "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts",
            "announcement_date": report_date or init_date,
            "last_modified_date": report_date or init_date,
            "matched_alias": matched_alias,
        })
    return out


def _parse_rss_date(s: str) -> str:
    if not s:
        return ""
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _fetch_fda_press_releases(
    companies: list[dict[str, Any]],
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    import feedparser  # lazy import

    try:
        r = requests.get(
            FDA_RSS_URL,
            headers={"User-Agent": "pharma-regulatory-monitor/1.0"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"  [warn] FDA press release RSS fetch failed: {e}")
        return []

    parsed = feedparser.parse(r.content)
    hits: list[dict[str, Any]] = []
    for entry in parsed.entries or []:
        title = getattr(entry, "title", "") or ""
        summary = getattr(entry, "summary", "") or ""
        link = getattr(entry, "link", "") or FDA_RSS_URL
        entry_id_raw = getattr(entry, "id", "") or link or title
        pub_struct = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        pub_date = ""
        if pub_struct:
            try:
                pub_date = datetime(*pub_struct[:6], tzinfo=timezone.utc).date().isoformat()
            except (TypeError, ValueError):
                pub_date = ""
        if not pub_date:
            pub_date = _parse_rss_date(getattr(entry, "published", "") or "")

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

        plain_summary = re.sub(r"<[^>]+>", "", summary).strip()
        if len(plain_summary) > 700:
            plain_summary = plain_summary[:700].rsplit(" ", 1)[0] + "…"

        event_id = f"FDA-PR:{entry_id_raw[:200]}"
        paragraph = (
            f"FDA press release: \"{title}\". Published: {pub_date or 'unknown'}. "
            f"Matched alias: {matched_alias}. Summary: {plain_summary or 'no summary provided'}"
        )

        hits.append({
            "ticker": company["ticker"],
            "company_name": company["name"],
            "sector": company.get("sector", ""),
            "country": company.get("country", ""),
            "event_id": event_id,
            "event_type": "fda_press_release",
            "event_subtype": "news",
            "headline": title[:200],
            "paragraph": paragraph,
            "source_url": link,
            "announcement_date": pub_date,
            "last_modified_date": pub_date,
            "matched_alias": matched_alias,
        })
    return hits


def fetch_recent_events(watchlist: dict[str, Any], days_back: int = 3) -> list[dict[str, Any]]:
    companies = [c for c in watchlist["companies"] if c.get("sector") == "Pharma"]
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days_back)

    hits: list[dict[str, Any]] = []

    # ── Drugs@FDA approvals ──
    for c in companies:
        apps = _fetch_drugsfda(c["ticker"], start, end)
        for app in apps:
            m = _match_company(app.get("sponsor_name") or "", [c])
            if not m:
                continue
            hits.extend(_drugsfda_hits(c, m[1], [app], start, end))

    # ── Drug Enforcement recalls ──
    for c in companies:
        recalls = _fetch_enforcement(c["ticker"], start, end)
        for rec in recalls:
            m = _match_company(rec.get("recalling_firm") or "", [c])
            if not m:
                continue
            hits.extend(_enforcement_hits(c, m[1], [rec]))

    # ── FDA press releases (catches CBER vaccine approvals) ──
    hits.extend(_fetch_fda_press_releases(companies, start, end))

    return hits


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
    from runner import load_master_watchlist  # noqa: E402

    wl = load_master_watchlist()
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    print(f"[fetch] FDA lookback {days} day(s)", file=sys.stderr)
    out = fetch_recent_events(wl, days_back=days)
    print(f"[fetch] {len(out)} hit(s)", file=sys.stderr)
    print(json.dumps(out, indent=2, default=str))
