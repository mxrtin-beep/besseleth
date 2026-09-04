"""Free, keyless web lookups used to backfill data enrichment couldn't
get from the item's own text — currently just org headquarters location.

Two tiers (enrich.py tries them in this order):

  1. Wikidata's public API (https://www.wikidata.org/w/api.php) — no
     key, generous rate limits with a plain User-Agent, and returns
     structured facts (a headquarters-location claim, P159, with real
     coordinates, P625) instead of prose that would need an LLM call to
     interpret. The catch: it only covers orgs notable enough to have a
     Wikidata entry — most funded startups and any real institution do,
     but plenty of early-stage/small companies don't.
  2. A general web search (DuckDuckGo's no-JS HTML results page — no
     key either, but not an official API, and the one part of this
     module that could break if DuckDuckGo changes its markup, same
     trade-off the extension's LinkedIn selectors carry) feeding a
     handful of result snippets to the local LLM (already used
     elsewhere in enrich.py) to read them and extract a location. This
     is what actually covers the orgs tier 1 misses — it costs an LLM
     call, so it's the second tier, not the first.
"""
from __future__ import annotations

import html
import re
import time

import requests

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = "besseleth/1.0 (local personal industry-briefing tool; https://github.com/)"
MIN_REQUEST_INTERVAL = 0.5  # seconds — polite spacing between calls (Wikidata/DDG have no strict published limit like Nominatim's)

_last_request_at = 0.0


def _get(params: dict) -> dict | None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    try:
        resp = requests.get(WIKIDATA_API, params={**params, "format": "json"}, headers={"User-Agent": USER_AGENT}, timeout=10)
        _last_request_at = time.monotonic()
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[web_lookup] Wikidata request failed: {e}")
        return None


def _entity_coords(qid: str) -> tuple[float, float] | None:
    data = _get({"action": "wbgetclaims", "entity": qid, "property": "P625"})
    try:
        value = data["claims"]["P625"][0]["mainsnak"]["datavalue"]["value"]
        return float(value["latitude"]), float(value["longitude"])
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def _entity_label(qid: str) -> str | None:
    data = _get({"action": "wbgetentities", "ids": qid, "props": "labels", "languages": "en"})
    try:
        return data["entities"][qid]["labels"]["en"]["value"]
    except (TypeError, KeyError):
        return None


def duckduckgo_search(query: str, max_results: int = 4) -> list[str]:
    """Free, keyless general web search via DuckDuckGo's no-JS HTML
    results page (meant for browsers without JavaScript, not an
    official API — best-effort). Returns up to `max_results` short text
    snippets pulled from the result blurbs, or [] on any failure
    (network error, or DuckDuckGo's markup no longer matching what this
    parses — it's a regex over their result__snippet links, not a real
    HTML parser, to avoid pulling in a new dependency for one scraper)."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    try:
        resp = requests.post(DUCKDUCKGO_HTML_URL, data={"q": query}, headers={"User-Agent": USER_AGENT}, timeout=10)
        _last_request_at = time.monotonic()
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[web_lookup] DuckDuckGo search failed: {e}")
        return []

    snippets = []
    for m in re.finditer(r'class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.DOTALL):
        text = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        if text:
            snippets.append(text)
        if len(snippets) >= max_results:
            break
    return snippets


def lookup_org_location(org_name: str) -> tuple[str, float, float] | None:
    """Best-effort: finds `org_name`'s Wikidata entity, then its
    headquarters location (P159) if it has one, else falls back to
    coordinates on the entity itself (P625, for an org that IS a place,
    e.g. a university). Returns (label, lat, lon), or None if the org
    isn't on Wikidata or has no location data there — a clean miss, not
    an error; the caller decides how to cache that."""
    search = _get({"action": "wbsearchentities", "search": org_name, "language": "en", "type": "item", "limit": 1})
    try:
        qid = search["search"][0]["id"]
    except (TypeError, KeyError, IndexError):
        return None

    hq_claims = _get({"action": "wbgetclaims", "entity": qid, "property": "P159"})
    hq_qid = None
    try:
        hq_qid = hq_claims["claims"]["P159"][0]["mainsnak"]["datavalue"]["value"]["id"]
    except (TypeError, KeyError, IndexError):
        pass

    if hq_qid:
        coords = _entity_coords(hq_qid)
        if coords:
            label = _entity_label(hq_qid) or org_name
            return (label, *coords)

    # No headquarters claim (or no coords on it) — try the org's own
    # entity directly, in case it's itself a located thing.
    coords = _entity_coords(qid)
    if coords:
        label = _entity_label(qid) or org_name
        return (label, *coords)

    return None
