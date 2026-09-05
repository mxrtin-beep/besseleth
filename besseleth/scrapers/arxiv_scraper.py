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


MAX_RESULTS_PER_KEYWORD_HARD_CAP = 1000  # backfill safety valve — see fetch()'s docstring


def fetch(config, days_back: int, max_results_per_keyword: int) -> list[Item]:
    """Fetches papers matching each configured keyword, newest first,
    stopping once results fall outside the `days_back` window.

    Paginates past `max_results_per_keyword` when needed: arXiv's API
    returns one page (of that size) per request, sorted newest first, so
    a single page only ever holds the *newest* N papers — a `days_back`
    of years (a deep backfill) needs several pages to actually reach that
    far back, not just a bigger cutoff applied to the same handful of
    recent results. Stops paginating for a keyword once a page's oldest
    entry falls before the cutoff (everything after it, on this page and
    any further one, is older still) or a hard cap of
    MAX_RESULTS_PER_KEYWORD_HARD_CAP total is hit, so an extremely broad
    keyword/category combo with a multi-year backfill can't page forever."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    items: list[Item] = []
    seen_urls: set[str] = set()

    for keyword in config.keywords:
        start = 0
        while True:
            params = {
                "search_query": _search_query(keyword, config.arxiv_categories),
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "start": start,
                "max_results": max_results_per_keyword,
            }
            try:
                resp = requests.get(ARXIV_API, params=params, timeout=20)
                resp.raise_for_status()
            except requests.RequestException as e:
                print(f"[arxiv] request failed for '{keyword}' (start={start}): {e}")
                break

            feed = feedparser.parse(resp.text)
            if not feed.entries:
                break

            reached_cutoff = False
            for entry in feed.entries:
                url = entry.get("link", "")
                try:
                    published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    published = None
                if published and published < cutoff:
                    # Sorted newest-first, so nothing from here on (this
                    # page or the next) can be back in the window either.
                    reached_cutoff = True
                    break
                if url in seen_urls:
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

            start += len(feed.entries)
            time.sleep(3)  # arXiv's own usage policy asks for >=3s between paginated requests

            if reached_cutoff or len(feed.entries) < max_results_per_keyword or start >= MAX_RESULTS_PER_KEYWORD_HARD_CAP:
                break

    return items
