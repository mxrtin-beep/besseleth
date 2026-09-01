"""LinkedIn source — stub.

LinkedIn's Terms of Service prohibit automated scraping of linkedin.com,
and it actively detects/blocks bot traffic (see LinkedIn v. hiQ Labs and
LinkedIn's current User Agreement §8.2). This module intentionally does
NOT scrape linkedin.com.

To get LinkedIn-sourced signal (job postings, people/company updates),
use a licensed path instead:

  1. LinkedIn Talent/Marketing APIs (official, requires partner approval) —
     https://learn.microsoft.com/en-us/linkedin/
  2. A licensed third-party data provider such as Proxycurl, Coresignal,
     or Bright Data's compliant LinkedIn dataset — most offer a
     pay-as-you-go API key.
  3. RSS feeds of company newsrooms/blogs as a free, ToS-safe proxy for
     "what's happening at company X" (configure these under
     sources.news.feeds instead — most companies publish one).

If you have one of these, implement `fetch()` below to call it and return
`Item`s the same way the other scrapers do; wire it into
besseleth/pipeline.py where `linkedin_scraper` is currently skipped.
"""
from __future__ import annotations

from ..db import Item


def fetch(config, source_cfg: dict) -> list[Item]:
    provider = source_cfg.get("provider")
    print(
        f"[linkedin] Skipped — no compliant scraper implemented. "
        f"Configured provider: {provider!r}. See linkedin_scraper.py docstring "
        f"for licensed alternatives, or add company newsroom RSS feeds under "
        f"sources.news.feeds as a free substitute."
    )
    return []
