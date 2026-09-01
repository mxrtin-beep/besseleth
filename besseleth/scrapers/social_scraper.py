"""Social sources: Bluesky (free public API) and X/Twitter (paid API,
manual-paste fallback).

**Bluesky** — the AT Protocol exposes a free, unauthenticated public search
endpoint (`app.bsky.feed.searchPosts`), so `fetch_bluesky()` just calls it
directly with your keywords. No account or API key needed.

**X/Twitter** — since 2023 X's API has no free search tier that works for
this use case (the free tier is post/reply only, no search); a paid
Basic/Pro plan is required for `GET /2/tweets/search/recent`.
`fetch_twitter()` supports that path if you have a bearer token
(`X_BEARER_TOKEN`), and otherwise degrades to the same paste/upload
fallback the other no-free-API sources use (`manual_drop.py` — drop files
into `dropbox_dir`, default `social_drops/`, or run
`python -m besseleth.cli social-add`).

**Substack** needs no code here at all — every Substack publishes a
standard RSS feed at `https://<name>.substack.com/feed`. Add those URLs to
`sources.blogs.feeds` in config.yaml and they'll flow through the existing
blog scraper.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from ..config import env
from ..db import Item
from .manual_drop import add_manual_item as _add_manual_item
from .manual_drop import fetch_drops
from .util import stable_id, text_matches_keywords

BLUESKY_SEARCH_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"


def fetch_bluesky(config, source_cfg: dict, days_back: int) -> list[Item]:
    if not source_cfg.get("enabled", True):
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    limit = source_cfg.get("max_results_per_keyword", 25)
    items: list[Item] = []
    seen: set[str] = set()

    for keyword in config.keywords:
        try:
            resp = requests.get(
                BLUESKY_SEARCH_URL,
                params={"q": keyword, "limit": limit, "sort": "latest"},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"[bluesky] search failed for '{keyword}': {e}")
            continue

        for post in data.get("posts", []):
            record = post.get("record", {}) or {}
            text = record.get("text", "") or ""
            created_at = record.get("createdAt", "")
            try:
                published = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError:
                published = datetime.now(timezone.utc)
            if published < cutoff:
                continue

            author = (post.get("author") or {}).get("handle", "")
            uri = post.get("uri", "")
            post_id = uri.rsplit("/", 1)[-1] if uri else text[:50]
            url = f"https://bsky.app/profile/{author}/post/{post_id}" if author and post_id else ""
            hits = text_matches_keywords(text, config.keywords) or [keyword]

            item_id = stable_id("social", url or uri or text[:200])
            if item_id in seen:
                continue
            seen.add(item_id)
            items.append(
                Item(
                    id=item_id,
                    source="social",
                    title=f"@{author}: {text[:80]}" if author else text[:80],
                    url=url,
                    summary=text,
                    published_at=published.isoformat(),
                    matched_keywords=hits,
                )
            )
    return items


def fetch_twitter(config, source_cfg: dict, days_back: int) -> list[Item]:
    if not source_cfg.get("enabled", True):
        return []
    token = env("X_BEARER_TOKEN")
    if not token:
        return []  # caller falls back to manual drops; see module docstring

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    items: list[Item] = []
    for keyword in config.keywords:
        try:
            resp = requests.get(
                "https://api.twitter.com/2/tweets/search/recent",
                params={
                    "query": f'"{keyword}" -is:retweet',
                    "max_results": min(source_cfg.get("max_results_per_keyword", 25), 100),
                    "tweet.fields": "created_at,author_id",
                    "start_time": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"[twitter] search failed for '{keyword}': {e}")
            continue

        for tweet in data.get("data", []):
            text = tweet.get("text", "")
            tweet_id = tweet.get("id", "")
            url = f"https://x.com/i/web/status/{tweet_id}" if tweet_id else ""
            hits = text_matches_keywords(text, config.keywords) or [keyword]
            items.append(
                Item(
                    id=stable_id("social", url or text[:200]),
                    source="social",
                    title=text[:80],
                    url=url,
                    summary=text,
                    published_at=tweet.get("created_at", datetime.now(timezone.utc).isoformat()),
                    matched_keywords=hits,
                )
            )
    return items


def add_manual_item(config, db, text: str, url: str = "") -> Item:
    return _add_manual_item(config, db, text, source="social", url=url)


def fetch(config, source_cfg: dict, days_back: int) -> list[Item]:
    items = []
    items += fetch_bluesky(config, source_cfg.get("bluesky", {}) or {}, days_back)

    twitter_cfg = source_cfg.get("twitter", {}) or {}
    twitter_items = fetch_twitter(config, twitter_cfg, days_back)
    items += twitter_items
    if twitter_cfg.get("enabled", True) and not twitter_items and not env("X_BEARER_TOKEN"):
        print(
            "[twitter] No X_BEARER_TOKEN set (free X search API no longer exists) — "
            "skipping live search. Paste tweets/posts worth tracking into "
            f"{source_cfg.get('dropbox_dir', 'social_drops')}/ instead, or run "
            "`python -m besseleth.cli social-add`."
        )

    dropbox_dir = source_cfg.get("dropbox_dir", "social_drops")
    items += fetch_drops(config, dropbox_dir, source="social")

    seen = set()
    unique = []
    for it in items:
        if it.id not in seen:
            unique.append(it)
            seen.add(it.id)
    return unique
