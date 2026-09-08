"""NIH grant count + cumulative award total per org, via NIH RePORTER's
API v2 (https://api.reporter.nih.gov) — free, keyless, no registration.

Complements funding_total_usd (which only ever gets populated from
funding-round NEWS coverage, i.e. essentially only industry orgs) with a
source that's actually a good fit for the academic/government/nonprofit
org_types funding-round coverage never touches. Same refresh-on-a-cadence
shape as clinicaltrials_scraper.py, and the same honest caveat: NIH
RePORTER's own organization-name strings don't always match how an org
name got captured from a news article, so coverage here will be partial,
not comprehensive — a real match still counts as real signal, a miss
just means "not found," not "no grants."
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from ..db import DB

NIH_REPORTER_API = "https://api.reporter.nih.gov/v2/projects/search"
MAX_RECORDS_PER_ORG = 500  # NIH RePORTER's own per-request cap


def _fetch_org_stats(org: str) -> tuple[int, float]:
    """Returns (grant_count, award_total_usd) for every NIH-funded project
    with `org` as the recipient organization, most recent fiscal years
    first isn't guaranteed — this sums whatever the API returns for the
    org-name match, uncapped by year."""
    count = 0
    award_total = 0.0
    offset = 0
    while True:
        body = {
            "criteria": {"org_names": [org]},
            "include_fields": ["AwardAmount"],
            "offset": offset,
            "limit": 500,
        }
        try:
            resp = requests.post(
                NIH_REPORTER_API, json=body, timeout=20,
                headers={"User-Agent": "besseleth/1.0 (industry-briefing tool)", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"[grants] NIH RePORTER request failed for '{org}': {e}")
            break

        results = data.get("results", [])
        for project in results:
            count += 1
            # Field casing in RePORTER's JSON responses is lowercase
            # snake_case even though the request's include_fields uses
            # CamelCase — checking both defensively since this hasn't
            # been exercised against live traffic yet.
            amount = project.get("award_amount", project.get("AwardAmount"))
            if isinstance(amount, (int, float)):
                award_total += amount

        offset += len(results)
        total_available = (data.get("meta") or {}).get("total", 0)
        if not results or offset >= total_available or offset >= MAX_RECORDS_PER_ORG:
            break

    return count, award_total


def sync_all(db: DB, orgs: list[str], recheck_days: int = 7) -> dict:
    """Refreshes NIH grant stats for every org in `orgs` not checked
    within `recheck_days`. Returns {"orgs_checked": int, "orgs_skipped": int}."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=recheck_days)
    checked = 0
    skipped = 0
    for org in orgs:
        existing = db.get_company(org)
        checked_at = existing["nih_grants_checked_at"] if existing else None
        if checked_at:
            try:
                if datetime.fromisoformat(checked_at) > cutoff:
                    skipped += 1
                    continue
            except ValueError:
                pass
        count, award_total = _fetch_org_stats(org)
        db.set_nih_grant_stats(org, count, award_total)
        checked += 1
    return {"orgs_checked": checked, "orgs_skipped": skipped}
