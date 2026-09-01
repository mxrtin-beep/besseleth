"""Shared "paste/upload a text file" ingestion, used by any source that
has no free structured API (LinkedIn, Luma events, etc).

Drop plain-text or Markdown files into a source's `dropbox_dir`. Each file
holds one snippet, or several separated by a line containing only `---`.
There's no required format — free text is fine — but a snippet parses
better the more it looks like this:

    <title, e.g. company/event/post name — first line>
    <optional second line: date/location/whatever>
    <a URL, anywhere in the snippet>
    <the rest: description/body text>

Concretely, for LinkedIn (dropbox_dir: linkedin_drops/), a saved file
`2026-09-08.txt` might contain:

    Neuralink - Research Scientist, Neural Interfaces
    San Francisco, CA · Posted 2 days ago
    https://www.linkedin.com/jobs/view/1234567890
    We're looking for a research scientist to join our neural interfaces
    team working on next-gen brain-computer interface implants.
    ---
    Sam Lee (Synchron) - "Excited to share our latest results at SfN..."
    https://www.linkedin.com/posts/sam-lee_neurotech-activity-1234567890

For Luma events (dropbox_dir: event_drops/), copy the event page text:

    Neurotech SF Meetup — October Demo Night
    Thu, Oct 9 · 6:00 PM PDT · San Francisco, CA
    https://lu.ma/neurotech-sf-oct
    Monthly meetup for neurotech founders, researchers, and engineers.
    This month: live BCI demos from three local startups.

Any plain-text export works — a copy-paste from the browser, a forwarded
email, a screenshot's OCR output, etc. Just save it as `.txt` or `.md`.
Processed files are moved to `<dropbox_dir>/processed/` so re-running
`fetch` never re-ingests them (the DB also dedupes by content hash, so
this is just tidiness, not a correctness requirement).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from ..db import Item
from .util import stable_id, text_matches_keywords

_SNIPPET_SEP = re.compile(r"^\s*---+\s*$", re.MULTILINE)
_URL_RE = re.compile(r"https?://\S+")


def parse_snippet(raw: str) -> tuple[str, str, str]:
    """Splits a pasted snippet into (title, url, body). Title = first
    non-empty line; url = first URL found anywhere; body = the whole thing."""
    lines = [l for l in raw.strip().splitlines() if l.strip()]
    title = lines[0].strip() if lines else raw.strip()[:120]
    url_match = _URL_RE.search(raw)
    url = url_match.group(0) if url_match else ""
    body = raw.strip()
    return title, url, body


def fetch_drops(config, dropbox_dir: str, source: str) -> list[Item]:
    """Reads user-pasted content from `dropbox_dir`, one Item per snippet,
    tagged with the given `source` (e.g. "linkedin", "event")."""
    dropbox = Path(dropbox_dir)
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
            title, url, body = parse_snippet(snippet)
            hits = text_matches_keywords(body, config.keywords)
            items.append(
                Item(
                    id=stable_id(source, url or body[:200]),
                    source=source,
                    title=title,
                    url=url,
                    summary=body,
                    published_at=datetime.now(timezone.utc).isoformat(),
                    matched_keywords=hits or ["manual"],
                )
            )
        path.rename(processed_dir / path.name)

    return items


def add_manual_item(config, db, text: str, source: str, url: str = "") -> Item:
    """Programmatic one-off ingestion (e.g. from a CLI paste command)."""
    title, parsed_url, body = parse_snippet(text)
    hits = text_matches_keywords(body, config.keywords)
    item = Item(
        id=stable_id(source, (url or parsed_url) or body[:200]),
        source=source,
        title=title,
        url=url or parsed_url,
        summary=body,
        published_at=datetime.now(timezone.utc).isoformat(),
        matched_keywords=hits or ["manual"],
    )
    db.upsert_item(item)
    return item


# --- One paste box, auto-classified -----------------------------------
#
# Domain-based heuristics for "figure out what this is" — used by the
# dashboard's single paste box and `besseleth.cli paste` so you don't
# have to know or care which specific source a link belongs to.

_DOMAIN_SOURCE_MAP = [
    (("linkedin.com",), "linkedin"),
    (("bsky.app", "twitter.com", "x.com"), "social"),
    (("lu.ma", "eventbrite.com", "meetup.com"), "event"),
    (("substack.com",), "blog"),
    (("arxiv.org",), "arxiv"),
]

# Human-facing labels for the source values above, used anywhere the UI
# shows "detected as: ...".
SOURCE_LABELS = {
    "linkedin": "LinkedIn",
    "social": "Social (Bluesky/X)",
    "event": "Event",
    "blog": "Blog",
    "arxiv": "arXiv",
    "news": "News",
    "clip": "Clipped (unrecognized source)",
}


def classify_source(text: str, url: str = "") -> str:
    """Guesses which besseleth source a pasted snippet belongs to, from
    its URL's domain (falls back to "clip" — a generic bucket — when
    nothing matches, rather than guessing wrong)."""
    _, parsed_url, _ = parse_snippet(text)
    candidate_url = (url or parsed_url or "").lower()
    for domains, source in _DOMAIN_SOURCE_MAP:
        if any(d in candidate_url for d in domains):
            return source
    return "clip"


def add_smart_item(config, db, text: str, url: str = "") -> tuple[Item, str]:
    """Auto-detects the source from the pasted text/URL and stores it
    accordingly. Returns (item, detected_source_label) — this is the
    single entry point behind the dashboard's one paste box."""
    source = classify_source(text, url)
    item = add_manual_item(config, db, text, source=source, url=url)
    return item, SOURCE_LABELS.get(source, source)
