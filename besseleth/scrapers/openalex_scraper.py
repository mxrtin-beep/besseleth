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

Overlap with arXiv is handled at fetch time, not left for report-time
dedup: arxiv_scraper checks each preprint against OpenAlex by DOI first
and, when found here, uses THIS module's (richer, real-affiliation)
version instead of storing a second, thinner arXiv-sourced row for the
same paper — see arxiv_scraper.fetch()'s docstring.

Org identity (which specific institution/company produced a paper) is
resolved HERE, at fetch time, not in the later general enrichment pass — see
paper_org.py's module docstring for why (the real author-institution
data this needs only exists in the API response right now, at fetch
time; it's gone by the time general enrichment runs). This replaced an
older approach (an LLM guessing org from a bare title+summary, with a
labs.yaml-based override) that was producing confidently wrong answers.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone

import requests

from .. import paper_org, web_lookup
from ..cancel import check_cancelled
from ..config import Config
from ..db import Item
from .util import stable_id, text_matches_keywords

OPENALEX_WORKS_API = "https://api.openalex.org/works"
MAX_RESULTS_PER_KEYWORD_HARD_CAP = 1000  # backfill safety valve — see fetch()'s docstring
LAB_AUTHOR_RECHECK_DAYS = 30  # see _resolve_author_id's docstring


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


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


def _work_to_item(
    work: dict, openalex_id: str, title: str, abstract: str, pub_date_str: str, matched_keywords: list[str],
    config: Config | None = None,
) -> Item:
    """Builds an Item from one OpenAlex work record — shared by fetch()
    (keyword search), fetch_known_lab_papers() (author search), and
    arxiv_scraper.fetch() (when an arXiv preprint turns out to already be
    indexed here), which differ only in HOW they decided this work is
    relevant, not in how to turn the record itself into an Item.

    Resolves org/org_type right here from the work's own authorship data
    (see paper_org.py) when `config` is given — the one moment this real
    affiliation data is available. `config=None` skips resolution
    (leaves org/org_type unset) purely for tests/callers that don't need
    it; every real caller in this codebase passes it."""
    authors_institutions = web_lookup.authorships_from_work(work)
    authors = ", ".join(name for name, _, _ in authors_institutions)
    org, org_type = (
        paper_org.resolve_paper_org_with_fallback(title, authors_institutions, config) if config else (None, None)
    )
    url = (
        (work.get("primary_location") or {}).get("landing_page_url")
        or work.get("doi")
        or openalex_id
    )
    return Item(
        id=stable_id("papers", openalex_id),
        source="papers",
        title=title,
        url=url or "",
        summary=abstract,
        published_at=pub_date_str or datetime.now(timezone.utc).isoformat(),
        matched_keywords=matched_keywords,
        authors=authors or None,
        citation_count=work.get("cited_by_count"),
        openalex_id=openalex_id or None,
        org=org,
        org_type=org_type,
    )


def fetch(config, days_back: int, max_results_per_keyword: int, mailto: str | None = None, api_key: str | None = None, cancel_event=None) -> list[Item]:
    """Fetches papers matching each configured keyword, newest first,
    stopping once results fall outside the `days_back` window.

    Paginates past `max_results_per_keyword` when needed — same fix as
    arxiv_scraper's fetch(): OpenAlex sorts newest-first and returns one
    page (of that size) per request, so without pagination a `days_back`
    of years (a deep backfill) just returns the same newest handful of
    papers over and over, no matter how far back the window is widened,
    and never actually reaches the older, more-cited papers a backfill is
    for. Stops paginating for a keyword once a page's oldest entry falls
    before the cutoff, OpenAlex returns fewer than a full page (no more
    results), or a hard cap of MAX_RESULTS_PER_KEYWORD_HARD_CAP total is
    hit.

    A deep backfill pages through a LOT of requests (page=14+ for one
    keyword alone isn't unusual), and OpenAlex's rate limit without an
    API key is tight enough that a sustained backfill routinely trips
    429s — which was the actual, direct explanation for citation counts
    looking all-zero: most requests were failing outright, not being
    filtered, so almost nothing from OpenAlex was ever actually stored.
    `api_key` (pass yours via sources.papers.openalex_api_key in
    config.yaml, or the OPENALEX_API_KEY env var — see
    https://openalex.org/settings/api for a free key) is what actually
    grants a real rate limit now; OpenAlex retired the old mailto-based
    "polite pool" and free anonymous calls are capped low enough to be
    unusable for real fetching. `mailto` is still sent too (harmless,
    and identifies the client in the User-Agent either way), but no
    longer does anything for rate limits on its own. Combined with
    retry-with-backoff on a 429 (same pattern as arxiv_scraper's fix),
    this should get a deep backfill through without the sustained
    failure cascade you'd get from hammering the free tier with no key
    and no pacing at all.

    filter=type:article|review — OpenAlex classifies works by a real
    `type` field (article, review, preprint, book-chapter, editorial,
    letter, dataset, ...), distinct from its own subject/relevance
    matching; a perspective/review piece that's still genuine, on-topic
    field coverage (survey articles, "state of the field" pieces — real
    Science Robotics/Nature Reviews-style content a briefing tool should
    plausibly want) was type "review", not "article", and got silently
    excluded before this covered both. Deliberately still excludes
    "preprint" (arxiv_scraper already covers preprints — this scraper's
    whole reason to exist is what's actually been formally published,
    see the module docstring) and other non-article types (editorial,
    letter, dataset, book-chapter, ...) that are rarely real research
    content on their own."""
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).date()
    cutoff = cutoff_date.isoformat()
    items: list[Item] = []
    seen_ids: set[str] = set()
    total_seen = 0
    skipped_had_abstract = 0  # rejected by the keyword recheck WITH an abstract present to check — the real noise case

    for keyword in config.keywords:
        page = 1
        while True:
            check_cancelled(cancel_event)
            params = {
                "search": keyword,
                "filter": f"from_publication_date:{cutoff},type:article|review",
                "sort": "publication_date:desc",
                "per-page": max_results_per_keyword,
                "page": page,
            }
            if mailto:
                params["mailto"] = mailto
            if api_key:
                params["api_key"] = api_key  # grants OpenAlex's real rate limit — see fetch()'s docstring
            headers = {"User-Agent": f"besseleth/1.0 (industry-briefing tool{f'; mailto:{mailto}' if mailto else ''})"}

            data = None
            for attempt in range(3):
                try:
                    resp = requests.get(OPENALEX_WORKS_API, params=params, headers=headers, timeout=20)
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except (requests.RequestException, ValueError) as e:
                    is_rate_limited = getattr(e, "response", None) is not None and e.response.status_code == 429
                    if attempt == 2:
                        print(f"[papers] OpenAlex request failed for '{keyword}' (page={page}) after 3 attempts: {e}")
                        data = None
                        break
                    backoff = 15 * (attempt + 1) if is_rate_limited else 5 * (attempt + 1)
                    print(f"[papers] OpenAlex request failed for '{keyword}' (page={page}): {e} — retrying in {backoff}s")
                    time.sleep(backoff)
            if data is None:
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

                items.append(_work_to_item(work, openalex_id, title, abstract, pub_date_str, hits, config=config))
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


OPENALEX_AUTHORS_API = "https://api.openalex.org/authors"
MAX_RESULTS_PER_AUTHOR_HARD_CAP = 200  # a backfill safety valve, same idea as MAX_RESULTS_PER_KEYWORD_HARD_CAP


def _resolve_author_id(db, pi: str, university: str, mailto: str | None, api_key: str | None, headers: dict) -> str | None:
    """Finds the OpenAlex author id for a labs.yaml {pi, university}
    entry — cached in lab_author_cache (see db.py) so this search only
    ever runs once per lab, not on every fetch. OpenAlex's author search
    is fuzzy/relevance-ranked, so the top hit for a common surname alone
    could easily be the wrong person entirely; requiring `university` to
    actually match somewhere in that author's own listed institution
    history is the same "don't guess when ambiguous" standard the rest
    of labs.yaml matching uses (see _match_known_lab). Matched on
    squashed (lowercased, punctuation/whitespace-stripped) text in
    EITHER direction — labs.yaml's own "Brown"/"Johns Hopkins" style
    short form vs. OpenAlex's fuller "Brown University"/"Johns Hopkins
    University" is a real, common mismatch a one-directional substring
    check misses depending on which one happens to be the longer string.

    Returns None if nothing sufficiently confident was found — a
    common/short surname with no matching institution, or an author
    OpenAlex just doesn't have. A None result is cached, but only for
    LAB_AUTHOR_RECHECK_DAYS: caching it forever (the original behavior)
    meant a genuinely well-known, prolific author could go permanently
    unresolved — and so silently absent from every future fetch no
    matter how many times labs.yaml is re-run — over a single
    transient miss (an OpenAlex hiccup, a name/institution phrasing
    that didn't match at the time), with no way to recover short of
    manually clearing the row from the database."""
    cached = db.get_lab_author_cache(pi, university)
    if cached and cached["author_id"]:
        return cached["author_id"]
    if cached and not cached["author_id"]:
        checked_at = datetime.fromisoformat(cached["checked_at"])
        if datetime.now(timezone.utc) - checked_at < timedelta(days=LAB_AUTHOR_RECHECK_DAYS):
            return None  # checked recently, nothing found — don't re-probe yet

    try:
        resp = requests.get(
            OPENALEX_AUTHORS_API,
            params={
                "search": f"{pi} {university}",
                "per_page": 5,
                **({"mailto": mailto} if mailto else {}),
                **({"api_key": api_key} if api_key else {}),
            },
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        candidates = resp.json().get("results", [])
    except requests.RequestException as e:
        print(f"[papers] OpenAlex author search failed for {pi!r} ({university}): {e}")
        return None  # NOT cached — a transient network failure shouldn't lock this lab out permanently

    university_squashed = _squash(university)
    for candidate in candidates:
        institutions = candidate.get("affiliations") or candidate.get("last_known_institutions") or []
        names = [
            (inst.get("institution") or inst).get("display_name", "")
            if isinstance(inst.get("institution", inst), dict) else ""
            for inst in institutions
        ]
        if any(
            university_squashed and (university_squashed in (n_squashed := _squash(n or "")) or n_squashed in university_squashed)
            for n in names
        ):
            author_id = (candidate.get("id") or "").rsplit("/", 1)[-1]
            db.set_lab_author_cache(pi, university, author_id)
            return author_id

    db.set_lab_author_cache(pi, university, None)
    return None


def fetch_known_lab_papers(
    config, db, days_back: int, max_results_per_author: int = 25, mailto: str | None = None,
    api_key: str | None = None, cancel_event=None
) -> list[Item]:
    """Pulls a labs.yaml-listed PI's own papers directly from OpenAlex
    by AUTHOR, not by your keyword list — the actual fix for "a lab I
    know is active isn't showing up": fetch()'s keyword search only ever
    finds a paper whose title/abstract happens to contain one of your
    configured phrases verbatim, which plenty of genuinely on-topic work
    from a real, named lab just doesn't (different terminology, a
    methods-focused title, etc). Once a PI is on your own list, their
    own papers are relevant by definition — no keyword recheck needed,
    unlike fetch()'s noise-filtering pass (see its docstring for why
    that exists there but shouldn't apply here).

    Deliberately NOT a substitute for website scraping (which was the
    original ask) — every lab's own site is differently formatted,
    often JS-rendered, and scraping each one would be exactly the kind
    of brittle, high-maintenance per-site scraper this codebase avoids
    elsewhere (job postings use each ATS's real API, not scraped HTML,
    for the same reason). An author-identity search against a real API
    besseleth already integrates with is the reliable version of the
    same idea.

    Only labs with BOTH `pi` and `university` set are attempted — an
    author search needs the institution to disambiguate a common
    surname (see _resolve_author_id); a labs.yaml entry with no
    university is used elsewhere (deterministic org-naming) but skipped
    here."""
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).date()
    cutoff = cutoff_date.isoformat()
    headers = {"User-Agent": f"besseleth/1.0 (industry-briefing tool{f'; mailto:{mailto}' if mailto else ''})"}

    items: list[Item] = []
    seen_ids: set[str] = set()
    for lab in config.labs:
        check_cancelled(cancel_event)  # between labs — could be a long list
        pi, university = lab.get("pi"), lab.get("university")
        if not pi or not university:
            continue
        author_id = _resolve_author_id(db, pi, university, mailto, api_key, headers)
        if not author_id:
            continue

        page = 1
        while True:
            check_cancelled(cancel_event)
            params = {
                "filter": f"author.id:{author_id},from_publication_date:{cutoff},type:article|review",
                "sort": "publication_date:desc",
                "per-page": max_results_per_author,
                "page": page,
            }
            if mailto:
                params["mailto"] = mailto
            if api_key:
                params["api_key"] = api_key
            try:
                resp = requests.get(OPENALEX_WORKS_API, params=params, headers=headers, timeout=20)
                resp.raise_for_status()
                results = resp.json().get("results", [])
            except requests.RequestException as e:
                print(f"[papers] OpenAlex works lookup failed for {pi} (author {author_id}): {e}")
                break
            if not results:
                break

            reached_cutoff = False
            for work in results:
                openalex_id = work.get("id", "")
                title = (work.get("title") or "").strip()
                pub_date_str = work.get("publication_date") or ""
                try:
                    if pub_date_str and date.fromisoformat(pub_date_str) < cutoff_date:
                        reached_cutoff = True
                        break
                except ValueError:
                    pass
                if not openalex_id or not title or openalex_id in seen_ids:
                    continue
                abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))
                # Tags the item as labs.yaml-sourced (not a real keyword
                # hit) — still passes report.py/dedupe.py's ordinary
                # "has at least one matched_keywords entry" checks, and
                # is visible on the item if you ever want to filter by it.
                items.append(_work_to_item(work, openalex_id, title, abstract, pub_date_str, [f"known_lab:{pi}"], config=config))
                seen_ids.add(openalex_id)

            page += 1
            time.sleep(1)  # be polite to OpenAlex's free API
            if reached_cutoff or len(results) < max_results_per_author or page * max_results_per_author >= MAX_RESULTS_PER_AUTHOR_HARD_CAP:
                break

    if items:
        print(f"[papers] OpenAlex (known labs): {len(items)} paper(s) from {len({i.matched_keywords[0] for i in items})} labs.yaml PI(s).")
    return items
