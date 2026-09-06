"""Fetches non-arXiv academic papers — published journal articles and
conference papers, not preprints — matching industry keywords, via
OpenAlex (https://openalex.org): a free, keyless, comprehensive
scholarly index (aggregated from Crossref/ORCID/publishers/PubMed/
institutional repositories).

Complements arxiv_scraper, doesn't replace it: arXiv only ever has
preprints, and only in whatever categories you configure. This covers
what's actually been formally published elsewhere — most invasive-BCI
and clinical work, for instance, publishes in a journal and never
touches arXiv at all — and comes with a `cited_by_count` arXiv preprints
don't have, letting you rank by actual impact rather than just recency.

Deliberate overlap with arXiv is possible and fine: a paper that started
as an arXiv preprint and later got published shows up as two items (one
per source, different ids), same as a LinkedIn post and a news article
about the same story do — dedupe.py's near-duplicate merge at report
time (title/text similarity) collapses these the same way.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import requests

from ..db import Item
from .util import stable_id, text_matches_keywords

OPENALEX_WORKS_API = "https://api.openalex.org/works"


def _reconstruct_abstract(inverted_index: dict | None) -> str:
    """OpenAlex gives abstracts as a word->positions "inverted index"
    (a copyright-driven quirk of their API — they can't redistribute
    publisher text verbatim, but an inverted index isn't the text
    itself) instead of plain text. Rebuilds the plain text from it."""
    if not inverted_index:
        return ""
    positions: dict[int, str] = {}
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


def fetch(config, days_back: int, max_results_per_keyword: int) -> list[Item]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).date().isoformat()
    items: list[Item] = []
    seen_ids: set[str] = set()

    for keyword in config.keywords:
        params = {
            "search": keyword,
            "filter": f"from_publication_date:{cutoff},type:article",
            "sort": "publication_date:desc",
            "per-page": max_results_per_keyword,
        }
        try:
            resp = requests.get(OPENALEX_WORKS_API, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"[papers] OpenAlex request failed for '{keyword}': {e}")
            continue

        for work in data.get("results", []):
            openalex_id = work.get("id", "")
            title = (work.get("title") or "").strip()
            if not openalex_id or not title or openalex_id in seen_ids:
                continue

            abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))
            authors = ", ".join(
                name for a in work.get("authorships", [])
                if (name := (a.get("author") or {}).get("display_name"))
            )
            url = (
                (work.get("primary_location") or {}).get("landing_page_url")
                or work.get("doi")
                or openalex_id
            )
            hits = text_matches_keywords(f"{title} {abstract}", config.keywords) or [keyword]

            items.append(
                Item(
                    id=stable_id("papers", openalex_id),
                    source="papers",
                    title=title,
                    url=url or "",
                    summary=abstract,
                    published_at=work.get("publication_date") or datetime.now(timezone.utc).isoformat(),
                    matched_keywords=hits,
                    authors=authors or None,
                    citation_count=work.get("cited_by_count"),
                )
            )
            seen_ids.add(openalex_id)

        time.sleep(1)  # be polite to OpenAlex's free API

    return items
