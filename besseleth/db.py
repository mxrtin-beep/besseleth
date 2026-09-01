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
"""

# Columns added after the initial release — migrated in with ALTER TABLE
# (each guarded individually so an existing DB upgrades in place).
ENRICHMENT_COLUMNS = {
    "org": "TEXT",                     # company/lab/institution the item is about, LLM-extracted
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
    ):
        self.conn.execute(
            """UPDATE items SET org = ?, org_type = ?, modality = ?, therapeutic_target = ?,
               novelty_score = ?, novelty_rationale = ?, location_text = ?, lat = ?, lon = ?,
               enriched_at = ? WHERE id = ?""",
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
                datetime.now(timezone.utc).isoformat(),
                item_id,
            ),
        )
        self.conn.commit()

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
        narrower set."""
        self.conn.row_factory = sqlite3.Row
        q = """
            SELECT org,
                   MAX(org_type) as org_type,
                   MAX(location_text) as location_text,
                   MAX(lat) as lat, MAX(lon) as lon,
                   COUNT(*) as n,
                   GROUP_CONCAT(DISTINCT source) as sources
            FROM items
            WHERE org IS NOT NULL AND org != ''
            GROUP BY org
            ORDER BY n DESC
        """
        return list(self.conn.execute(q).fetchall())

    def papers(self, sources: list[str]) -> list[sqlite3.Row]:
        """All items in the given sources, enriched or not — the papers
        table's data source. Not filtered by report status: this is a
        standing, browsable index, not a weekly snapshot."""
        self.conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in sources)
        q = f"SELECT * FROM items WHERE source IN ({placeholders}) ORDER BY published_at DESC"
        return list(self.conn.execute(q, sources).fetchall())
