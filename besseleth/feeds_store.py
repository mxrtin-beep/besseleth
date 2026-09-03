"""User-submitted RSS/Atom feed URLs — the dashboard's "Feeds" tab, for
adding a news or blog source besseleth's config.yaml is missing without
hand-editing that (heavily-commented) file. Kept in a separate satellite
file, same pattern as devices.yaml/companies.yaml/job_boards.yaml: config.yaml
is the curated, comment-carrying config a person edits by hand; this is
small, UI-managed data that gets rewritten wholesale on every add/remove,
which would blow away config.yaml's comments if done there instead.

Merged into `sources.news.feeds` / `sources.blogs.feeds` at fetch time
(see pipeline.py) — a submitted feed behaves exactly like one hand-added
to config.yaml, just editable from the browser.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

CATEGORIES = ("news", "blog")


def load_feeds(path: str | Path = "feeds.yaml") -> dict[str, list[dict]]:
    p = Path(path)
    if not p.exists():
        return {c: [] for c in CATEGORIES}
    with open(p) as f:
        raw = yaml.safe_load(f) or {}
    return {c: list(raw.get(c, []) or []) for c in CATEGORIES}


def _save(path: str | Path, feeds: dict[str, list[dict]]) -> None:
    with open(path, "w") as f:
        yaml.safe_dump(feeds, f, sort_keys=False)


def add_feed(path: str | Path, category: str, url: str, label: str = "") -> bool:
    """Returns False (no-op) if that URL is already in that category."""
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}, got {category!r}")
    feeds = load_feeds(path)
    if any(f["url"] == url for f in feeds[category]):
        return False
    feeds[category].append({"url": url, "label": label, "added_at": datetime.now(timezone.utc).isoformat()})
    _save(path, feeds)
    return True


def remove_feed(path: str | Path, category: str, url: str) -> bool:
    """Returns False (no-op) if that URL wasn't found in that category."""
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}, got {category!r}")
    feeds = load_feeds(path)
    before = len(feeds[category])
    feeds[category] = [f for f in feeds[category] if f["url"] != url]
    if len(feeds[category]) == before:
        return False
    _save(path, feeds)
    return True
