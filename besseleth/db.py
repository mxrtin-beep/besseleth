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

-- Devices/systems tracked over time (trends/store.py) — e.g. for
-- neurotech, each BCI's information transfer rate, implant longevity,
-- FDA regulatory status. Used to live in a hand-copied devices.yaml;
-- moved here so (a) new metric columns show up automatically instead of
-- needing devices.example.yaml re-copied by hand, and (b) the same
-- device can gain a *new* dated row as its story develops (a later FDA
-- designation, a new metric reading) rather than being a single
-- never-updated entry — that's what makes a real timeline possible.
-- One row per (name, org, date_reported); auto-extraction only skips an
-- exact repeat of that triple, not the device itself.
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    org TEXT NOT NULL,
    org_type TEXT,                  -- industry | academic
    fda_status TEXT,
    metrics_json TEXT,              -- {"metric_key": number, ...}
    source_url TEXT,
    date_reported TEXT,
    notes TEXT,
    auto_extracted INTEGER DEFAULT 0,
    recorded_at TEXT NOT NULL        -- when this row was captured (distinct from date_reported)
);
CREATE INDEX IF NOT EXISTS idx_devices_name_org ON devices(name, org);

-- Company-level business metrics (trends/company_store.py) — funding,
-- stock price, IPO status. Same rationale as `devices` for living here
-- instead of companies.yaml. One row per company, kept up to date in
-- place (unlike devices, a company isn't a timeline of reports).
CREATE TABLE IF NOT EXISTS companies (
    name TEXT PRIMARY KEY,
    stock_ticker TEXT,
    stock_price REAL,
    stock_price_updated_at TEXT,
    funding_total_usd REAL,
    last_funding_round TEXT,
    last_funding_date TEXT,
    ipo_date TEXT,                  -- ISO8601 date, if it's gone public
    stock_exchange TEXT,            -- e.g. "NASDAQ" — set once ipo_date is
    is_public INTEGER DEFAULT 0,
    source_url TEXT,
    notes TEXT,
    auto_extracted INTEGER DEFAULT 0
);
"""

# Columns added after the initial release to `devices`/`companies` —
# same auto-migration idiom as ENRICHMENT_COLUMNS, for the same reason:
# a new metric shouldn't require anyone to re-copy an example file.
DEVICE_COLUMNS: dict[str, str] = {}
COMPANY_COLUMNS: dict[str, str] = {}

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
    "matched_reason": "TEXT",          # "company" | "school" — why matched_contact matched (see personalize.py)
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
    matched_reason: Optional[str] = None
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
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(devices)")}
        for col, sqltype in DEVICE_COLUMNS.items():
            if col not in existing:
                self.conn.execute(f"ALTER TABLE devices ADD COLUMN {col} {sqltype}")
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(companies)")}
        for col, sqltype in COMPANY_COLUMNS.items():
            if col not in existing:
                self.conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {sqltype}")

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

    def record_enrich_run(self, items: int, seconds: float) -> None:
        """Accumulates all-time enrich stats in `meta`, alongside the
        most recent run's own numbers — powers the dashboard's "enriched
        N items, avg Xs/item" display. Only called for a run that
        actually processed something (see enrich.py), so a no-op run
        doesn't dilute the all-time average with a bunch of zeros."""
        total_items = int(self.get_meta("enrich_total_items") or 0) + items
        total_seconds = float(self.get_meta("enrich_total_seconds") or 0) + seconds
        self.set_meta("enrich_total_items", str(total_items))
        self.set_meta("enrich_total_seconds", str(total_seconds))
        self.set_meta("enrich_last_run_items", str(items))
        self.set_meta("enrich_last_run_seconds", str(seconds))
        self.set_meta("enrich_last_run_at", datetime.now(timezone.utc).isoformat())

    def get_enrich_stats(self) -> dict:
        return {
            "total_items": int(self.get_meta("enrich_total_items") or 0),
            "total_seconds": float(self.get_meta("enrich_total_seconds") or 0),
            "last_run_items": int(self.get_meta("enrich_last_run_items") or 0),
            "last_run_seconds": float(self.get_meta("enrich_last_run_seconds") or 0),
            "last_run_at": self.get_meta("enrich_last_run_at"),
        }

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

    def items_to_reenrich(self, sources: list[str], limit: int) -> list[sqlite3.Row]:
        """Every item in the given sources, oldest-enriched (or never
        enriched) first — for the dashboard's "re-check already-enriched
        items too" toggle. Oldest-first (not just "everything at once")
        means a big backlog gets cycled through gradually over repeated
        runs, same CPU-friendly batching as a normal enrich, rather than
        one huge re-enrich burst."""
        self.conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in sources)
        q = (
            f"SELECT * FROM items WHERE source IN ({placeholders}) "
            "ORDER BY enriched_at IS NOT NULL, enriched_at ASC LIMIT ?"
        )
        return list(self.conn.execute(q, [*sources, limit]).fetchall())

    def recent_items_for_dedupe(self, sources: list[str], limit: int = 300) -> list[Item]:
        """The most recent items in the given sources, as Item objects
        (novelty_score/novelty_rationale included) — for
        dedupe.group_near_duplicates() to find near-duplicate rows (the
        same article picked up by two feeds, most commonly) so their
        novelty scores can be synced. Multiple rows for the same story
        are expected and kept — a scraper pulling from several sites
        inevitably sees the same news more than once — the fix is
        making sure they agree, not collapsing them into one. Bounded
        to the most recent `limit` so this stays cheap on a large
        history."""
        self.conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in sources)
        q = f"SELECT * FROM items WHERE source IN ({placeholders}) ORDER BY published_at DESC LIMIT ?"
        rows = self.conn.execute(q, [*sources, limit]).fetchall()
        return [
            Item(
                id=r["id"], source=r["source"], title=r["title"], url=r["url"] or "",
                summary=r["summary"] or "", published_at=r["published_at"] or "",
                novelty_score=r["novelty_score"], novelty_rationale=r["novelty_rationale"],
            )
            for r in rows
        ]

    def sync_novelty(self, item_id: str, novelty_score: int | None, novelty_rationale: str | None) -> None:
        self.conn.execute(
            "UPDATE items SET novelty_score = ?, novelty_rationale = ? WHERE id = ?",
            (novelty_score, novelty_rationale, item_id),
        )
        self.conn.commit()

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

    def accumulated_knowledge_stats(self) -> dict:
        """A snapshot of everything besseleth has accumulated across all
        runs, ever — not just this week's items. Used to give the report's
        closing 'big picture' section something to place new items
        against (an org that's been quiet suddenly active again, a trend
        that's been building for months, etc.)."""
        total_items = self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        earliest = self.conn.execute(
            "SELECT MIN(published_at) FROM items WHERE published_at IS NOT NULL AND published_at != ''"
        ).fetchone()[0]
        org_counts = self.org_item_counts()
        top_orgs = sorted(org_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
        return {
            "total_items": total_items,
            "total_orgs": len(org_counts),
            "top_orgs": top_orgs,
            "earliest_date": (earliest or "")[:10],
        }

    def org_item_counts(self) -> dict[str, int]:
        """{org: item count} for every distinct org — used to pick which
        spelling/casing variant of a near-duplicate org name is the most-
        used one, when canonicalizing (see enrich.py's
        _canonicalize_existing_orgs)."""
        rows = self.conn.execute(
            "SELECT org, COUNT(*) as n FROM items WHERE org IS NOT NULL AND org != '' GROUP BY org"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def rename_org(self, old_org: str, new_org: str) -> int:
        """Repoints every item from `old_org` to `new_org` (exact string
        match) — used to fold spelling/casing variants of the same org
        ("Ability Neurotech" vs "Ability NeuroTech") into one canonical
        row instead of them accumulating as separate Orgs-table entries.
        Returns how many rows were changed."""
        cur = self.conn.execute("UPDATE items SET org = ? WHERE org = ?", (new_org, old_org))
        self.conn.commit()
        return cur.rowcount

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

    def location_text_variants(self) -> list[sqlite3.Row]:
        """Every distinct (location_text, lat, lon, count) with a
        location set — lets near-identical geocoded points that differ
        only in how much detail the LLM included ("Mountain View" vs.
        "Mountain View, CA" vs. "Mountain View, California, United
        States") be clustered and standardized (see enrich.py's
        _standardize_location_names)."""
        self.conn.row_factory = sqlite3.Row
        return list(
            self.conn.execute(
                """SELECT location_text, lat, lon, COUNT(*) as n FROM items
                   WHERE lat IS NOT NULL AND location_text IS NOT NULL
                   GROUP BY location_text, lat, lon ORDER BY n DESC"""
            ).fetchall()
        )

    def standardize_location(
        self, old_text: str, old_lat: float, old_lon: float, new_text: str, new_lat: float, new_lon: float
    ) -> int:
        """Rewrites every item currently at exactly (old_text, old_lat,
        old_lon) — the caller clusters by rounded coordinates and passes
        each variant's own exact values — to the cluster's chosen
        canonical (new_text, new_lat, new_lon). Snapping lat/lon to one
        point too (not just the text label) is what actually merges
        several near-identical geocoded points into one Map tab marker;
        the values only ever come from another item that already
        geocoded to (within ~1km of) the same real place, never guessed.
        Returns how many items changed."""
        cur = self.conn.execute(
            "UPDATE items SET location_text = ?, lat = ?, lon = ? WHERE location_text = ? AND lat = ? AND lon = ?",
            (new_text, new_lat, new_lon, old_text, old_lat, old_lon),
        )
        self.conn.commit()
        return cur.rowcount

    def org_location_votes(self, org: str) -> list[sqlite3.Row]:
        """Every distinct (location_text, lat, lon) items for `org` carry,
        most-agreed-on first — lets a consensus location be picked when
        items disagree (see enrich.py's _apply_location_consensus)."""
        self.conn.row_factory = sqlite3.Row
        return list(
            self.conn.execute(
                """SELECT location_text, lat, lon, COUNT(*) as n FROM items
                   WHERE lower(org) = lower(?) AND lat IS NOT NULL
                   GROUP BY location_text, lat, lon ORDER BY n DESC""",
                (org,),
            ).fetchall()
        )

    def set_org_location_all(self, org: str, location_text: str, lat: float, lon: float) -> int:
        """Overwrites location on EVERY item for `org`, not just those
        missing one — unlike set_org_location (additive-only), this is
        for reconciling disagreement: one org should be one point on the
        map, not split across markers because a minority of items got a
        different LLM location guess. Returns how many items changed."""
        cur = self.conn.execute(
            "UPDATE items SET location_text = ?, lat = ?, lon = ? WHERE lower(org) = lower(?)",
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

    # --- devices/companies (trends) -------------------------------------

    def devices(self) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        return list(self.conn.execute("SELECT * FROM devices ORDER BY date_reported, id").fetchall())

    def delete_bogus_devices(self) -> int:
        """Removes device rows whose name is just the org's own name
        (case-insensitive) — a bug in an earlier version of enrich.py
        created these when it couldn't identify a specific device, which
        cluttered the FDA timeline with "devices" that were never
        actually about one. Auto-extracted only; a row you've hand-edited
        or hand-added with that name is left alone. Returns how many
        were removed."""
        cur = self.conn.execute(
            "DELETE FROM devices WHERE lower(name) = lower(org) AND auto_extracted = 1"
        )
        self.conn.commit()
        return cur.rowcount

    def device_exists(self, name: str, org: str, date_reported: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM devices WHERE lower(name) = lower(?) AND lower(org) = lower(?) AND date_reported = ?",
            (name, org, date_reported),
        ).fetchone()
        return row is not None

    def add_device(
        self,
        name: str,
        org: str,
        org_type: str,
        fda_status: str,
        metrics_json: str,
        source_url: str,
        date_reported: str,
        notes: str,
        auto_extracted: bool,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO devices
               (name, org, org_type, fda_status, metrics_json, source_url, date_reported, notes, auto_extracted, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name, org, org_type, fda_status, metrics_json, source_url, date_reported, notes,
                1 if auto_extracted else 0, datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def earliest_item_date_for_org(self, org: str) -> str | None:
        row = self.conn.execute(
            "SELECT MIN(published_at) FROM items WHERE lower(org) = lower(?) AND published_at IS NOT NULL",
            (org,),
        ).fetchone()
        return (row[0] or "")[:10] or None if row else None

    def companies(self) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        return list(self.conn.execute("SELECT * FROM companies ORDER BY name").fetchall())

    def get_company(self, name: str) -> sqlite3.Row | None:
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute("SELECT * FROM companies WHERE lower(name) = lower(?)", (name,)).fetchone()

    def add_company(self, **fields: Any) -> bool:
        """Inserts a new company row if `fields['name']` doesn't already
        exist (case-insensitive) — mirrors the old auto_upsert_company's
        never-overwrite semantics. Returns True if it inserted."""
        if self.get_company(fields["name"]):
            return False
        cols = list(fields.keys())
        placeholders = ",".join("?" for _ in cols)
        self.conn.execute(
            f"INSERT INTO companies ({','.join(cols)}) VALUES ({placeholders})",
            [fields[c] for c in cols],
        )
        self.conn.commit()
        return True

    def set_company_ipo(self, name: str, ipo_date: str, stock_exchange: str) -> None:
        """Records that a company went public — inserts a bare row if
        it's brand new (an IPO is newsworthy enough to add on its own,
        unlike auto_upsert_company's funding-only trigger), but only
        fills ipo_date/stock_exchange if not already set, so a
        hand-corrected value is never clobbered by a later, possibly
        secondhand mention of the same IPO."""
        if not self.add_company(
            name=name, stock_ticker="", stock_price=None, stock_price_updated_at="",
            funding_total_usd=None, last_funding_round="", last_funding_date="",
            ipo_date=ipo_date, stock_exchange=stock_exchange, is_public=1,
            source_url="", notes="Auto-extracted by besseleth from a scraped item — verify before trusting.",
            auto_extracted=1,
        ):
            self.conn.execute(
                """UPDATE companies SET
                       ipo_date = COALESCE(NULLIF(ipo_date, ''), ?),
                       stock_exchange = COALESCE(NULLIF(stock_exchange, ''), ?),
                       is_public = 1
                   WHERE lower(name) = lower(?)""",
                (ipo_date, stock_exchange, name),
            )
            self.conn.commit()

    def delete_company(self, name: str) -> None:
        self.conn.execute("DELETE FROM companies WHERE lower(name) = lower(?)", (name,))
        self.conn.commit()

    def merge_company(self, keep_name: str, drop_name: str) -> None:
        """Folds `drop_name` into `keep_name` — fills any field that's
        set on the dropped row but null/empty on the kept one, then
        deletes the dropped row. Used to fix a near-duplicate company
        (e.g. an LLM typo like "Axfot" for "Axoft") without losing
        whichever of the two rows happened to have the funding/IPO data."""
        keep = self.get_company(keep_name)
        drop = self.get_company(drop_name)
        if not keep or not drop:
            return
        fields = [
            "stock_ticker", "stock_price", "stock_price_updated_at", "funding_total_usd",
            "last_funding_round", "last_funding_date", "ipo_date", "stock_exchange", "source_url", "notes",
        ]
        updates = {f: drop[f] for f in fields if not keep[f] and drop[f]}
        if keep["is_public"] == 0 and drop["is_public"]:
            updates["is_public"] = 1
        if updates:
            self.update_company(keep_name, **updates)
        self.delete_company(drop_name)

    def update_company(self, name: str, **fields: Any) -> None:
        if not fields:
            return
        cols = list(fields.keys())
        set_clause = ", ".join(f"{c} = ?" for c in cols)
        self.conn.execute(
            f"UPDATE companies SET {set_clause} WHERE lower(name) = lower(?)",
            [fields[c] for c in cols] + [name],
        )
        self.conn.commit()
