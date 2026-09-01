"""Tiny sqlite store for scraped items — dedup across runs."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,           -- stable hash: source + url/arxiv id
    source TEXT NOT NULL,          -- arxiv | news | conference | linkedin
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


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

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
