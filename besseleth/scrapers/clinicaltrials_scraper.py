"""Clinical trial count + cumulative enrolled-patient total per org, via
ClinicalTrials.gov's API v2 (https://clinicaltrials.gov/data-api/api) —
free, keyless, no rate-limit registration needed.

Powers two Trends company metrics: clinical_trial_count (breadth — how
many trials this org sponsors) and clinical_trial_enrollment_total
(scale — total patients across those trials, summed). Scale is the more
informative of the two on its own: a single 500-patient pivotal trial and
a single 10-patient feasibility study both read as "1 trial" under count
alone, but very differently under enrollment.

Runs after enrichment, same as jobs_scraper — needs the org names
enrichment just extracted (db.orgs()) to know who to look up. Refreshes
each org at most every `recheck_days` (default 7): a live sponsor search
per org is a real request, and trial data doesn't change fast enough to
justify re-checking every single fetch cycle.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from ..db import DB

CT_GOV_API = "https://clinicaltrials.gov/api/v2/studies"
MAX_PAGES_PER_ORG = 20  # safety valve — 20 pages * 100 = 2000 trials, far more than any real sponsor has


def _fetch_org_stats(org: str) -> tuple[int, int]:
    """Returns (trial_count, enrollment_total) for every trial where `org`
    matches as a sponsor. `query.spons` is ClinicalTrials.gov's own
    sponsor-name search — a real field lookup, not general full-text
    search, but still text-matching under the hood: a short/generic org
    name could in principle over-match the way OpenAlex's fuzzy search
    did (see openalex_scraper.py's own history with this). Worth spot-
    checking a few orgs after the first run rather than assuming it's
    perfectly clean."""
    count = 0
    enrollment_total = 0
    page_token = None
    for _ in range(MAX_PAGES_PER_ORG):
        params = {
            "query.spons": org,
            "fields": "protocolSection.designModule.enrollmentInfo",
            "pageSize": 100,
            "format": "json",
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            resp = requests.get(
                CT_GOV_API, params=params, timeout=20,
                headers={"User-Agent": "besseleth/1.0 (industry-briefing tool)"},
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"[trials] ClinicalTrials.gov request failed for '{org}': {e}")
            break

        studies = data.get("studies", [])
        for study in studies:
            count += 1
            enrollment = (
                (study.get("protocolSection") or {})
                .get("designModule", {})
                .get("enrollmentInfo", {})
                .get("count")
            )
            if isinstance(enrollment, (int, float)):
                enrollment_total += int(enrollment)

        page_token = data.get("nextPageToken")
        if not page_token or not studies:
            break

    return count, enrollment_total


def sync_all(db: DB, orgs: list[str], recheck_days: int = 7) -> dict:
    """Refreshes clinical trial stats for every org in `orgs` not checked
    within `recheck_days`. Returns {"orgs_checked": int, "orgs_skipped": int}."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=recheck_days)
    checked = 0
    skipped = 0
    for org in orgs:
        existing = db.get_company(org)
        checked_at = existing["clinical_trials_checked_at"] if existing else None
        if checked_at:
            try:
                if datetime.fromisoformat(checked_at) > cutoff:
                    skipped += 1
                    continue
            except ValueError:
                pass
        count, enrollment = _fetch_org_stats(org)
        db.set_clinical_trial_stats(org, count, enrollment)
        checked += 1
    return {"orgs_checked": checked, "orgs_skipped": skipped}
