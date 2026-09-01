"""Fetches recent arXiv papers matching industry keywords/categories.

Uses the free, public arXiv Atom API (no key required):
https://arxiv.org/help/api
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

import feedparser
import requests

from ..db import Item
from .util import stable_id, strip_html, text_matches_keywords

ARXIV_API = "http://export.arxiv.org/api/query"


def _search_query(keyword: str, categories: list[str]) -> str:
    kw = f'all:"{keyword}"'
    if categories:
        cat_q = " OR ".join(f"cat:{c}" for c in categories)
        return f"({kw}) AND ({cat_q})"
    return kw


def fetch(config, days_back: int, max_results_per_keyword: int) -> list[Item]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    items: list[Item] = []
    seen_urls: set[str] = set()

    for keyword in config.keywords:
        params = {
            "search_query": _search_query(keyword, config.arxiv_categories),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": max_results_per_keyword,
        }
        try:
            resp = requests.get(ARXIV_API, params=params, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[arxiv] request failed for '{keyword}': {e}")
            continue

        feed = feedparser.parse(resp.text)
        for entry in feed.entries:
            url = entry.get("link", "")
            if url in seen_urls:
                continue
            published_str = entry.get("published", "")
            try:
                published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            except Exception:
                published = None
            if published and published < cutoff:
                continue

            title = entry.get("title", "").strip()
            summary = strip_html(entry.get("summary", ""))
            hits = text_matches_keywords(f"{title} {summary}", config.keywords)

            items.append(
                Item(
                    id=stable_id("arxiv", url or title),
                    source="arxiv",
                    title=title,
                    url=url,
                    summary=summary,
                    published_at=(published or datetime.now(timezone.utc)).isoformat(),
                    matched_keywords=hits or [keyword],
                )
            )
            seen_urls.add(url)

        time.sleep(1)  # be polite to arXiv's free API

    return items
