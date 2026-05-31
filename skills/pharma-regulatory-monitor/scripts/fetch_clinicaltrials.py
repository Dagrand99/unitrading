"""
Fetch ClinicalTrials.gov trial-status changes for the pharma watchlist via the
public v2 REST API.

Endpoint: GET https://clinicaltrials.gov/api/v2/studies

For each watchlist company, query studies sponsored or led by that company that
were updated within the lookback window. We emit a hit only for trial states
that historically move biotech stocks:

  COMPLETED               — readout pending / about to publish
  TERMINATED              — trial killed mid-flight (very high impact)
  WITHDRAWN               — trial pulled before enrollment
  SUSPENDED               — paused (often a safety pause; high impact)

Status changes between snapshots (e.g. Phase 2 → Phase 3) require a longitudinal
diff; we do not implement that here — the LLM is given the current snapshot and
the most-recent update date so it can reason about freshness.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

API_URL = "https://clinicaltrials.gov/api/v2/studies"
TIMEOUT = 60
PAGE_SIZE = 100

HIGH_SIGNAL_STATUSES = {
    "COMPLETED",
    "TERMINATED",
    "WITHDRAWN",
    "SUSPENDED",
}

# Phases we want to alert on — early-phase status changes are noisy for
# established biotechs. Phase 2/3 events are where stock impact lives.
HIGH_SIGNAL_PHASES = {"PHASE2", "PHASE3", "PHASE2_PHASE3", "PHASE4"}


def _match_company(name: str, companies: list[dict[str, Any]]) -> tuple[dict[str, Any], str] | None:
    if not name:
        return None
    for c in companies:
        for alias in c.get("aliases", []):
            if re.search(rf"\b{re.escape(alias)}\b", name, re.IGNORECASE):
                return c, alias
    return None


def _query_company(search_term: str, start: date, end: date) -> list[dict[str, Any]]:
    studies: list[dict[str, Any]] = []
    page_token: str | None = None
    safety_pages = 0
    while True:
        params: dict[str, Any] = {
            "query.spons": search_term,
            "filter.advanced": f"AREA[LastUpdatePostDate]RANGE[{start.isoformat()},{end.isoformat()}]",
            "pageSize": PAGE_SIZE,
            "format": "json",
        }
        if page_token:
            params["pageToken"] = page_token
        r = requests.get(API_URL, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        page = payload.get("studies") or []
        studies.extend(page)
        page_token = payload.get("nextPageToken")
        safety_pages += 1
        if not page_token or safety_pages >= 10:
            break
    return studies


def _study_to_hit(study: dict[str, Any], company: dict[str, Any], matched_alias: str) -> dict[str, Any] | None:
    proto = study.get("protocolSection") or {}
    ident = proto.get("identificationModule") or {}
    status_mod = proto.get("statusModule") or {}
    design = proto.get("designModule") or {}
    sponsor_mod = proto.get("sponsorCollaboratorsModule") or {}
    conditions_mod = proto.get("conditionsModule") or {}
    interventions_mod = proto.get("armsInterventionsModule") or {}

    nct_id = ident.get("nctId") or ""
    brief_title = ident.get("briefTitle") or "Untitled study"
    official_title = ident.get("officialTitle") or ""

    overall_status = (status_mod.get("overallStatus") or "").upper()
    last_update = status_mod.get("lastUpdatePostDateStruct", {}).get("date") or status_mod.get("lastUpdateSubmitDate") or ""
    primary_completion = status_mod.get("primaryCompletionDateStruct", {}).get("date") or ""
    why_stopped = status_mod.get("whyStopped") or ""

    phases = design.get("phases") or []
    phase_str = ",".join(phases) if phases else "n/a"
    study_type = design.get("studyType") or ""

    lead_sponsor = (sponsor_mod.get("leadSponsor") or {}).get("name") or ""

    conditions = ", ".join((conditions_mod.get("conditions") or [])[:4]) or "n/a"
    interventions = ", ".join(
        [(iv.get("name") or "").strip() for iv in (interventions_mod.get("interventions") or [])[:4] if iv.get("name")]
    ) or "n/a"

    if overall_status not in HIGH_SIGNAL_STATUSES:
        return None
    if phases and not (set(phases) & HIGH_SIGNAL_PHASES):
        return None

    event_id = f"CT:{nct_id}:{overall_status}:{last_update or 'unknown'}"
    headline = f"ClinicalTrials.gov {overall_status.title()}: {lead_sponsor} — {brief_title[:120]}"

    paragraph = (
        f"ClinicalTrials.gov status change: trial {nct_id} sponsored by {lead_sponsor} is now "
        f"{overall_status}. Brief title: \"{brief_title}\". "
        f"Phase(s): {phase_str}. Study type: {study_type or 'n/a'}. "
        f"Indication(s): {conditions}. Intervention(s): {interventions}. "
        f"Primary completion date: {primary_completion or 'n/a'}. "
        f"Last update posted: {last_update or 'n/a'}. "
    )
    if why_stopped:
        paragraph += f"Reason recorded for early stop / suspension: \"{why_stopped}\". "
    if official_title and official_title != brief_title:
        paragraph += f"Official title: \"{official_title[:300]}\"."

    source_url = f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else "https://clinicaltrials.gov"

    return {
        "ticker": company["ticker"],
        "company_name": company["name"],
        "sector": company.get("sector", ""),
        "country": company.get("country", ""),
        "event_id": event_id,
        "event_type": "trial_status_change",
        "event_subtype": overall_status,
        "headline": headline,
        "paragraph": paragraph,
        "source_url": source_url,
        "announcement_date": last_update or primary_completion or "",
        "last_modified_date": last_update or "",
        "matched_alias": matched_alias,
    }


def fetch_recent_events(watchlist: dict[str, Any], days_back: int = 7) -> list[dict[str, Any]]:
    companies = [c for c in watchlist["companies"] if c.get("sector") == "Pharma"]
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days_back)

    hits: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for c in companies:
        terms = c.get("search_terms") or [c["name"]]
        studies: list[dict[str, Any]] = []
        for term in terms:
            try:
                studies.extend(_query_company(term, start, end))
            except Exception as e:
                print(f"  [warn] ClinicalTrials.gov query failed for {c['name']} ({term}): {e}")

        for s in studies:
            proto = s.get("protocolSection") or {}
            lead_sponsor = (proto.get("sponsorCollaboratorsModule", {}).get("leadSponsor") or {}).get("name") or ""
            match = _match_company(lead_sponsor, [c])
            if not match:
                continue
            hit = _study_to_hit(s, c, match[1])
            if not hit:
                continue
            if hit["event_id"] in seen_ids:
                continue
            seen_ids.add(hit["event_id"])
            hits.append(hit)

    return hits


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
    from runner import load_master_watchlist  # noqa: E402

    wl = load_master_watchlist()
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    print(f"[fetch] ClinicalTrials.gov lookback {days} day(s)", file=sys.stderr)
    out = fetch_recent_events(wl, days_back=days)
    print(f"[fetch] {len(out)} hit(s)", file=sys.stderr)
    print(json.dumps(out, indent=2, default=str))
