"""Geocodes a "City, Country" string to (lat, lon), and reverse-geocodes
(lat, lon) back to a standardized "City[, State], Country" label, using
OpenStreetMap's free Nominatim API — no key required, but its usage
policy (https://operations.osmfoundation.org/policies/nominatim/)
requires: identifying User-Agent, max ~1 request/second, and caching
results instead of re-geocoding the same place repeatedly. This module
does all three: a persistent on-disk cache (`.geocode_cache.json`, plus
`.reverse_geocode_cache.json` for the reverse direction — both
gitignored) so a location is only ever looked up once, and a minimum
delay between live requests.

Map data is © OpenStreetMap contributors — the dashboard's Map tab
credits this, and you should too if you build on it further.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "besseleth/1.0 (local personal industry-briefing tool; https://github.com/)"
MIN_REQUEST_INTERVAL = 1.1  # seconds — stay under Nominatim's ~1 req/s policy

# Countries commonly written as a familiar abbreviation rather than their
# full formal name — Nominatim's `address.country` always gives the
# latter ("United States", "United Kingdom"), but "USA"/"UK" is the
# actual standard form almost everyone (including this feature's own
# original ask) writes them as. Deliberately short — anywhere not listed
# just keeps Nominatim's own full country name, which is fine/normal for
# most countries ("Germany", "Japan", ...).
_COUNTRY_ABBREVIATIONS = {
    "united states": "USA",
    "united states of america": "USA",
    "united kingdom": "UK",
}

_last_request_at = 0.0


def _load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(path: Path, cache: dict):
    try:
        path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError as e:
        print(f"[geocode] failed to save cache: {e}")


def geocode(location_text: str, cache_path: str | Path = ".geocode_cache.json") -> tuple[float, float] | None:
    """Returns (lat, lon) for a free-text location, or None if it
    couldn't be resolved. Cached indefinitely — locations don't move."""
    global _last_request_at
    if not location_text or not location_text.strip():
        return None

    key = location_text.strip().lower()
    path = Path(cache_path)
    cache = _load_cache(path)
    if key in cache:
        return tuple(cache[key]) if cache[key] else None

    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)

    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": location_text, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        _last_request_at = time.monotonic()
        resp.raise_for_status()
        results = resp.json()
    except requests.RequestException as e:
        print(f"[geocode] lookup failed for {location_text!r}: {e}")
        return None

    if not results:
        cache[key] = None
        _save_cache(path, cache)
        return None

    coords = (float(results[0]["lat"]), float(results[0]["lon"]))
    # (0, 0) is "Null Island" — open ocean in the Gulf of Guinea, not a
    # real place. Nominatim shouldn't return it for a genuine query, but
    # a vague/malformed location_text (e.g. the LLM guessing "Remote" or
    # "Global") can occasionally resolve to something degenerate like
    # this — treat it as a failed lookup rather than plotting an org in
    # the ocean.
    if abs(coords[0]) < 0.01 and abs(coords[1]) < 0.01:
        cache[key] = None
        _save_cache(path, cache)
        return None
    cache[key] = list(coords)
    _save_cache(path, cache)
    return coords


def _format_address(address: dict) -> str | None:
    """Nominatim's `address` breakdown (from reverse geocoding, or a
    forward search with addressdetails=1) into a standardized "City[,
    State], Country" label — the state segment only when Nominatim
    itself considers the place to have one at this administrative level
    (most non-federal countries don't get one, e.g. UK/most of Europe —
    that's what naturally produces "London, UK" for one and "Cambridge,
    Massachusetts, USA" for the other, without hardcoding which
    countries get a state segment). None if there's no city-level name
    to anchor on at all (open ocean, an unnamed area)."""
    city = (
        address.get("city") or address.get("town") or address.get("village")
        or address.get("municipality") or address.get("county")
    )
    if not city:
        return None
    country = address.get("country") or ""
    country = _COUNTRY_ABBREVIATIONS.get(country.strip().lower(), country)
    state = address.get("state") or ""
    parts = [p for p in (city, state, country) if p]
    return ", ".join(parts) if parts else None


def reverse_geocode(lat: float, lon: float, cache_path: str | Path = ".reverse_geocode_cache.json") -> str | None:
    """The inverse of geocode() — (lat, lon) back to a standardized
    "City[, State], Country" label (see _format_address). Used to give
    every location_text a consistent format regardless of which tier
    produced the original guess (the item-extraction LLM, a web-search-
    plus-LLM lookup, or a Wikidata/Wikipedia entity label — none of
    which are reliably formatted the same way two different orgs, let
    alone two different code paths, happen to phrase a location).
    Cached (rounded to ~11m — same real-world spot resolves from cache
    instead of a fresh request) and rate-limited the same as geocode().
    Returns None on any failure — callers should fall back to whatever
    label they already had rather than losing the location entirely."""
    global _last_request_at
    key = f"{round(lat, 4)},{round(lon, 4)}"
    path = Path(cache_path)
    cache = _load_cache(path)
    if key in cache:
        return cache[key]

    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)

    try:
        resp = requests.get(
            NOMINATIM_REVERSE_URL,
            params={"lat": lat, "lon": lon, "format": "json", "addressdetails": 1, "zoom": 14},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        _last_request_at = time.monotonic()
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[geocode] reverse lookup failed for ({lat}, {lon}): {e}")
        return None

    label = _format_address(data.get("address") or {})
    cache[key] = label
    _save_cache(path, cache)
    return label
