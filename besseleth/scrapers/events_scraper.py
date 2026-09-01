"""IRL events near you — meetups, demo nights, local conferences — sourced
from Luma and Eventbrite where practical, plus a curated watchlist and a
paste/upload fallback for anything neither platform exposes.

**Reality check on "search events near me" APIs**, so this module doesn't
promise more than it delivers:

  - **Eventbrite** killed its public general-search endpoint
    (`/v3/events/search/`) for new apps in Dec 2019 — a plain API key can
    no longer do "find events near this city". What *does* still work with
    a free personal OAuth token is listing events for an **organizer you
    follow/manage** or a **specific venue**, so `fetch_eventbrite()` below
    supports that narrower, still-useful case: put organizer IDs you care
    about in config and it'll pull their upcoming events.
  - **Luma (lu.ma)** has no public search API at all. It does publish a
    plain iCal feed per calendar (`https://lu.ma/calendar/<id>/ical`) for
    calendars you follow, which `fetch_luma_calendars()` reads for free.

For true "anything happening near me this week" discovery, the practical
free path is: browse Luma/Eventbrite/Meetup yourself (they're good at
geo-search on their own sites) and paste the ones worth tracking into
`dropbox_dir` (default `event_drops/`) — see `manual_drop.py`'s docstring
for the exact format. Pasted events get keyword-matched and folded into
the report exactly like everything else.
"""
from __future__ import annotations

from datetime import datetime, timezone

import requests

from ..config import env
from ..db import Item
from .manual_drop import add_manual_item as _add_manual_item
from .manual_drop import fetch_drops
from .util import stable_id, strip_html, text_matches_keywords

try:
    from icalendar import Calendar  # optional dependency
except ImportError:  # pragma: no cover
    Calendar = None


def fetch_watchlist(config, source_cfg: dict) -> list[Item]:
    """Curated recurring local meetups/series you already know about —
    same idea as the conference watchlist, but for smaller/local events."""
    items = []
    for entry in source_cfg.get("watchlist", []):
        name = entry.get("name", "")
        url = entry.get("url", "")
        location = entry.get("location", "")
        cadence = entry.get("cadence", "")
        blurb = " · ".join(x for x in [location, cadence] if x)
        hits = text_matches_keywords(name, config.keywords)
        items.append(
            Item(
                id=stable_id("event", url or name),
                source="event",
                title=name,
                url=url,
                summary=blurb or name,
                published_at=datetime.now(timezone.utc).isoformat(),
                matched_keywords=hits or [config.industry_name],
            )
        )
    return items


def fetch_luma_calendars(config, source_cfg: dict) -> list[Item]:
    """Reads iCal feeds for Luma calendars you follow. Find a calendar's
    feed URL via its page -> Subscribe -> "Add to calendar" -> copy the
    .ics link (looks like https://lu.ma/calendar/<id>/ical)."""
    calendar_urls = source_cfg.get("luma_calendar_ical_urls", [])
    if not calendar_urls:
        return []
    if Calendar is None:
        print("[events] `icalendar` package not installed; skipping Luma calendars. `pip install icalendar`.")
        return []

    items = []
    for cal_url in calendar_urls:
        try:
            resp = requests.get(cal_url, timeout=20)
            resp.raise_for_status()
            cal = Calendar.from_ical(resp.text)
        except Exception as e:
            print(f"[events] failed to read Luma calendar {cal_url}: {e}")
            continue

        for component in cal.walk("VEVENT"):
            title = str(component.get("summary", "")).strip()
            desc = strip_html(str(component.get("description", "")))
            url = str(component.get("url", "")) or cal_url
            dtstart = component.get("dtstart")
            published = dtstart.dt.isoformat() if dtstart else datetime.now(timezone.utc).isoformat()
            hits = text_matches_keywords(f"{title} {desc}", config.keywords)
            if not hits:
                continue
            items.append(
                Item(
                    id=stable_id("event", url or title),
                    source="event",
                    title=title,
                    url=url,
                    summary=desc,
                    published_at=published,
                    matched_keywords=hits,
                )
            )
    return items


def fetch_eventbrite(config, source_cfg: dict) -> list[Item]:
    """Lists upcoming events for specific organizer IDs (not a geo search —
    see module docstring for why). Set EVENTBRITE_TOKEN and list organizer
    IDs under sources.events.eventbrite.organizer_ids in config."""
    organizer_ids = source_cfg.get("organizer_ids", [])
    if not organizer_ids:
        return []
    token = env("EVENTBRITE_TOKEN")
    if not token:
        print("[events] Eventbrite organizer_ids configured but EVENTBRITE_TOKEN is not set; skipping.")
        return []

    items = []
    for org_id in organizer_ids:
        try:
            resp = requests.get(
                f"https://www.eventbriteapi.com/v3/organizers/{org_id}/events/",
                params={"status": "live", "order_by": "start_asc"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"[events] Eventbrite request failed for organizer {org_id}: {e}")
            continue

        for ev in data.get("events", []):
            title = (ev.get("name") or {}).get("text", "")
            desc = strip_html((ev.get("description") or {}).get("text", "") or "")
            url = ev.get("url", "")
            hits = text_matches_keywords(f"{title} {desc}", config.keywords)
            if not hits:
                continue
            items.append(
                Item(
                    id=stable_id("event", url or title),
                    source="event",
                    title=title,
                    url=url,
                    summary=desc,
                    published_at=(ev.get("start") or {}).get("utc", datetime.now(timezone.utc).isoformat()),
                    matched_keywords=hits,
                )
            )
    return items


def add_manual_item(config, db, text: str, url: str = "") -> Item:
    return _add_manual_item(config, db, text, source="event", url=url)


def fetch(config, source_cfg: dict) -> list[Item]:
    items = fetch_watchlist(config, source_cfg)
    items += fetch_luma_calendars(config, source_cfg.get("luma", {}) or source_cfg)
    items += fetch_eventbrite(config, source_cfg.get("eventbrite", {}) or {})

    dropbox_dir = source_cfg.get("dropbox_dir", "event_drops")
    drops = fetch_drops(config, dropbox_dir, source="event")
    items += drops
    if not drops:
        print(
            f"[events] No manual event drops found. Paste Luma/Eventbrite/Meetup pages you "
            f"find into {dropbox_dir}/, or run `python -m besseleth.cli event-add`."
        )

    # dedupe across the sub-fetchers
    seen = set()
    unique = []
    for it in items:
        if it.id not in seen:
            unique.append(it)
            seen.add(it.id)
    return unique
