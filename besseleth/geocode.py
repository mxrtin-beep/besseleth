"""Geocodes a "City, Country" string to (lat, lon) using OpenStreetMap's
free Nominatim API — no key required, but its usage policy
(https://operations.osmfoundation.org/policies/nominatim/) requires:
identifying User-Agent, max ~1 request/second, and caching results
instead of re-geocoding the same place repeatedly. This module does all
three: a persistent on-disk cache (`.geocode_cache.json`, gitignored) so
a location is only ever looked up once, and a minimum delay between live
requests.

Map data is © OpenStreetMap contributors — the dashboard's Map tab
credits this, and you should too if you build on it further.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "besseleth/1.0 (local personal industry-briefing tool; https://github.com/)"
MIN_REQUEST_INTERVAL = 1.1  # seconds — stay under Nominatim's ~1 req/s policy

_last_request_at = 0.0


def _load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(path: Path, cache: dict):
    try:
        path.write_text(json.dumps(cache, indent=2))
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
