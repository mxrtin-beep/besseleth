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
from datetime import date, datetime, timedelta, timezone

import requests

from ..db import Item
from .util import stable_id, text_matches_keywords

OPENALEX_WORKS_API = "https://api.openalex.org/works"
MAX_RESULTS_PER_KEYWORD_HARD_CAP = 1000  # backfill safety valve — see fetch()'s docstring


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
    """Fetches papers matching each configured keyword, newest first,
    stopping once results fall outside the `days_back` window.

    Paginates past `max_results_per_keyword` when needed — same fix as
    arxiv_scraper's fetch(): OpenAlex sorts newest-first and returns one
    page (of that size) per request, so without pagination a `days_back`
    of years (a deep backfill) just returns the same newest handful of
    papers over and over, no matter how far back the window is widened,
    and never actually reaches the older, more-cited papers a backfill is
    for. This also happens to be the direct explanation for citation
    counts looking suspiciously all-zero: a paper that's only ever a few
    days/weeks old genuinely hasn't had time to accumulate citations yet
    (that's real, not a bug) — but a backfill that was silently stuck on
    only ever fetching the newest page made EVERY fetched paper look that
    fresh, backfill or not, which is the bug. Stops paginating for a
    keyword once a page's oldest entry falls before the cutoff, OpenAlex
    returns fewer than a full page (no more results), or a hard cap of
    MAX_RESULTS_PER_KEYWORD_HARD_CAP total is hit."""
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).date()
    cutoff = cutoff_date.isoformat()
    items: list[Item] = []
    seen_ids: set[str] = set()
    total_seen = 0
    skipped_had_abstract = 0  # rejected by the keyword recheck WITH an abstract present to check — the real noise case

    for keyword in config.keywords:
        page = 1
        while True:
            params = {
                "search": keyword,
                "filter": f"from_publication_date:{cutoff},type:article",
                "sort": "publication_date:desc",
                "per-page": max_results_per_keyword,
                "page": page,
            }
            try:
                resp = requests.get(OPENALEX_WORKS_API, params=params, timeout=20)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                print(f"[papers] OpenAlex request failed for '{keyword}' (page={page}): {e}")
                break

            results = data.get("results", [])
            if not results:
                break

            reached_cutoff = False
            for work in results:
                openalex_id = work.get("id", "")
                title = (work.get("title") or "").strip()
                pub_date_str = work.get("publication_date") or ""
                try:
                    if pub_date_str and date.fromisoformat(pub_date_str) < cutoff_date:
                        # Sorted newest-first, so nothing from here on (this
                        # page or the next) can be back in the window either.
                        reached_cutoff = True
                        break
                except ValueError:
                    pass
                if not openalex_id or not title or openalex_id in seen_ids:
                    continue

                abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))
                total_seen += 1

                # OpenAlex's `search` param is a fuzzy, relevance-ranked
                # full-text search — it does NOT require the keyword phrase to
                # actually appear in the title/abstract, so a search for e.g.
                # "transcranial ultrasound stimulation" can return a paper
                # that just scored well on "ultrasound" alone (an unrelated
                # PSMA contrast-agent study, say). Re-check with the same
                # strict substring match arxiv_scraper uses, and skip the
                # result outright if none of our actual keywords are in there
                # — don't just fall back to tagging it with the searched
                # keyword regardless, which is what let this noise through.
                #
                # BUT only enforce this when there's an abstract to check
                # against: a large share of OpenAlex works have no abstract
                # at all (abstract_inverted_index null — many publishers
                # don't share it with OpenAlex), and a paper's TITLE alone
                # very often doesn't happen to contain a full keyword phrase
                # verbatim even when the paper is genuinely on-topic. Without
                # this carve-out, the anti-noise check above was rejecting a
                # large fraction of real, relevant results for having "only"
                # a title to verify against — trading the original noise
                # problem for a much worse recall problem (citation_count
                # data essentially never showing up because nothing from
                # OpenAlex was surviving the recheck). With no abstract, we
                # trust OpenAlex's own relevance search instead of demanding
                # local proof we don't have enough text to produce.
                hits = text_matches_keywords(f"{title} {abstract}", config.keywords)
                if not hits and abstract:
                    skipped_had_abstract += 1
                    continue
                hits = hits or [keyword]

                authors = ", ".join(
                    name for a in work.get("authorships", [])
                    if (name := (a.get("author") or {}).get("display_name"))
                )
                url = (
                    (work.get("primary_location") or {}).get("landing_page_url")
                    or work.get("doi")
                    or openalex_id
                )

                items.append(
                    Item(
                        id=stable_id("papers", openalex_id),
                        source="papers",
                        title=title,
                        url=url or "",
                        summary=abstract,
                        published_at=pub_date_str or datetime.now(timezone.utc).isoformat(),
                        matched_keywords=hits,
                        authors=authors or None,
                        citation_count=work.get("cited_by_count"),
                    )
                )
                seen_ids.add(openalex_id)

            page += 1
            time.sleep(1)  # be polite to OpenAlex's free API

            if reached_cutoff or len(results) < max_results_per_keyword or page * max_results_per_keyword >= MAX_RESULTS_PER_KEYWORD_HARD_CAP:
                break

    print(
        f"[papers] OpenAlex: saw {total_seen} candidate(s) across {len(config.keywords)} keyword(s), "
        f"kept {len(items)}, rejected {skipped_had_abstract} by the keyword recheck (had an abstract that didn't "
        f"match — the rest of the gap between seen/kept is duplicates across keywords, not rejections)."
    )
    return items
