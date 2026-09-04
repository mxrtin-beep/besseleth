"""Tiny sqlite store for scraped items — dedup across runs, plus the
structured "paper" fields (org, modality, therapeutic target, novelty)
that besseleth/enrich.py fills in for the papers table."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,           -- stable hash: source + url/arxiv id
    source TEXT NOT NULL,          -- arxiv | news | blog | conference | event | social | linkedin | clip
    title TEXT NOT NULL,
    url TEXT,
    summary TEXT,                  -- original abstract/description
    published_at TEXT,             -- ISO8601
    fetched_at TEXT NOT NULL,
    matched_keywords TEXT,         -- comma-separated
    matched_contact TEXT,          -- contact name if personalized match, else NULL
    matched_company TEXT,
    included_in_report TEXT        -- report id/date once emitted, else NULL
);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_published ON items(published_at);

-- Tiny key-value store for state that needs to survive a restart, e.g.
-- the scheduler's last-fetch time (so re-launching the app doesn't kick
-- off an immediate re-fetch if one already ran recently).
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Job postings pulled from each org's ATS job-board API (see
-- besseleth/scrapers/jobs_scraper.py). Unlike `items`, this is kept in
-- sync with what's actually live: a posting missing from an org's most
-- recent listing gets `removed_at` set rather than being deleted, so the
-- table tracks "currently open" without losing history.
CREATE TABLE IF NOT EXISTS job_postings (
    id TEXT PRIMARY KEY,            -- stable hash: org + platform + external_id
    org TEXT NOT NULL,
    platform TEXT NOT NULL,         -- greenhouse | lever | ashby
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    location TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,     -- bumped every run the posting is still listed
    removed_at TEXT                 -- set once it drops off the org's listing
);
CREATE INDEX IF NOT EXISTS idx_jobs_org ON job_postings(org);

-- Which ATS platform+slug each org's job board was found at (or that
-- none was found), so auto-detect doesn't re-probe every org on every
-- run — see jobs_scraper.py's _resolve_board().
CREATE TABLE IF NOT EXISTS job_board_cache (
    org TEXT PRIMARY KEY,
    platform TEXT,                  -- NULL if nothing was found
    slug TEXT,
    checked_at TEXT NOT NULL
);

-- Whether a web lookup (see web_lookup.py) already tried to find an
-- org's location, so a miss isn't re-queried every enrich run — same
-- idea as job_board_cache. The location itself is cached too (not just
-- found/not), so a later item for the same org that's missing a
-- location can be backfilled for free from here instead of re-querying
-- Wikidata.
CREATE TABLE IF NOT EXISTS org_location_cache (
    org TEXT PRIMARY KEY,
    found INTEGER NOT NULL,
    location_text TEXT,
    lat REAL,
    lon REAL,
    checked_at TEXT NOT NULL
);
"""

# Columns added after the initial release — migrated in with ALTER TABLE
# (each guarded individually so an existing DB upgrades in place).
ENRICHMENT_COLUMNS = {
    "org": "TEXT",                     # company/lab/institution the item is about, LLM-extracted
    "org_description": "TEXT",         # what the org does/is, LLM-extracted, kept to <=5 words (e.g. "BCI implant company")
    "org_type": "TEXT",                # industry | academic | government | nonprofit | unknown
    "modality": "TEXT",                # EEG | ECoG | CNS implant | PNS implant | EMG | fMRI | fNIRS | other | unknown
    "therapeutic_target": "TEXT",      # e.g. motor, speech, vision, memory, mood/psychiatric, epilepsy, pain, other
    "novelty_score": "INTEGER",        # 1-5, how surprising vs. other items on the same topic
    "novelty_rationale": "TEXT",       # one-sentence LLM rationale for the score
    "location_text": "TEXT",           # "City, Country" as guessed by the LLM from the item's text, or NULL
    "lat": "REAL",                     # geocoded from location_text (Nominatim/OSM, free) — NULL if ungeocodable
    "lon": "REAL",
    "enriched_at": "TEXT",             # ISO8601 once enrichment has run for this item (even if it found nothing)
}


@dataclass
class Item:
    id: str
    source: str
    title: str
    url: str = ""
    summary: str = ""
    published_at: str = ""
    matched_keywords: list[str] = field(default_factory=list)
    matched_contact: Optional[str] = None
    matched_company: Optional[str] = None
    org: Optional[str] = None
    org_type: Optional[str] = None
    modality: Optional[str] = None
    therapeutic_target: Optional[str] = None
    novelty_score: Optional[int] = None
    novelty_rationale: Optional[str] = None
    location_text: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(items)")}
        for col, sqltype in ENRICHMENT_COLUMNS.items():
            if col not in existing:
                self.conn.execute(f"ALTER TABLE items ADD COLUMN {col} {sqltype}")

    def close(self):
        self.conn.close()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def upsert_item(self, item: Item) -> bool:
        """Insert if new. Returns True if it was newly inserted."""
        cur = self.conn.execute("SELECT 1 FROM items WHERE id = ?", (item.id,))
        if cur.fetchone():
            return False
        self.conn.execute(
            """INSERT INTO items
               (id, source, title, url, summary, published_at, fetched_at,
                matched_keywords, matched_contact, matched_company, included_in_report)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (
                item.id,
                item.source,
                item.title,
                item.url,
                item.summary,
                item.published_at,
                datetime.now(timezone.utc).isoformat(),
                ",".join(item.matched_keywords),
                item.matched_contact,
                item.matched_company,
            ),
        )
        self.conn.commit()
        return True

    def delete_item(self, item_id: str) -> bool:
        """Removes an item outright (used for pasted/manual entries you
        want to take back — scraped items are left alone by convention,
        since re-fetching would just bring them back anyway)."""
        cur = self.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def manual_items(self, sources: list[str], limit: int = 100) -> list[sqlite3.Row]:
        """Recently pasted items (linkedin/event/social/clip), newest
        first — for the dashboard's Paste tab list-with-delete view."""
        self.conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in sources)
        q = f"SELECT * FROM items WHERE source IN ({placeholders}) ORDER BY fetched_at DESC LIMIT ?"
        return list(self.conn.execute(q, [*sources, limit]).fetchall())

    def unreported_items(self, source: Optional[str] = None) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        q = "SELECT * FROM items WHERE included_in_report IS NULL"
        params: list[Any] = []
        if source:
            q += " AND source = ?"
            params.append(source)
        q += " ORDER BY published_at DESC"
        return list(self.conn.execute(q, params).fetchall())

    def mark_reported(self, ids: list[str], report_id: str):
        if not ids:
            return
        self.conn.executemany(
            "UPDATE items SET included_in_report = ? WHERE id = ?",
            [(report_id, i) for i in ids],
        )
        self.conn.commit()

    def unenriched_items(self, sources: list[str], limit: int) -> list[sqlite3.Row]:
        """Items in the given sources that enrich.py hasn't processed yet."""
        self.conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in sources)
        q = (
            f"SELECT * FROM items WHERE source IN ({placeholders}) AND enriched_at IS NULL "
            "ORDER BY published_at DESC LIMIT ?"
        )
        return list(self.conn.execute(q, [*sources, limit]).fetchall())

    def recent_items_for_context(self, source: str, keywords: list[str], exclude_id: str, limit: int = 5) -> list[sqlite3.Row]:
        """A handful of other recent items sharing at least one keyword —
        used as novelty-scoring context ('surprising compared to what?')."""
        if not keywords:
            return []
        self.conn.row_factory = sqlite3.Row
        clauses = " OR ".join("matched_keywords LIKE ?" for _ in keywords)
        params = [f"%{kw}%" for kw in keywords]
        q = (
            f"SELECT title, summary FROM items WHERE source = ? AND id != ? AND ({clauses}) "
            "ORDER BY published_at DESC LIMIT ?"
        )
        return list(self.conn.execute(q, [source, exclude_id, *params, limit]).fetchall())

    def save_enrichment(
        self,
        item_id: str,
        org: Optional[str],
        org_type: Optional[str],
        modality: Optional[str],
        therapeutic_target: Optional[str],
        novelty_score: Optional[int],
        novelty_rationale: Optional[str],
        location_text: Optional[str] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        org_description: Optional[str] = None,
    ):
        self.conn.execute(
            """UPDATE items SET org = ?, org_type = ?, modality = ?, therapeutic_target = ?,
               novelty_score = ?, novelty_rationale = ?, location_text = ?, lat = ?, lon = ?,
               org_description = ?, enriched_at = ? WHERE id = ?""",
            (
                org,
                org_type,
                modality,
                therapeutic_target,
                novelty_score,
                novelty_rationale,
                location_text,
                lat,
                lon,
                org_description,
                datetime.now(timezone.utc).isoformat(),
                item_id,
            ),
        )
        self.conn.commit()

    def distinct_orgs(self) -> list[str]:
        """Every distinct org value currently stored — used to sweep
        already-enriched rows against enrich.py's validity check, since
        that check only runs on newly-enriched items otherwise."""
        rows = self.conn.execute("SELECT DISTINCT org FROM items WHERE org IS NOT NULL AND org != ''").fetchall()
        return [r[0] for r in rows]

    def clear_org_matches(self, names: list[str]) -> int:
        """Nulls out `org`/`org_description` on any item whose org
        exact-matches (case-insensitive) one of `names` — cleanup for
        items enriched before enrich.py started guarding against the
        LLM naming the industry itself as an "org" (e.g. the industry's
        own name showing up in the Orgs table as if it were a company).
        Cheap and safe to run on every enrich pass: already-clean rows
        just don't match. Returns how many rows were cleared."""
        if not names:
            return 0
        placeholders = ",".join("?" for _ in names)
        cur = self.conn.execute(
            f"UPDATE items SET org = NULL, org_description = NULL WHERE org IS NOT NULL AND lower(org) IN ({placeholders})",
            [n.lower() for n in names],
        )
        self.conn.commit()
        return cur.rowcount

    def distinct_locations(self) -> list[str]:
        """Every distinct location_text value currently stored — same
        idea as distinct_orgs(), for sweeping existing rows against
        enrich.py's location validity check."""
        rows = self.conn.execute(
            "SELECT DISTINCT location_text FROM items WHERE location_text IS NOT NULL AND location_text != ''"
        ).fetchall()
        return [r[0] for r in rows]

    def clear_location_matches(self, texts: list[str]) -> int:
        """Nulls out location_text/lat/lon on any item whose location_text
        exact-matches (case-insensitive) one of `texts` — cleanup for
        items enriched before enrich.py started rejecting vague guesses
        like "Remote"/"Global" (which could geocode to something
        nonsensical, e.g. an org plotted in open ocean). Returns how many
        rows were cleared."""
        if not texts:
            return 0
        placeholders = ",".join("?" for _ in texts)
        cur = self.conn.execute(
            f"""UPDATE items SET location_text = NULL, lat = NULL, lon = NULL
                WHERE location_text IS NOT NULL AND lower(location_text) IN ({placeholders})""",
            [t.lower() for t in texts],
        )
        self.conn.commit()
        return cur.rowcount

    def orgs_missing_location(self) -> list[str]:
        """Valid, non-empty orgs that have no located item at all (every
        item mentioning them has lat IS NULL) — candidates for the
        web_lookup.py backfill pass."""
        rows = self.conn.execute(
            """SELECT org FROM items WHERE org IS NOT NULL AND org != '' GROUP BY org HAVING MAX(lat) IS NULL"""
        ).fetchall()
        return [r[0] for r in rows]

    def get_org_location_cache(self, org: str) -> sqlite3.Row | None:
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute("SELECT * FROM org_location_cache WHERE org = ?", (org,)).fetchone()

    def set_org_location_cache(
        self, org: str, found: bool, location_text: str | None = None, lat: float | None = None, lon: float | None = None
    ) -> None:
        self.conn.execute(
            """INSERT INTO org_location_cache (org, found, location_text, lat, lon, checked_at) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(org) DO UPDATE SET found = excluded.found, location_text = excluded.location_text,
               lat = excluded.lat, lon = excluded.lon, checked_at = excluded.checked_at""",
            (org, int(found), location_text, lat, lon, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def set_org_location(self, org: str, location_text: str, lat: float, lon: float) -> int:
        """Backfills location_text/lat/lon on every item for `org` that
        doesn't already have one — additive only, never overwrites a
        location an item's own text already gave (that one came from the
        item itself, more specific than an org-level HQ guess). Returns
        how many items were updated."""
        cur = self.conn.execute(
            "UPDATE items SET location_text = ?, lat = ?, lon = ? WHERE org = ? AND lat IS NULL",
            (location_text, lat, lon, org),
        )
        self.conn.commit()
        return cur.rowcount

    def locations(self) -> list[sqlite3.Row]:
        """Orgs with a geocoded location, aggregated for the map — count
        of items and the most common org_type at each point."""
        self.conn.row_factory = sqlite3.Row
        q = """
            SELECT org, location_text, lat, lon, org_type, source, COUNT(*) as n
            FROM items
            WHERE lat IS NOT NULL AND lon IS NOT NULL AND org IS NOT NULL
            GROUP BY org, lat, lon, org_type, source
            ORDER BY n DESC
        """
        return list(self.conn.execute(q).fetchall())

    def orgs(self) -> list[sqlite3.Row]:
        """Every org (company/lab/institution) besseleth has extracted from
        an item, whether or not it was ever geocoded — the superset of
        locations(), which only covers the subset that resolved to a
        lat/lon. This is the source for the standalone Orgs table; the
        map itself still needs a location, so it stays scoped to that
        narrower set. Each org also carries a representative source URL
        and a short (<=5 word) description — both taken from that org's
        most recent enriched item, so there's always something to click
        through to and a quick sense of what the org actually is."""
        self.conn.row_factory = sqlite3.Row
        q = """
            SELECT org,
                   MAX(org_type) as org_type,
                   MAX(location_text) as location_text,
                   MAX(lat) as lat, MAX(lon) as lon,
                   COUNT(*) as n,
                   GROUP_CONCAT(DISTINCT source) as sources,
                   (SELECT i2.url FROM items i2 WHERE i2.org = items.org AND i2.url IS NOT NULL AND i2.url != ''
                    ORDER BY i2.published_at DESC LIMIT 1) as source_url,
                   (SELECT i2.org_description FROM items i2 WHERE i2.org = items.org
                    AND i2.org_description IS NOT NULL AND i2.org_description != ''
                    ORDER BY i2.published_at DESC LIMIT 1) as org_description
            FROM items
            WHERE org IS NOT NULL AND org != ''
            GROUP BY org
            ORDER BY n DESC
        """
        return list(self.conn.execute(q).fetchall())

    # --- Job postings -----------------------------------------------------

    def get_job_board_cache(self, org: str) -> sqlite3.Row | None:
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute("SELECT * FROM job_board_cache WHERE org = ?", (org,)).fetchone()

    def set_job_board_cache(self, org: str, platform: str | None, slug: str | None) -> None:
        self.conn.execute(
            """INSERT INTO job_board_cache (org, platform, slug, checked_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(org) DO UPDATE SET platform = excluded.platform, slug = excluded.slug,
               checked_at = excluded.checked_at""",
            (org, platform, slug, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def upsert_job_posting(self, id: str, org: str, platform: str, external_id: str, title: str, url: str, location: str) -> None:
        """Insert a posting first-seen now, or bump last_seen_at (and
        un-remove it, in case it was re-listed) if already known."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO job_postings (id, org, platform, external_id, title, url, location, first_seen_at, last_seen_at, removed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
               ON CONFLICT(id) DO UPDATE SET title = excluded.title, url = excluded.url,
               location = excluded.location, last_seen_at = excluded.last_seen_at, removed_at = NULL""",
            (id, org, platform, external_id, title, url, location, now, now),
        )

    def mark_stale_job_postings_removed(self, org: str, seen_ids: list[str]) -> int:
        """Marks any of `org`'s postings not in `seen_ids` (i.e. not
        touched by the run that just finished) as removed — called once
        per org after its listing has been fully re-synced. Returns how
        many were newly marked."""
        placeholders = ",".join("?" for _ in seen_ids) or "''"
        cur = self.conn.execute(
            f"""UPDATE job_postings SET removed_at = ?
                WHERE org = ? AND removed_at IS NULL AND id NOT IN ({placeholders})""",
            (datetime.now(timezone.utc).isoformat(), org, *seen_ids),
        )
        self.conn.commit()
        return cur.rowcount

    def job_postings(self, active_only: bool = False) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        q = "SELECT * FROM job_postings"
        if active_only:
            q += " WHERE removed_at IS NULL"
        q += " ORDER BY first_seen_at DESC"
        return list(self.conn.execute(q).fetchall())

    def papers(self, sources: list[str]) -> list[sqlite3.Row]:
        """All items in the given sources, enriched or not — the papers
        table's data source. Not filtered by report status: this is a
        standing, browsable index, not a weekly snapshot."""
        self.conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in sources)
        q = f"SELECT * FROM items WHERE source IN ({placeholders}) ORDER BY published_at DESC"
        return list(self.conn.execute(q, sources).fetchall())
