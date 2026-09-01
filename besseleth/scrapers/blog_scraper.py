"""Pulls industry/company blog posts from RSS/Atom feeds — same mechanism
as news_scraper, kept as a separate source+report section since blogs
(company engineering blogs, lab blogs, researcher Substacks) read
differently from trade press and you'll likely want a different feed list.
"""
from __future__ import annotations

from ..db import Item
from .news_scraper import fetch_feeds


def fetch(config, source_cfg: dict, days_back: int) -> list[Item]:
    return fetch_feeds(config, source_cfg.get("feeds", []), days_back, source="blog")
