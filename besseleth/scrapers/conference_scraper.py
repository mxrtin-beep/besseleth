"""Surfaces upcoming conferences/industry events from a curated watchlist.

There's no good free, structured API for "industry events in category X",
so this reads the hand-maintained `sources.conferences.watchlist` in
config.yaml and turns it into report items. Extend the watchlist as you
discover new events. If you have a paid Eventbrite/PredictHQ key you can
add a fetcher here following the same pattern as news_scraper's NewsAPI path.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..db import Item
from .util import stable_id, text_matches_keywords


def fetch(config, source_cfg: dict) -> list[Item]:
    items = []
    for entry in source_cfg.get("watchlist", []):
        name = entry.get("name", "")
        url = entry.get("url", "")
        month = entry.get("month", "")
        blurb = f"{name} — {month}" if month else name
        hits = text_matches_keywords(name, config.keywords)
        items.append(
            Item(
                id=stable_id("conference", url or name),
                source="conference",
                title=name,
                url=url,
                summary=blurb,
                published_at=datetime.now(timezone.utc).isoformat(),
                matched_keywords=hits or [config.industry_name],
            )
        )
    return items
