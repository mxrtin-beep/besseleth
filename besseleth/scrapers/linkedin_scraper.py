"""LinkedIn source.

LinkedIn's Terms of Service prohibit automated scraping of linkedin.com,
and it actively detects/blocks bot traffic (see LinkedIn v. hiQ Labs and
LinkedIn's current User Agreement §8.2). This module intentionally does
NOT scrape linkedin.com.

Three ways to get LinkedIn-sourced signal instead, roughly cheapest-to-
most-official:

  1. **Manual paste/upload (this module, `fetch_manual_drops`)** — free,
     zero API keys, no ToS risk. You copy a post, job listing, or "who
     viewed/posted" digest off LinkedIn yourself and drop it in a folder
     (or paste it via the CLI); besseleth treats it like any other item —
     matched against keywords, run through personalization, and folded
     into the weekly report. This is the default/recommended path below.

  2. A licensed third-party data provider such as Proxycurl, Coresignal,
     or Bright Data's compliant LinkedIn dataset — pay-as-you-go API key.

  3. LinkedIn Talent/Marketing APIs (official, requires partner approval) —
     https://learn.microsoft.com/en-us/linkedin/

  4. RSS feeds of company newsrooms/blogs as a free, ToS-safe proxy for
     "what's happening at company X" — configure these under
     sources.news.feeds instead of here.

If you have a provider from (2) or (3), implement `fetch()` below to call
it and return `Item`s the same way the other scrapers do.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from ..db import Item
from .util import stable_id, text_matches_keywords

# --- Manual paste/upload fallback -----------------------------------------
#
# Drop plain-text or .md files into the configured `dropbox_dir` (default:
# "linkedin_drops/"), one per item, or several separated by a line of just
# "---". Each snippet becomes an Item exactly like a scraped one: matched
# against industry.keywords and, downstream, checked against your
# contacts' companies for personalization (e.g. "Jane's company Neuralink
# is hiring" gets flagged even though you pasted it in by hand).
#
# Typical snippet, saved as e.g. linkedin_drops/2026-09-08.txt:
#
#   Neuralink - Research Scientist, Neural Interfaces
#   San Francisco, CA · Posted 2 days ago
#   https://www.linkedin.com/jobs/view/1234567890
#   We're looking for a research scientist to join our neural interfaces team...

_SNIPPET_SEP = re.compile(r"^\s*---+\s*$", re.MULTILINE)
_URL_RE = re.compile(r"https?://\S+")


def _parse_snippet(raw: str) -> tuple[str, str, str]:
    """Splits a pasted snippet into (title, url, body). Title = first
    non-empty line; url = first URL found anywhere; body = the rest."""
    lines = [l for l in raw.strip().splitlines() if l.strip()]
    title = lines[0].strip() if lines else raw.strip()[:120]
    url_match = _URL_RE.search(raw)
    url = url_match.group(0) if url_match else ""
    body = raw.strip()
    return title, url, body


def fetch_manual_drops(config, source_cfg: dict) -> list[Item]:
    """Reads user-pasted LinkedIn content from `dropbox_dir`. Each file's
    snippets become Items; matched files are moved to `dropbox_dir/processed/`
    so re-running doesn't re-ingest them (the DB dedupes anyway, by content
    hash, so this is just tidiness)."""
    dropbox = Path(source_cfg.get("dropbox_dir", "linkedin_drops"))
    if not dropbox.exists():
        return []

    processed_dir = dropbox / "processed"
    processed_dir.mkdir(exist_ok=True)

    items: list[Item] = []
    for path in sorted(dropbox.glob("*")):
        if path.is_dir() or path.suffix not in (".txt", ".md", ""):
            continue
        raw = path.read_text(errors="ignore")
        for snippet in _SNIPPET_SEP.split(raw):
            if not snippet.strip():
                continue
            title, url, body = _parse_snippet(snippet)
            hits = text_matches_keywords(body, config.keywords)
            items.append(
                Item(
                    id=stable_id("linkedin", url or body[:200]),
                    source="linkedin",
                    title=title,
                    url=url,
                    summary=body,
                    published_at=datetime.now(timezone.utc).isoformat(),
                    matched_keywords=hits or ["manual"],
                )
            )
        path.rename(processed_dir / path.name)

    return items


def add_manual_item(config, db, text: str, url: str = "") -> Item:
    """Programmatic one-off ingestion, e.g. from `besseleth.cli linkedin-add`.
    Skips the dropbox file dance for a quick `paste and go`."""
    title, parsed_url, body = _parse_snippet(text)
    hits = text_matches_keywords(body, config.keywords)
    item = Item(
        id=stable_id("linkedin", (url or parsed_url) or body[:200]),
        source="linkedin",
        title=title,
        url=url or parsed_url,
        summary=body,
        published_at=datetime.now(timezone.utc).isoformat(),
        matched_keywords=hits or ["manual"],
    )
    db.upsert_item(item)
    return item


# --- Licensed API path (stub) ----------------------------------------------


def fetch(config, source_cfg: dict) -> list[Item]:
    items = fetch_manual_drops(config, source_cfg)

    provider = source_cfg.get("provider")
    if provider and provider != "manual":
        print(
            f"[linkedin] Provider {provider!r} configured but not implemented — "
            f"see linkedin_scraper.py docstring for how to wire one in. "
            f"Falling back to manual drops only ({len(items)} found)."
        )
    elif not items:
        print(
            "[linkedin] No manual drops found. Paste LinkedIn content into "
            f"{source_cfg.get('dropbox_dir', 'linkedin_drops')}/ as .txt files, "
            "or run `python -m besseleth.cli linkedin-add` to paste one from stdin."
        )

    return items
