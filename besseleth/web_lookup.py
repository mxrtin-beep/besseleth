"""Free, keyless web lookups used to backfill data enrichment couldn't
get from the item's own text — currently just org headquarters location.

Uses Wikidata's public API (https://www.wikidata.org/w/api.php) rather
than a general web search: no key, generous rate limits with a plain
User-Agent, and — critically — it returns structured facts (a
headquarters-location claim, P159, with real coordinates, P625) instead
of prose that would need another LLM call to interpret. That trade-off
means it only finds orgs notable enough to have a Wikidata entry (most
funded startups and any real institution do; a two-person stealth
startup won't) — a clean miss there is reported as "not found", not an
error.
"""
from __future__ import annotations

import time

import requests

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
USER_AGENT = "besseleth/1.0 (local personal industry-briefing tool; https://github.com/)"
MIN_REQUEST_INTERVAL = 0.5  # seconds — polite spacing between calls, Wikidata has no strict published limit like Nominatim's

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
