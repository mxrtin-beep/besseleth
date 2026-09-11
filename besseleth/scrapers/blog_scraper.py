"""Pulls industry/company blog posts — same mechanism as news_scraper for
a plain RSS/Atom feed, but with a real backfill path for Substack
specifically (see _fetch_substack_archive's docstring for why RSS alone
can't do that). Kept as a separate source+report section since blogs
(company engineering blogs, lab blogs, researcher Substacks) read
differently from trade press and you'll likely want a different feed list.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone

import requests

from ..db import Item
from .news_scraper import fetch_feeds
from .util import stable_id, strip_html

_SUBSTACK_RE = re.compile(r"^https?://([\w-]+)\.substack\.com", re.IGNORECASE)
MAX_ARCHIVE_PAGES_PER_FEED = 200  # backfill safety valve, same spirit as arxiv_scraper's hard cap


def _substack_slug(feed_url: str) -> str | None:
    m = _SUBSTACK_RE.match(feed_url.strip())
    return m.group(1) if m else None


def _fetch_substack_archive(slug: str, cutoff_date: date, page_size: int = 25) -> list[Item]:
    """Substack's `/feed` RSS only ever exposes the most recent ~20-30
    posts — there is no RSS mechanism to page further back, so a
    publication's older history is simply unreachable through the feed
    URL no matter how far back `days_back`/a backfill asks. Substack
    itself, however, serves its own archive page (yoursubstack.com/archive)
    from a JSON API behind it — undocumented, but stable and widely relied
    on (the same one Substack's own web archive page calls) — that pages
    through EVERY post a publication has ever published, newest first.
    This is the only way to actually backfill a Substack blog; a non-
    Substack blog has no equivalent and stays feed-only (see fetch())."""
    items: list[Item] = []
    offset = 0
    for _ in range(MAX_ARCHIVE_PAGES_PER_FEED):
        try:
            resp = requests.get(
                f"https://{slug}.substack.com/api/v1/archive",
                params={"sort": "new", "search": "", "offset": offset, "limit": page_size},
                timeout=20,
            )
            resp.raise_for_status()
            posts = resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"[blog] Substack archive request failed for '{slug}' (offset={offset}): {e}")
            break
        if not posts or not isinstance(posts, list):
            break

        reached_cutoff = False
        for post in posts:
            post_date_str = (post.get("post_date") or "")[:10]
            try:
                if post_date_str and date.fromisoformat(post_date_str) < cutoff_date:
                    # Sorted newest-first, so nothing from here on (this
                    # page or any further one) can be back in the window.
                    reached_cutoff = True
                    break
            except ValueError:
                pass

            title = (post.get("title") or "").strip()
            url = post.get("canonical_url") or ""
            if not title or not url:
                continue
            summary = strip_html(post.get("description") or post.get("subtitle") or post.get("truncated_body_text") or "")
            items.append(
                Item(
                    id=stable_id("blog", url),
                    source="blog",
                    title=title,
                    url=url,
                    summary=summary,
                    published_at=(post.get("post_date") or datetime.now(timezone.utc).isoformat()),
                    matched_keywords=[],  # blogs are trusted wholesale — see fetch()'s docstring
                )
            )

        offset += len(posts)
        time.sleep(1)  # be polite — this is an unofficial endpoint, not a published API

        if reached_cutoff or len(posts) < page_size:
            break

    return items


def fetch(config, source_cfg: dict, days_back: int) -> list[Item]:
    # require_keyword_match=False (for the plain-RSS path below):
    # unlike news.feeds (broad wire/topic feeds that need keyword
    # filtering to cut noise), a blog feed is something you deliberately
    # added because it's a specific neurotech company/lab/researcher's
    # blog — most of its posts won't happen to contain one of your
    # keyword phrases even though the whole feed is exactly on-topic, so
    # filtering post-by-post here was silently dropping nearly everything
    # from every blog feed you configured.
    feeds = source_cfg.get("feeds", [])
    substack_feeds = [f for f in feeds if _substack_slug(f)]
    other_feeds = [f for f in feeds if not _substack_slug(f)]

    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).date()
    items: list[Item] = []
    for feed_url in substack_feeds:
        slug = _substack_slug(feed_url)
        items += _fetch_substack_archive(slug, cutoff_date)

    items += fetch_feeds(config, other_feeds, days_back, source="blog", require_keyword_match=False)
    return items
