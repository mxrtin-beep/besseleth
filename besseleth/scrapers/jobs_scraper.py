"""Job postings from the orgs besseleth already knows about (companies/
labs it has extracted from items — see `db.orgs()`), pulled from each
org's public ATS job-board API rather than scraping its careers page.

**Why ATS APIs, not the careers page itself**: a company's own careers
page is one-off HTML that varies wildly and breaks on every redesign —
not a stable thing to scrape per org. Most funded startups don't build
that page from scratch, though; they point it at one of a handful of
applicant-tracking platforms, each of which exposes a free, public,
structured JSON API for its job list (no auth, no scraping, meant to be
read by other sites). This module covers three: Greenhouse, Lever, and
Ashby. An org on a platform not covered here, or with a fully custom
setup, just won't show job postings — there's nothing structured to
read from it.

**Finding each org's board** — two ways, manual always wins:
  - Auto-detect: tries a few slug guesses (see `_slug_candidates()`)
    against each platform's list endpoint. Whichever combination
    responds with an actual job list wins. The result (including "found
    nothing") is cached in `job_board_cache` so a run doesn't re-probe
    every org every time — a miss is retried after `recheck_days` in
    case the org sets up a board later.
  - Manual: entries in `job_boards.yaml` (see job_boards.example.yaml)
    for an org whose slug auto-detect can't guess, or whose actual
    platform differs from what got auto-detected.

**Add and remove**: unlike besseleth's other sources, this one tracks
what's still true, not just what was ever seen — a posting missing from
an org's current listing is marked removed (`db.mark_stale_job_postings_removed`)
rather than lingering forever. See db.py's `job_postings` table.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

from ..db import DB
from .util import stable_id

_SUFFIX_RE = re.compile(
    # Deliberately NOT stripping "labs" — plenty of orgs in this space use
    # it as part of their actual name/slug (e.g. "Merge Labs" -> mergelabs).
    r"\b(inc|incorporated|llc|ltd|corp|corporation|co|company|technologies|technology|the)\b\.?",
    re.IGNORECASE,
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _slug_candidates(org: str) -> list[str]:
    """A few plausible ATS slugs for an org name, most-likely first —
    e.g. "Merge Labs, Inc." -> ["mergelabs", "merge-labs", "merge"]."""
    base = org.lower().strip()
    stripped = _SUFFIX_RE.sub("", base).strip()
    candidates = []
    for text in (stripped, base):
        squashed = _NON_ALNUM_RE.sub("", text)
        dashed = _NON_ALNUM_RE.sub("-", text).strip("-")
        for c in (squashed, dashed):
            if c and c not in candidates:
                candidates.append(c)
    # Also just the first word (e.g. "Merge" out of "Merge Labs") —
    # common for a short/rebranded company name.
    first_word = _NON_ALNUM_RE.sub("", stripped.split()[0]) if stripped.split() else None
    if first_word and first_word not in candidates:
        candidates.append(first_word)
    return candidates[:4]  # keep the probe budget small


def _fetch_greenhouse(slug: str) -> list[dict] | None:
    try:
        resp = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=10)
        if resp.status_code != 200:
            return None
        jobs = resp.json().get("jobs", [])
        if not jobs:
            return None
        return [
            {
                "external_id": str(j["id"]),
                "title": j.get("title", ""),
                "url": j.get("absolute_url", ""),
                "location": (j.get("location") or {}).get("name", ""),
            }
            for j in jobs
        ]
    except (requests.RequestException, ValueError, KeyError):
        return None


def _fetch_lever(slug: str) -> list[dict] | None:
    try:
        resp = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=10)
        if resp.status_code != 200:
            return None
        jobs = resp.json()
        if not isinstance(jobs, list) or not jobs:
            return None
        return [
            {
                "external_id": str(j["id"]),
                "title": j.get("text", ""),
                "url": j.get("hostedUrl", ""),
                "location": (j.get("categories") or {}).get("location", ""),
            }
            for j in jobs
        ]
    except (requests.RequestException, ValueError, KeyError):
        return None


def _fetch_ashby(slug: str) -> list[dict] | None:
    try:
        resp = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=10)
        if resp.status_code != 200:
            return None
        jobs = resp.json().get("jobs", [])
        if not jobs:
            return None
        return [
            {
                "external_id": str(j["id"]),
                "title": j.get("title", ""),
                "url": j.get("jobUrl", ""),
                "location": j.get("location", ""),
            }
            for j in jobs
        ]
    except (requests.RequestException, ValueError, KeyError):
        return None


_PLATFORM_FETCHERS = {
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
    "ashby": _fetch_ashby,
}


def load_manual_boards(path) -> dict[str, dict]:
    """org name -> {"platform": ..., "slug": ...} from job_boards.yaml."""
    p = Path(path)
    if not p.exists():
        return {}
    with open(p) as f:
        raw = yaml.safe_load(f) or []
    return {entry["org"]: {"platform": entry["platform"], "slug": entry["slug"]} for entry in raw if entry.get("org")}


def _resolve_board(db: DB, org: str, manual: dict[str, dict], recheck_days: int) -> tuple[str, str] | None:
    """Returns (platform, slug) for `org`, or None if it has no known
    job board. Manual mapping wins outright; otherwise consults/updates
    the cache, auto-probing only on a cache miss or an overdue re-check."""
    if org in manual:
        return manual[org]["platform"], manual[org]["slug"]

    cached = db.get_job_board_cache(org)
    if cached:
        if cached["platform"]:
            return cached["platform"], cached["slug"]
        checked_at = datetime.fromisoformat(cached["checked_at"])
        if datetime.now(timezone.utc) - checked_at < timedelta(days=recheck_days):
            return None  # checked recently, still nothing — don't re-probe yet

    for slug in _slug_candidates(org):
        for platform, fetcher in _PLATFORM_FETCHERS.items():
            if fetcher(slug) is not None:
                db.set_job_board_cache(org, platform, slug)
                return platform, slug
    db.set_job_board_cache(org, None, None)
    return None


def sync_org_jobs(db: DB, org: str, platform: str, slug: str) -> int:
    """Fetches `org`'s current listing from its board, upserts each
    posting, and marks any of the org's previously-seen postings not in
    this listing as removed. Returns the number of postings currently
    live (0 if the board came back empty or unreachable this run —
    postings are only marked removed based on this run's actual result,
    never on a failed fetch, so a transient API error doesn't wipe them)."""
    jobs = _PLATFORM_FETCHERS[platform](slug)
    if jobs is None:
        return 0
    seen_ids = []
    for j in jobs:
        posting_id = stable_id("job", f"{org}:{platform}:{j['external_id']}")
        db.upsert_job_posting(
            posting_id, org=org, platform=platform, external_id=j["external_id"],
            title=j["title"], url=j["url"], location=j["location"],
        )
        seen_ids.append(posting_id)
    db.conn.commit()
    db.mark_stale_job_postings_removed(org, seen_ids)
    return len(seen_ids)


def fetch(config, db: DB) -> dict:
    """Syncs job postings for every org besseleth knows about (plus any
    org-only present in job_boards.yaml). Returns {"orgs_checked": int,
    "orgs_with_board": int, "active_postings": int}."""
    jobs_cfg = config.raw.get("jobs", {}) or {}
    if not jobs_cfg.get("enabled", True):
        return {"orgs_checked": 0, "orgs_with_board": 0, "active_postings": 0}

    recheck_days = jobs_cfg.get("recheck_days", 14)
    max_new_probes_per_run = jobs_cfg.get("max_new_probes_per_run", 15)
    manual = load_manual_boards(config.job_boards_path)

    orgs = {row["org"] for row in db.orgs()} | set(manual.keys())

    orgs_with_board = 0
    active_postings = 0
    new_probes = 0
    for org in sorted(orgs):
        # A never-probed (or overdue-for-recheck) org costs up to ~12
        # requests trying candidate slugs across 3 platforms — bounded
        # per run so a big org backlog doesn't do that all at once.
        # Already-resolved orgs (manual, or cached found/not-found and
        # not yet due) are one cheap lookup and don't count against this.
        is_new_probe = org not in manual and (
            (cached := db.get_job_board_cache(org)) is None
            or (not cached["platform"] and datetime.now(timezone.utc) - datetime.fromisoformat(cached["checked_at"]) >= timedelta(days=recheck_days))
        )
        if is_new_probe:
            if new_probes >= max_new_probes_per_run:
                continue
            new_probes += 1

        board = _resolve_board(db, org, manual, recheck_days)
        if not board:
            continue
        orgs_with_board += 1
        active_postings += sync_org_jobs(db, org, board[0], board[1])

    return {"orgs_checked": len(orgs), "orgs_with_board": orgs_with_board, "active_postings": active_postings}
