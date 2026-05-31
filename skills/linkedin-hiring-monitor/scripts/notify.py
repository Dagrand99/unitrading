"""
Resend email helper for linkedin-hiring-monitor.

Sends a single daily digest email summarizing job-posting activity per
company over the past 7 days, with a top "massive hiring" section
highlighting companies whose dedup'd 7-day count is at or above the
threshold (default 10).
"""
from __future__ import annotations

import html
import os
from datetime import datetime, timezone
from typing import Any

import requests

RESEND_URL = "https://api.resend.com/emails"
TIMEOUT = 30


def _h(v: Any) -> str:
    """Safely HTML-escape any value (bool/None/int → string first)."""
    return html.escape("" if v is None else str(v))


def _company_row_html(entry: dict[str, Any], max_titles: int = 5, highlight: bool = False) -> str:
    titles = entry.get("sample_titles", [])[:max_titles]
    title_html = "".join(
        f'<li><a href="{_h(t["url"])}">{_h(t["title"] or "(untitled)")}</a></li>'
        for t in titles
    )
    extra = ""
    if entry["total_count"] > max_titles:
        extra = f'<li style="color:#888;">… +{entry["total_count"] - max_titles} more</li>'

    # Unusual / massive hiring → red row.
    if highlight:
        row_bg = "background:#fff0f0;"
        text_color = "color:#b00020;"
        ticker_html = f'<strong style="{text_color}">{_h(entry["ticker"])}</strong> <span style="{text_color} font-size:11px;">●</span>'
        count_html = f'<strong style="{text_color} font-size:16px;">{entry["total_count"]}</strong>'
    else:
        row_bg = ""
        text_color = ""
        ticker_html = f'<strong>{_h(entry["ticker"])}</strong>'
        count_html = f'<strong>{entry["total_count"]}</strong>'

    return f"""
<tr style="{row_bg}">
  <td valign="top" style="padding:8px 12px; border-bottom:1px solid #eee;">
    {ticker_html} · {_h(entry['name'])}
    <br/><span style="color:#888; font-size:12px;">{_h(entry['sector'])} · {_h(entry['country'])}</span>
  </td>
  <td valign="top" style="padding:8px 12px; border-bottom:1px solid #eee; text-align:right; font-family:monospace; font-size:14px;">
    {count_html}<br/>
    <span style="color:#888; font-size:11px;">new: {entry['new_count']}</span>
  </td>
  <td valign="top" style="padding:8px 12px; border-bottom:1px solid #eee;">
    <ul style="margin:0; padding-left:18px; font-size:13px;">{title_html}{extra}</ul>
  </td>
</tr>"""


def _section(title: str, rows: list[dict[str, Any]], note: str = "", highlight: bool = False) -> str:
    if not rows:
        return ""
    body = "".join(_company_row_html(r, highlight=highlight) for r in rows)
    note_html = f'<p style="color:#666; font-size:12px; margin:4px 0 8px 0;">{_h(note)}</p>' if note else ""
    return f"""
<h3 style="margin:18px 0 4px 0;">{_h(title)}</h3>
{note_html}
<table cellpadding="0" cellspacing="0" border="0" style="width:100%; border-collapse:collapse; font-family:-apple-system,system-ui,sans-serif;">
  <thead>
    <tr style="background:#f5f5f5;">
      <th align="left" style="padding:6px 12px; font-size:12px; color:#555;">Company</th>
      <th align="right" style="padding:6px 12px; font-size:12px; color:#555;">7d / new</th>
      <th align="left" style="padding:6px 12px; font-size:12px; color:#555;">Sample postings</th>
    </tr>
  </thead>
  <tbody>{body}</tbody>
</table>"""


def send_digest(
    *,
    threshold: int,
    massive_hiring: list[dict[str, Any]],
    moderate: list[dict[str, Any]],
    quiet: list[dict[str, Any]],
    zero: list[dict[str, Any]],
    total_new: int,
    total_postings: int,
    run_started_at: str,
) -> dict[str, Any]:
    api_key = os.environ["RESEND_API_KEY"]
    to_addr = os.environ["NOTIFY_EMAIL_TO"]
    from_addr = os.environ.get(
        "NOTIFY_EMAIL_FROM",
        "LinkedIn Hiring Monitor <onboarding@resend.dev>",
    )

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n_massive = len(massive_hiring)
    if n_massive > 0:
        tickers = ", ".join(c["ticker"] for c in massive_hiring[:5])
        if n_massive > 5:
            tickers += f" +{n_massive - 5}"
        subject = (
            f"[LinkedIn Hiring · {date_str}] {n_massive} company(s) at "
            f"{threshold}+ postings — {tickers}"
        )
    else:
        subject = (
            f"[LinkedIn Hiring · {date_str}] No massive hiring (≥{threshold}); "
            f"{total_new} new postings across watchlist"
        )

    zero_html = ""
    if zero:
        zero_tickers = ", ".join(_h(c["ticker"]) for c in zero)
        zero_html = f"""
<h3 style="margin:18px 0 4px 0;">Quiet (0 postings in last 7 days)</h3>
<p style="color:#888; font-size:13px;">{zero_tickers}</p>"""

    body_html = f"""<!doctype html>
<html><body style="font-family:-apple-system,system-ui,sans-serif; max-width:840px; color:#222;">
<h2 style="margin:0 0 4px 0;">LinkedIn Hiring Digest — {date_str}</h2>
<p style="margin:0 0 16px 0; color:#666; font-size:13px;">
  Watchlist size: {len(massive_hiring) + len(moderate) + len(quiet) + len(zero)} ·
  Total postings (7d): <strong>{total_postings}</strong> ·
  New since last run: <strong>{total_new}</strong> ·
  Massive-hiring threshold: ≥{threshold} postings/company in 7 days
  <br/>Run started (UTC): {_h(run_started_at)}
</p>

{_section(f"⚠ Unusual hiring (≥{threshold} postings in last 7 days)", massive_hiring, "Highlighted in red — potential expansion or strategic build-out signal.", highlight=True)}
{_section("Moderate hiring (1–{} postings)".format(threshold - 1), moderate)}
{zero_html}

<hr style="border:none; border-top:1px solid #eee; margin:24px 0 8px 0;"/>
<p style="color:#888; font-size:12px;">
Generated by linkedin-hiring-monitor. Source: Google search of linkedin.com/jobs/view,
past-week recency filter, via SerpAPI. Counts reflect Google's index, not LinkedIn's
internal post-date — there may be a 0–48h indexing lag.
</p>
</body></html>"""

    r = requests.post(
        RESEND_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": from_addr,
            "to": [to_addr],
            "subject": subject,
            "html": body_html,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()
