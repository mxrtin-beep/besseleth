"""Pulls industry news from RSS/Atom feeds (free) and optionally NewsAPI.org.

Google News RSS search (`https://news.google.com/rss/search?q=...`) needs no
key and covers most trade press, so it's the default in config.example.yaml.
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import feedparser
import requests

from ..config import env
from ..db import Item
from .util import stable_id, strip_html, text_matches_keywords

# Google News RSS search has no offset/page parameter and no real
# "give me older results" mode — one query always returns roughly its
# newest ~100 matches for that search, full stop, so widening days_back
# alone (like a plain cutoff filter) can't reach further back than that.
# What it DOES support, as an unofficial but reliable Google Search
# operator embedded right in the query text, is `after:`/`before:` date
# bounds. So a real historical backfill slices the requested range into
# date-bounded chunks and issues one dated query per chunk instead of one
# undated query — each chunk gets its own ~100-result budget, so a
# year-long ask actually samples each month instead of only ever
# returning the same latest handful. Only applied to Google News search
# URLs specifically (detected by domain) — a plain RSS feed (a blog, a
# publication's own feed) has no such operator and stays a single fetch,
# same as before.
GOOGLE_NEWS_SEARCH_DOMAIN = "news.google.com"
HISTORICAL_CHUNK_DAYS = 30
MAX_HISTORICAL_CHUNKS_PER_KEYWORD = 60  # ~5 years at the default chunk size — a safety valve,
                                        # not a real expectation any backfill needs to go further


def _is_google_news_search(feed_template: str) -> bool:
    return GOOGLE_NEWS_SEARCH_DOMAIN in feed_template


def _date_chunks(start: date, end: date, chunk_days: int = HISTORICAL_CHUNK_DAYS) -> list[tuple[date, date]]:
    chunks = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=chunk_days), end)
        chunks.append((cur, nxt))
        cur = nxt
    return chunks


def _parse_feed_entries(
    feed_url: str, config, cutoff: datetime, source: str = "news", require_keyword_match: bool = True
) -> list[Item]:
    items = []
    try:
        parsed = feedparser.parse(feed_url)
    except Exception as e:
        print(f"[{source}] failed to parse feed {feed_url}: {e}")
        return items

    for entry in parsed.entries:
        title = entry.get("title", "").strip()
        summary = strip_html(entry.get("summary", "") or entry.get("description", ""))
        hits = text_matches_keywords(f"{title} {summary}", config.keywords)
        # A news feed is typically broad (a wire feed, a topic-level RSS
        # covering way more than this industry) so the keyword filter is
        # essential noise-cutting there. A blog feed is the opposite: you
        # picked that specific feed BECAUSE it's a neurotech company/lab/
        # researcher's blog, so most of its posts (a hiring update, a
        # culture post, a release note) won't happen to contain one of
        # your keyword phrases even though the whole feed is exactly what
        # you meant to follow — requiring a match there was silently
        # dropping nearly everything from every blog you added. See
        # blog_scraper.fetch, which passes require_keyword_match=False.
        if require_keyword_match and not hits:
            continue

        url = entry.get("link", "")
        published = None
        if getattr(entry, "published_parsed", None):
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if published and published < cutoff:
            continue

        items.append(
            Item(
                id=stable_id(source, url or title),
                source=source,
                title=title,
                url=url,
                summary=summary,
                published_at=(published or datetime.now(timezone.utc)).isoformat(),
                matched_keywords=hits,
            )
        )
    return items


def _fetch_newsapi(config, cutoff: datetime) -> list[Item]:
    api_key = env("NEWSAPI_KEY")
    if not api_key:
        print("[news] use_newsapi is set but NEWSAPI_KEY is not in the environment; skipping.")
        return []
    items = []
    for keyword in config.keywords:
        try:
            resp = requests.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": keyword,
                    "from": cutoff.date().isoformat(),
                    "sortBy": "publishedAt",
                    "language": "en",
                    "apiKey": api_key,
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"[news] NewsAPI request failed for '{keyword}': {e}")
            continue

        for article in data.get("articles", []):
            title = article.get("title", "") or ""
            summary = article.get("description", "") or ""
            url = article.get("url", "")
            hits = text_matches_keywords(f"{title} {summary}", config.keywords) or [keyword]
            items.append(
                Item(
                    id=stable_id("news", url or title),
                    source="news",
                    title=title,
                    url=url,
                    summary=summary,
                    published_at=article.get("publishedAt", datetime.now(timezone.utc).isoformat()),
                    matched_keywords=hits,
                )
            )
    return items


def fetch_feeds(
    config, feeds: list[str], days_back: int, source: str = "news", require_keyword_match: bool = True
) -> list[Item]:
    """Generic RSS/Atom fetch usable for news, blogs, or any other
    feed-based source — just pass a different `source` label.

    days_back beyond HISTORICAL_CHUNK_DAYS is treated as a real backfill,
    not just a slightly wider everyday window — a Google News search feed
    (see the module docstring) gets sliced into dated chunks so it can
    actually reach that far back; every other feed just gets the same
    single fetch as always, filtered by the same cutoff (an ordinary RSS
    feed has no history to chunk into regardless)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    items: list[Item] = []
    seen_ids: set[str] = set()

    def _collect(url: str):
        for it in _parse_feed_entries(url, config, cutoff, source=source, require_keyword_match=require_keyword_match):
            if it.id not in seen_ids:
                items.append(it)
                seen_ids.add(it.id)

    for feed_template in feeds:
        if "{query}" not in feed_template:
            _collect(feed_template)
            continue

        is_backfill = days_back > HISTORICAL_CHUNK_DAYS and _is_google_news_search(feed_template)
        for keyword in config.keywords:
            if not is_backfill:
                _collect(feed_template.format(query=quote(keyword)))
                continue

            start_date = datetime.now(timezone.utc).date() - timedelta(days=days_back)
            end_date = datetime.now(timezone.utc).date()
            chunks = _date_chunks(start_date, end_date)[:MAX_HISTORICAL_CHUNKS_PER_KEYWORD]
            for i, (chunk_start, chunk_end) in enumerate(chunks):
                dated_query = f"{keyword} after:{chunk_start.isoformat()} before:{chunk_end.isoformat()}"
                _collect(feed_template.format(query=quote(dated_query)))
                if i < len(chunks) - 1:
                    time.sleep(1)  # be polite — this is an unofficial endpoint, not a real API

    return items


def fetch(config, source_cfg: dict, days_back: int) -> list[Item]:
    items = fetch_feeds(config, source_cfg.get("feeds", []), days_back, source="news")
    seen_ids = {i.id for i in items}

    if source_cfg.get("use_newsapi"):
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        for it in _fetch_newsapi(config, cutoff):
            if it.id not in seen_ids:
                items.append(it)
                seen_ids.add(it.id)

    return items
