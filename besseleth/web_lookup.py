"""Free, keyless web lookups used to backfill data enrichment couldn't
get from the item's own text — currently just org headquarters location.

Three tiers (enrich.py tries them in this order):

  1. Wikidata's public API (https://www.wikidata.org/w/api.php) — no
     key, generous rate limits with a plain User-Agent, and returns
     structured facts (a headquarters-location claim, P159, or a plain
     location, P276, with real coordinates, P625, on whichever of those
     resolves) instead of prose that would need an LLM call to
     interpret.
  2. Wikipedia's own coordinates, when Wikidata's property graph didn't
     have what tier 1 needed — a university/lab/institute almost always
     has a Wikipedia infobox with a geocoded {{coord}}, even when its
     Wikidata entry's HQ/location property chain doesn't resolve
     cleanly (e.g. a lab that's a department of a university rather
     than its own located entity). Also free, structured, no key.
  3. A general web search (DuckDuckGo's no-JS HTML results page — no
     key either, but not an official API, and the one part of this
     module that could break if DuckDuckGo changes its markup, same
     trade-off the extension's LinkedIn selectors carry) feeding a
     handful of result snippets to the local LLM (already used
     elsewhere in enrich.py) to read them and extract a location. This
     is what covers an org neither of the first two know about — small/
     early-stage companies mostly — at the cost of an LLM call, so it's
     the last tier, not the first.

Tiers 1 and 2 both live in this module (lookup_org_location() tries
both); tier 3 is enrich.py's own function, since it needs the LLM.
"""
from __future__ import annotations

import html
import re
import time
from urllib.parse import quote

import requests

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_SEARCH_API = "https://en.wikipedia.org/w/api.php"
WIKIPEDIA_SUMMARY_API = "https://en.wikipedia.org/api/rest_v1/page/summary/"
DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = "besseleth/1.0 (local personal industry-briefing tool; https://github.com/)"
MIN_REQUEST_INTERVAL = 0.5  # seconds — polite spacing between calls (none of these publish a strict limit like Nominatim's)

_last_request_at = 0.0


def _throttle():
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)


def _get(url: str, params: dict) -> dict | None:
    global _last_request_at
    _throttle()
    try:
        resp = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=10)
        _last_request_at = time.monotonic()
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[web_lookup] request to {url} failed: {e}")
        return None


def _wikidata_get(params: dict) -> dict | None:
    return _get(WIKIDATA_API, {**params, "format": "json"})


def _entity_coords(qid: str) -> tuple[float, float] | None:
    data = _wikidata_get({"action": "wbgetclaims", "entity": qid, "property": "P625"})
    try:
        value = data["claims"]["P625"][0]["mainsnak"]["datavalue"]["value"]
        return float(value["latitude"]), float(value["longitude"])
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def _entity_label(qid: str) -> str | None:
    data = _wikidata_get({"action": "wbgetentities", "ids": qid, "props": "labels", "languages": "en"})
    try:
        return data["entities"][qid]["labels"]["en"]["value"]
    except (TypeError, KeyError):
        return None


def _claim_entity_id(qid: str, prop: str) -> str | None:
    data = _wikidata_get({"action": "wbgetclaims", "entity": qid, "property": prop})
    try:
        return data["claims"][prop][0]["mainsnak"]["datavalue"]["value"]["id"]
    except (TypeError, KeyError, IndexError):
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
    _throttle()
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


def _wikidata_location(org_name: str) -> tuple[str, float, float] | None:
    """Finds `org_name`'s Wikidata entity, then tries — in order — its
    headquarters location (P159), a plain location claim (P276), and
    finally coordinates on the entity itself (P625, for an org that IS
    a place, e.g. many universities). Returns (label, lat, lon), or None
    if nothing resolved — a clean miss, not an error."""
    search = _wikidata_get({"action": "wbsearchentities", "search": org_name, "language": "en", "type": "item", "limit": 1})
    try:
        qid = search["search"][0]["id"]
    except (TypeError, KeyError, IndexError):
        return None

    for location_prop in ("P159", "P276"):
        location_qid = _claim_entity_id(qid, location_prop)
        if location_qid:
            coords = _entity_coords(location_qid)
            if coords:
                label = _entity_label(location_qid) or org_name
                return (label, *coords)

    # No usable location claim — try the org's own entity directly.
    coords = _entity_coords(qid)
    if coords:
        label = _entity_label(qid) or org_name
        return (label, *coords)

    return None


def _wikipedia_location(org_name: str) -> tuple[str, float, float] | None:
    """Finds `org_name`'s Wikipedia article and reads its coordinates,
    when it has any — universities, labs, and research institutes
    almost always do (a geocoded {{coord}} in the infobox), even when
    the Wikidata property chain _wikidata_location() tries doesn't
    resolve cleanly (e.g. a lab that's a department of a university
    rather than its own located entity). Returns (label, lat, lon), or
    None on a clean miss."""
    search = _get(WIKIPEDIA_SEARCH_API, {"action": "query", "list": "search", "srsearch": org_name, "srlimit": 1, "format": "json"})
    try:
        title = search["query"]["search"][0]["title"]
    except (TypeError, KeyError, IndexError):
        return None

    global _last_request_at
    _throttle()
    try:
        resp = requests.get(WIKIPEDIA_SUMMARY_API + quote(title), headers={"User-Agent": USER_AGENT}, timeout=10)
        _last_request_at = time.monotonic()
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[web_lookup] Wikipedia summary request failed: {e}")
        return None

    try:
        coords = data["coordinates"]
        return (data.get("title") or org_name, float(coords["lat"]), float(coords["lon"]))
    except (TypeError, KeyError, ValueError):
        return None


def lookup_org_location(org_name: str) -> tuple[str, float, float] | None:
    """Tries Wikidata first, then Wikipedia's own coordinates — see the
    module docstring for why in that order. Returns (label, lat, lon),
    or None if neither has anything; the caller decides how to cache
    that (and whether to fall back further, e.g. to a web search)."""
    return _wikidata_location(org_name) or _wikipedia_location(org_name)
