"""LinkedIn source.

LinkedIn's Terms of Service prohibit automated scraping of linkedin.com,
and it actively detects/blocks bot traffic (see LinkedIn v. hiQ Labs and
LinkedIn's current User Agreement §8.2). This module intentionally does
NOT scrape linkedin.com.

Default path: paste/upload content yourself — see `manual_drop.py`'s
docstring for exactly what to save and where (dropbox_dir defaults to
`linkedin_drops/`). It's free, needs no API keys, and flows through the
normal pipeline: keyword matching, personalization against your contacts'
companies, and the weekly report.

Other options, in case you have access to them:

  - A licensed third-party data provider such as Proxycurl, Coresignal,
    or Bright Data's compliant LinkedIn dataset — pay-as-you-go API key.
  - LinkedIn Talent/Marketing APIs (official, requires partner approval) —
    https://learn.microsoft.com/en-us/linkedin/
  - Company newsroom/blog RSS feeds as a free, ToS-safe proxy for "what's
    happening at company X" — configure these under sources.blogs.feeds
    instead of here.

If you have a provider from the first two, implement `fetch()` below to
call it and return `Item`s the same way the other scrapers do.
"""
from __future__ import annotations

from ..db import Item
from .manual_drop import add_manual_item as _add_manual_item
from .manual_drop import fetch_drops


def add_manual_item(config, db, text: str, url: str = "") -> Item:
    return _add_manual_item(config, db, text, source="linkedin", url=url)


def fetch(config, source_cfg: dict) -> list[Item]:
    dropbox_dir = source_cfg.get("dropbox_dir", "linkedin_drops")
    items = fetch_drops(config, dropbox_dir, source="linkedin")

    provider = source_cfg.get("provider")
    if provider and provider != "manual":
        print(
            f"[linkedin] Provider {provider!r} configured but not implemented — "
            f"see linkedin_scraper.py docstring for how to wire one in. "
            f"Falling back to manual drops only ({len(items)} found)."
        )
    elif not items:
        print(
            f"[linkedin] No manual drops found. Paste LinkedIn content into "
            f"{dropbox_dir}/ as .txt files, or run "
            f"`python -m besseleth.cli linkedin-add` to paste one from stdin."
        )

    return items
