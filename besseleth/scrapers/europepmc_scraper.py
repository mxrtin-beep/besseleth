"""Fetches recent bioRxiv/medRxiv/ChemRxiv/Research Square preprints
matching industry keywords, via Europe PMC's free, keyless REST API:
https://europepmc.org/RestfulWebService

Why Europe PMC and not bioRxiv's own API: bioRxiv/medRxiv's own public
API (api.biorxiv.org) only lists papers by DATE RANGE or DOI — it has no
full-text/keyword search at all, so it can't answer "papers matching
'senolytic'" the way arXiv's or OpenAlex's API can. Europe PMC indexes
bioRxiv, medRxiv, ChemRxiv, and Research Square under one umbrella (real
keyword search, `SRC:PPR` restricts results to preprints specifically,
not the published-journal-paper literature Europe PMC also indexes) —
one integration covering "arXiv's parallels" in biology/medicine/
chemistry, the same way openalex_scraper.py covers published papers
arXiv itself never touches.

This is the newest scraper in the project and hasn't been exercised
against live Europe PMC traffic yet (this sandbox has no outbound
network access to verify field names against a real response) — the
JSON field names below (title, authorString, abstractText,
firstPublicationDate, doi, source, id) are Europe PMC's documented
schema, but if a fetch after deploying this shows unexpected KeyErrors
or empty results in the log, that's the first place to look."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import requests

from .. import paper_org
from ..cancel import check_cancelled
from ..db import Item
from .util import stable_id, text_matches_keywords

EUROPEPMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
MAX_RESULTS_PER_KEYWORD_HARD_CAP = 1000  # backfill safety valve — same idea as arxiv_scraper's


def _article_url(result: dict) -> str:
    # Europe PMC's own stable article page — works regardless of
    # whether we know the origin server's (bioRxiv/medRxiv/...) own URL
    # scheme, same reasoning as linking to an OpenAlex/arXiv page
    # elsewhere in this codebase rather than guessing a publisher URL.
    src = result.get("source", "PPR")
    rid = result.get("id", "")
    if src and rid:
        return f"https://europepmc.org/article/{src}/{rid}"
    doi = result.get("doi")
    return f"https://doi.org/{doi}" if doi else ""


def fetch(config, days_back: int, max_results_per_keyword: int, cancel_event=None) -> list[Item]:
    """Fetches preprints matching each configured keyword, newest first,
    stopping once results fall outside the `days_back` window — same
    pagination/cutoff shape as arxiv_scraper.fetch() (see its docstring),
    using Europe PMC's cursorMark pagination instead of a page offset."""
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).date()
    items: list[Item] = []
    seen_urls: set[str] = set()

    for keyword in config.keywords:
        cursor_mark = "*"
        total_seen = 0
        while True:
            check_cancelled(cancel_event)
            params = {
                "query": f'"{keyword}" AND SRC:PPR',
                "format": "json",
                "resultType": "core",  # includes abstractText, not just bibliographic fields
                "sort": "P_PDATE_D desc",
                "pageSize": max_results_per_keyword,
                "cursorMark": cursor_mark,
            }
            data = None
            for attempt in range(3):
                try:
                    resp = requests.get(
                        EUROPEPMC_API, params=params, timeout=20,
                        headers={"User-Agent": "besseleth/1.0 (industry-briefing tool)"},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except (requests.RequestException, ValueError) as e:
                    is_rate_limited = getattr(e, "response", None) is not None and e.response.status_code == 429
                    if attempt == 2:
                        print(f"[europepmc] request failed for '{keyword}' after 3 attempts: {e}")
                        data = None
                        break
                    backoff = 15 * (attempt + 1) if is_rate_limited else 5 * (attempt + 1)
                    print(f"[europepmc] request failed for '{keyword}': {e} — retrying in {backoff}s")
                    time.sleep(backoff)
            if data is None:
                break

            results = (data.get("resultList") or {}).get("result") or []
            if not results:
                break

            reached_cutoff = False
            for result in results:
                pub_date_str = result.get("firstPublicationDate") or ""
                try:
                    if pub_date_str and date.fromisoformat(pub_date_str) < cutoff_date:
                        # Sorted newest-first (P_PDATE_D desc) — nothing
                        # from here on, this page or the next, is back in
                        # the window either.
                        reached_cutoff = True
                        break
                except ValueError:
                    pass

                url = _article_url(result)
                if not url or url in seen_urls:
                    continue
                title = (result.get("title") or "").strip()
                if not title:
                    continue
                summary = (result.get("abstractText") or "").strip()
                total_seen += 1

                hits = text_matches_keywords(f"{title} {summary}", config.keywords)
                if summary and not hits:
                    # Same noise guard as openalex_scraper's keyword
                    # recheck (see its docstring) — Europe PMC's `query`
                    # is a relevance-ranked search too, not a strict
                    # substring filter, so a result can rank well on
                    # unrelated grounds. Only enforced when there's an
                    # abstract to actually check against.
                    continue

                author_string = (result.get("authorString") or "").strip()
                author_names = [a.strip() for a in author_string.split(",") if a.strip()]
                org, org_type = paper_org.resolve_paper_org_with_fallback(
                    title, [(name, [], False) for name in author_names], config
                )
                items.append(
                    Item(
                        id=stable_id("europepmc", url),
                        source="papers",  # merged with arXiv/OpenAlex — see pipeline.py
                        title=title,
                        url=url,
                        summary=summary,
                        published_at=f"{pub_date_str}T00:00:00+00:00" if pub_date_str else datetime.now(timezone.utc).isoformat(),
                        matched_keywords=hits or [keyword],
                        authors=author_string or None,
                        org=org,
                        org_type=org_type,
                    )
                )
                seen_urls.add(url)

            cursor_mark = data.get("nextCursorMark")
            if reached_cutoff or not cursor_mark or len(results) < max_results_per_keyword or total_seen >= MAX_RESULTS_PER_KEYWORD_HARD_CAP:
                break
            time.sleep(1)  # a polite gap between paginated requests, same idea as arXiv's own 3s policy

    print(f"[europepmc] {len(items)} preprint(s) kept across {len(config.keywords)} keyword(s).")
    return items
