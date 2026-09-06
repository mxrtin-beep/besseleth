"""Pulls industry/company blog posts from RSS/Atom feeds — same mechanism
as news_scraper, kept as a separate source+report section since blogs
(company engineering blogs, lab blogs, researcher Substacks) read
differently from trade press and you'll likely want a different feed list.
"""
from __future__ import annotations

from ..db import Item
from .news_scraper import fetch_feeds


def fetch(config, source_cfg: dict, days_back: int) -> list[Item]:
    # require_keyword_match=False: unlike news.feeds (broad wire/topic
    # feeds that need keyword filtering to cut noise), a blog feed is
    # something you deliberately added because it's a specific neurotech
    # company/lab/researcher's blog — most of its posts won't happen to
    # contain one of your keyword phrases even though the whole feed is
    # exactly on-topic, so filtering post-by-post here was silently
    # dropping nearly everything from every blog feed you configured.
    return fetch_feeds(config, source_cfg.get("feeds", []), days_back, source="blog", require_keyword_match=False)
