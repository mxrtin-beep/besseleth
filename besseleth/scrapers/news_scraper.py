"""Pulls industry news from RSS/Atom feeds (free) and optionally NewsAPI.org.

Google News RSS search (`https://news.google.com/rss/search?q=...`) needs no
key and covers most trade press, so it's the default in config.example.yaml.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import feedparser
import requests

from ..config import env
from ..db import Item
from .util import stable_id, strip_html, text_matches_keywords


def _parse_feed_entries(feed_url: str, config, cutoff: datetime) -> list[Item]:
    items = []
    try:
        parsed = feedparser.parse(feed_url)
    except Exception as e:
        print(f"[news] failed to parse feed {feed_url}: {e}")
        return items

    for entry in parsed.entries:
        title = entry.get("title", "").strip()
        summary = strip_html(entry.get("summary", "") or entry.get("description", ""))
        hits = text_matches_keywords(f"{title} {summary}", config.keywords)
        if not hits:
            continue

        url = entry.get("link", "")
        published = None
        if getattr(entry, "published_parsed", None):
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if published and published < cutoff:
            continue

        items.append(
            Item(
                id=stable_id("news", url or title),
                source="news",
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


def fetch(config, source_cfg: dict, days_back: int) -> list[Item]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    items: list[Item] = []
    seen_ids: set[str] = set()

    for feed_template in source_cfg.get("feeds", []):
        if "{query}" in feed_template:
            for keyword in config.keywords:
                url = feed_template.format(query=quote(keyword))
                for it in _parse_feed_entries(url, config, cutoff):
                    if it.id not in seen_ids:
                        items.append(it)
                        seen_ids.add(it.id)
        else:
            for it in _parse_feed_entries(feed_template, config, cutoff):
                if it.id not in seen_ids:
                    items.append(it)
                    seen_ids.add(it.id)

    if source_cfg.get("use_newsapi"):
        for it in _fetch_newsapi(config, cutoff):
            if it.id not in seen_ids:
                items.append(it)
                seen_ids.add(it.id)

    return items
