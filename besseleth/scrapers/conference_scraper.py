"""Surfaces conferences/industry events, plus news *about* them (accepted
talks, CFP deadlines, sponsor/speaker announcements).

There's no good free, structured API for "industry events in category X",
so `fetch()` reads the hand-maintained `sources.conferences.watchlist` in
config.yaml and turns it into report items. Extend the watchlist as you
discover new events.

`fetch_conference_news()` optionally follows each watchlist entry's own
`news_feed` (an RSS/Atom feed the conference publishes, if it has one —
many do for accepted-paper announcements or a blog) and pulls in anything
matching your industry keywords, tagged source="conference_news" so it's
reported separately from the plain calendar listing.

If you have a paid Eventbrite/PredictHQ key for programmatic conference
discovery, add a fetcher here following the pattern in events_scraper.py.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..db import Item
from .news_scraper import fetch_feeds
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


def fetch_conference_news(config, source_cfg: dict, days_back: int) -> list[Item]:
    feeds = [entry["news_feed"] for entry in source_cfg.get("watchlist", []) if entry.get("news_feed")]
    return fetch_feeds(config, feeds, days_back, source="conference_news")
