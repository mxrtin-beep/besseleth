"""Enriches papers/news/blog items with structured metadata (org, org
type, modality, therapeutic target, location) and a novelty score — this
is what powers the dashboard's filterable Papers table and Map tab. When
an item reports concrete numbers, it also drafts and auto-appends entries
to devices.yaml/companies.yaml, so those datasets build themselves from
the same pass instead of needing a separate manual step per paper.

Runs automatically after each fetch (bounded by
`enrichment.max_items_per_run` so one fetch cycle can't trigger an
unbounded number of LLM calls), best-effort:

  - No Ollama running / backend "none" → every eligible item is marked
    enriched with "unknown" fields rather than left to retry forever —
    there's nothing more to learn without an LLM.
  - Ollama running but this call fails → the item is left un-enriched
    (no `enriched_at`) so the next fetch retries it instead of silently
    giving up.

`enrich_items()` returns just a count (0 is ambiguous — disabled? nothing
pending? Ollama down?) for backward compatibility; `enrich_items_detailed()`
returns *why*, and is what the CLI and dashboard "Enrich now" button use
so a 0 always comes with an explanation instead of silently doing nothing.

Auto-extraction into devices.yaml/companies.yaml stays auditable rather
than "trust the AI": every auto-added entry is tagged `auto_extracted:
true`, keeps its `source_url` (the actual item, not a homepage) so a
wrong number is easy to spot-check, and is skipped entirely if an entry
for that name+org already exists — it only ever adds new rows, never
silently overwrites one you've corrected.

The vocab for org_type/modality/therapeutic_target is a *suggestion* in
the prompt, not a hard enum — freeform values still get stored, so an
unusual paper isn't forced into the wrong bucket; the dashboard's filter
dropdowns are simply populated from whatever values actually appear.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

from . import web_lookup
from .config import Config, env
from .db import DB
from .feeds_store import load_feeds
from .geocode import geocode
from .trends.company_store import auto_upsert_company
from .trends.store import auto_append_device
from . import summarizer as summarizer_mod

DEFAULT_SOURCES = ["arxiv", "news", "blog"]

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    match = _JSON_BLOCK_RE.search(text or "")
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def ollama_status(summarizer_cfg: dict) -> tuple[bool, str]:
    """Quick reachability + model check. Returns (ok, message)."""
    ollama_url = summarizer_cfg.get("ollama_url", "http://localhost:11434")
    model = summarizer_cfg.get("model", "llama3.1")
    try:
        resp = requests.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=5)
        resp.raise_for_status()
    except requests.RequestException as e:
        return False, (
            f"Can't reach Ollama at {ollama_url} ({e}). Install it from https://ollama.ai, "
            f"then run `ollama serve` (or it's already running as a background service on Mac/Windows)."
        )
    tags = resp.json().get("models", [])
    have = {t.get("name", "").split(":")[0] for t in tags}
    if model.split(":")[0] not in have:
        return False, (
            f"Ollama is running, but model {model!r} isn't pulled. Run: ollama pull {model}"
        )
    return True, f"Ollama reachable at {ollama_url}, model {model!r} available."


def _build_prompt(row, config: Config, context: str) -> str:
    metric_keys = ", ".join(f"{m['key']} ({m.get('unit', '')})" for m in config.trend_metrics if m.get("type", "numeric") == "numeric")
    categorical_keys = ", ".join(m["key"] for m in config.trend_metrics if m.get("type") == "categorical")

    return (
        f"Read this {row['source']} item about {config.industry_name}. Extract structured metadata as JSON with "
        "exactly these keys (use null for anything not present or unclear — never invent a number):\n"
        f'  "org": the primary company, lab, or institution the item is about — a specific NAMED organization only, '
        f'e.g. "Neuralink" or "Stanford University". Use null for anything else, INCLUDING: the general field/'
        f'industry itself (never "{config.industry_name}" or a synonym for it); a vague group description like '
        f'"Chinese scientists", "researchers", "a team at the university", or "the company"; and — this is a common '
        f'mistake — the PUBLICATION or news outlet reporting the story (e.g. if the text says "according to '
        f'TechCrunch..." or "36Kr reports that...", that outlet is NOT the org; keep looking for who the story is '
        f"actually about). If the text doesn't name the specific organization, that's null, not your best guess "
        f"at a description of one\n"
        '  "org_description": at most 5 words on what that org is/does, e.g. "BCI implant company" or '
        '"Academic neuroscience lab" — null if "org" is null\n'
        '  "org_type": one of "industry", "academic", "government", "nonprofit", or "unknown"\n'
        '  "modality": the technical approach/category, e.g. "EEG", "ECoG", "CNS implant", "PNS implant", "EMG", '
        '"fMRI", "fNIRS", or another short label if none fit; "unknown" if unclear\n'
        '  "therapeutic_target": what it addresses, e.g. "motor", "speech", "vision", "hearing", "memory", '
        '"mood/psychiatric", "epilepsy", "pain", "other", or "unknown" if not applicable/unclear\n'
        '  "novelty_score": integer 1-5 — how surprising/novel this is COMPARED TO the other recent items on the '
        "same topic listed below (1 = incremental/expected, 5 = a genuine surprise or breakthrough relative to them)\n"
        '  "novelty_rationale": one concise sentence justifying the novelty_score\n'
        '  "location": the city and country of the org\'s relevant site/HQ mentioned or clearly implied by the '
        'text, as "City, Country" (e.g. "San Francisco, USA") — null if not mentioned or you would be guessing\n'
        '  "device_metrics": an object with any of these keys the text reports concrete numbers/values for — '
        f"{metric_keys}, {categorical_keys} — omit keys with no data, use {{}} if none reported\n"
        '  "company_funding": an object {"funding_total_usd": number or null, "last_funding_round": string or '
        'null, "last_funding_date": "YYYY-MM-DD" or null} if this item reports a specific funding amount/round '
        'for "org" — use {} if not a funding story\n\n'
        f"Item title: {row['title']}\nItem text: {(row['summary'] or '')[:1500]}\n\n"
        f"Other recent items on the same topic (for novelty comparison):\n{context}\n\n"
        "Respond with ONLY the JSON object, no other text."
    )


_NON_ORG_EXACT = {
    "unknown", "n/a", "na", "none", "various", "unspecified", "not specified", "not mentioned",
    "not applicable", "researchers", "scientists", "the researchers", "the scientists", "authors",
    "the authors", "the team", "the company", "the companies", "the university", "the lab", "the labs",
    "investigators", "academics",
}
# "<Demonym/adjective> <generic role noun>" — e.g. "Chinese scientists",
# "European researchers". Deliberately doesn't include "lab(s)"/"labs" or
# "institute" etc. in the role-noun list: those are common LEGITIMATE org
# name endings (e.g. "Merge Labs"), unlike "scientists"/"researchers"/
# "team", which are never part of an actual org's name.
_GENERIC_GROUP_RE = re.compile(
    r"^(the\s+)?[A-Za-z]+\s+(scientists|researchers|engineers|team|teams|group|groups|authors|academics|"
    r"investigators|physicians|doctors|clinicians|developers|students|professors)$",
    re.IGNORECASE,
)


_NON_LOCATION_EXACT = {
    "unknown", "n/a", "na", "none", "unspecified", "not specified", "not mentioned", "not applicable",
    "remote", "global", "worldwide", "international", "online", "virtual", "earth", "various", "multiple",
    "various locations", "multiple locations", "tbd", "n/a, n/a",
}


def _looks_like_a_real_location(location_text: str) -> bool:
    """Same idea as _looks_like_a_named_org: rejects a vague/non-answer
    the LLM handed back instead of null (e.g. "Remote", "Global") before
    it ever reaches geocoding — this is what was landing orgs at
    implausible points (an ocean, a country's random centroid) instead
    of just staying unlocated."""
    normalized = location_text.strip().lower().strip(",. ")
    if not normalized or normalized in _NON_LOCATION_EXACT:
        return False
    # A real "City, Country" (or just a country/region) answer has some
    # alphabetic content; a bare punctuation/number string isn't one.
    if not any(c.isalpha() for c in normalized):
        return False
    return True


def _hostname(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return re.sub(r"^www\.", "", host).split(":")[0]


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _known_publisher_names(config: Config) -> set[str]:
    """Every configured NEWS feed's hostname (e.g. "bioengineer.org") and
    squashed base name (e.g. "techtimes", from "techtimes.com" — so it
    matches the human-written form "Tech Times" too), from both
    config.yaml and anything submitted via the dashboard's Feeds tab.
    News feeds are third-party outlets reporting ON companies, so this
    is a safe list of "definitely not the org" — an LLM extraction
    naming one means it picked up on "according to Tech Times..." and
    named who's reporting the story instead of who it's about.

    Deliberately excludes blog feeds: unlike news, a configured blog is
    routinely a company's OWN blog (e.g. neuralink.com/blog/feed), where
    the feed owner and the story's subject are legitimately the same
    org — applying this exclusion there would wrongly null out a
    correct extraction."""
    names: set[str] = set()
    for feed_url in config.source("news").get("feeds", []):
        host = _hostname(feed_url)
        if host:
            names.add(host)
            names.add(_squash(host.split(".")[0]))
    try:
        submitted = load_feeds(config.feeds_path)
        for entry in submitted.get("news", []):
            host = _hostname(entry.get("url", ""))
            if host:
                names.add(host)
                names.add(_squash(host.split(".")[0]))
    except Exception:
        pass  # feeds.yaml missing/unreadable — just skip this signal, not fatal
    return names


def _looks_like_a_named_org(org: str, config: Config) -> bool:
    """False for anything that isn't naming a specific organization: the
    industry name/a keyword verbatim, an explicit non-answer ('unknown',
    'n/a', ...), a vague group description ('Chinese scientists', 'the
    researchers'), or one of besseleth's own configured news/blog feed
    sources (e.g. "Tech Times", "36Kr", "bioengineer.org") — those are
    who reported the story, not who it's about. All prompted against
    directly too (see _build_prompt) — this is the defensive backstop
    for when the LLM ignores that instruction anyway."""
    normalized = org.strip().lower()
    if not normalized:
        return False
    non_orgs = _NON_ORG_EXACT | {config.industry_name.strip().lower()} | {k.strip().lower() for k in config.keywords}
    if normalized in non_orgs:
        return False
    if _GENERIC_GROUP_RE.match(org.strip()):
        return False
    publisher_names = _known_publisher_names(config)
    if normalized in publisher_names or _squash(org) in publisher_names:
        return False
    return True


def _enrich_one(row, db: DB, config: Config, summarizer_cfg: dict) -> bool:
    """Returns True if enrichment was saved (success or graceful
    'unknown' fallback), False if it should be retried next time."""
    context_rows = db.recent_items_for_context(
        row["source"], (row["matched_keywords"] or "").split(","), exclude_id=row["id"]
    )
    context = "\n".join(f"- {r['title']}: {(r['summary'] or '')[:200]}" for r in context_rows) or "(no similar recent items yet)"

    prompt = _build_prompt(row, config, context)
    result = summarizer_mod._ollama_generate(
        prompt,
        summarizer_cfg.get("ollama_url", "http://localhost:11434"),
        summarizer_cfg.get("model", "llama3.1"),
        timeout=60,
        num_thread=summarizer_cfg.get("num_thread"),
    )
    if result is None:
        return False  # Ollama unreachable — retry next time

    data = _extract_json(result) or {}
    novelty = data.get("novelty_score")
    try:
        novelty = int(novelty) if novelty is not None else None
        if novelty is not None and not (1 <= novelty <= 5):
            novelty = None
    except (TypeError, ValueError):
        novelty = None

    org = data.get("org") or None
    if org and not _looks_like_a_named_org(org, config):
        org = None
    org_description = (data.get("org_description") or "").strip() or None
    if org_description and org:
        words = org_description.split()
        if len(words) > 5:
            org_description = " ".join(words[:5])
    elif not org:
        org_description = None
    location_text = data.get("location") or None
    if location_text and not _looks_like_a_real_location(location_text):
        location_text = None
    lat = lon = None
    if location_text:
        coords = geocode(location_text)
        if coords:
            lat, lon = coords

    db.save_enrichment(
        row["id"],
        org=org,
        org_type=data.get("org_type") or "unknown",
        modality=data.get("modality") or "unknown",
        therapeutic_target=data.get("therapeutic_target") or "unknown",
        novelty_score=novelty,
        novelty_rationale=data.get("novelty_rationale") or None,
        location_text=location_text,
        lat=lat,
        lon=lon,
        org_description=org_description,
    )

    # Fold concrete numbers into devices.yaml/companies.yaml — additive
    # only, never overwrites an existing entry (see module docstring).
    device_metrics = data.get("device_metrics") or {}
    if org and device_metrics:
        auto_append_device(
            config.devices_path,
            name=device_metrics.get("device_type") and f"{org} {device_metrics['device_type']}" or org,
            org=org,
            org_type=data.get("org_type") or "unknown",
            fda_status=device_metrics.get("fda_status", "unknown"),
            metrics={k: v for k, v in device_metrics.items() if k not in ("fda_status",)},
            source_url=row["url"] or "",
            date_reported=(row["published_at"] or "")[:10],
        )

    funding = data.get("company_funding") or {}
    if org and funding.get("funding_total_usd"):
        auto_upsert_company(
            config.companies_path,
            name=org,
            funding_total_usd=funding.get("funding_total_usd"),
            last_funding_round=funding.get("last_funding_round") or "",
            last_funding_date=funding.get("last_funding_date") or "",
            source_url=row["url"] or "",
        )

    return True


def _search_org_location(org: str, summarizer_cfg: dict) -> tuple[str, float, float] | None:
    """Tier 2: a general web search (DuckDuckGo) plus the local LLM to
    read the results, for an org Wikidata doesn't know about — covers
    the small/early-stage companies tier 1 misses, at the cost of an
    LLM call, so this only runs after that one comes back empty.
    Requires Ollama; returns None on any failure at any step (no
    results, Ollama unreachable, the model saying it can't tell, or a
    location that fails the same validity check as the LLM's own item-
    level extraction)."""
    if summarizer_cfg.get("backend") != "ollama":
        return None
    snippets = web_lookup.duckduckgo_search(f"{org} headquarters location city")
    if not snippets:
        return None

    prompt = (
        f'Based on these web search result snippets, what city and country is "{org}"\'s headquarters or main '
        f'office in? Respond with ONLY "City, Country" (e.g. "San Francisco, USA"), or exactly "unknown" if the '
        f"snippets don't make it clear — never guess.\n\nSnippets:\n" + "\n".join(f"- {s}" for s in snippets)
    )
    result = summarizer_mod._ollama_generate(
        prompt,
        summarizer_cfg.get("ollama_url", "http://localhost:11434"),
        summarizer_cfg.get("model", "llama3.1"),
        timeout=30,
        num_thread=summarizer_cfg.get("num_thread"),
    )
    if not result:
        return None

    location_text = result.strip().strip('"')
    if not _looks_like_a_real_location(location_text):
        return None
    coords = geocode(location_text)
    if not coords:
        return None
    return (location_text, *coords)


def _backfill_org_locations(config: Config, db: DB) -> int:
    """Fills in a missing location for orgs that have none, independent
    of the LLM pass above (that one only ever knows what a given item's
    own text says, so an org whose location was never mentioned in any
    item stays unlocated forever without this): tries a free Wikidata/
    Wikipedia lookup first, then a general web search read by the local
    LLM if that comes back empty — see web_lookup.py's docstring for why
    in that order. Bounded per run (enrichment.max_org_lookups_per_run,
    shared across both tiers) and cached — found or not — so a miss
    isn't re-queried every run; a cached hit is reapplied for free if a
    newer item for the same org shows up without its own location.
    Returns how many orgs got newly filled in."""
    cfg = config.raw.get("enrichment", {}) or {}
    max_lookups = cfg.get("max_org_lookups_per_run", 8)
    recheck_days = cfg.get("location_recheck_days", 30)
    if max_lookups <= 0:
        return 0

    filled = 0
    attempted = 0
    for org in db.orgs_missing_location():
        cached = db.get_org_location_cache(org)
        if cached and cached["found"]:
            # Already know this one — reapply from cache, free (no web call,
            # doesn't count against this run's lookup budget).
            db.set_org_location(org, cached["location_text"], cached["lat"], cached["lon"])
            filled += 1
            continue
        if cached and not cached["found"]:
            checked_at = datetime.fromisoformat(cached["checked_at"])
            if datetime.now(timezone.utc) - checked_at < timedelta(days=recheck_days):
                continue  # checked recently, nothing found — don't re-probe yet

        if attempted >= max_lookups:
            continue
        attempted += 1
        result = web_lookup.lookup_org_location(org) or _search_org_location(org, config.summarizer)
        if result:
            label, lat, lon = result
            db.set_org_location(org, label, lat, lon)
            db.set_org_location_cache(org, found=True, location_text=label, lat=lat, lon=lon)
            filled += 1
        else:
            db.set_org_location_cache(org, found=False)

    return filled


def enrich_items_detailed(config: Config, db: DB) -> dict:
    """Enriches up to `enrichment.max_items_per_run` unenriched items.
    Returns {"processed": int, "message": str, "backend": str} — the
    message always explains a 0, so 'nothing happened' is never silent:
    enrichment disabled in config, nothing left to enrich (already
    caught up), no LLM configured (marked unknown instead), or Ollama
    unreachable (left pending — will retry once it's back)."""
    cfg = config.raw.get("enrichment", {}) or {}
    summarizer_cfg = config.summarizer
    backend = summarizer_cfg.get("backend", "none")

    if not cfg.get("enabled", True):
        return {"processed": 0, "message": "enrichment.enabled is false in config.yaml — nothing to do.", "backend": backend}

    # Self-healing cleanup for items enriched before this guard existed
    # (or before it covered vague-group phrasing like "Chinese scientists")
    # — sweeps every org value currently stored against the same check a
    # fresh enrichment applies. Cheap (one query for the distinct list,
    # then an indexed exact-match update), safe to run every call.
    invalid_orgs = [o for o in db.distinct_orgs() if not _looks_like_a_named_org(o, config)]
    cleared = db.clear_org_matches(invalid_orgs)
    if cleared:
        print(f"[enrich] Cleared {cleared} item(s) whose 'org' was actually the industry name/a keyword.")

    invalid_locations = [loc for loc in db.distinct_locations() if not _looks_like_a_real_location(loc)]
    locations_cleared = db.clear_location_matches(invalid_locations)
    if locations_cleared:
        print(f"[enrich] Cleared {locations_cleared} item(s) with a vague location guess (e.g. 'Remote'/'Global').")

    # Independent of the LLM pass below — a free web lookup (Wikidata)
    # for orgs whose location was never mentioned in any item's own
    # text, so those don't just stay unlocated forever.
    locations_filled = _backfill_org_locations(config, db)
    location_note = f" Filled in a location for {locations_filled} org(s) via web lookup." if locations_filled else ""

    sources = cfg.get("sources", DEFAULT_SOURCES)
    max_items = cfg.get("max_items_per_run", 20)

    rows = db.unenriched_items(sources, max_items)
    if not rows:
        return {
            "processed": 0,
            "message": f"Nothing to enrich — every item in enrichment.sources is already tagged (or unknown).{location_note}",
            "backend": backend,
        }

    if backend != "ollama":
        for row in rows:
            db.save_enrichment(
                row["id"], org=None, org_type="unknown", modality="unknown",
                therapeutic_target="unknown", novelty_score=None, novelty_rationale=None,
            )
        msg = (
            f"summarizer.backend is {backend!r}, not 'ollama' — marked {len(rows)} item(s) 'unknown' rather than "
            f"leaving them pending. Set summarizer.backend: \"ollama\" in config.yaml and have Ollama running to "
            f"actually extract org/modality/location/etc.{location_note}"
        )
        print(f"[enrich] {msg}")
        return {"processed": len(rows), "message": msg, "backend": backend}

    ok, status_msg = ollama_status(summarizer_cfg)
    if not ok:
        status_msg += location_note
        print(f"[enrich] {status_msg}")
        return {"processed": 0, "message": status_msg, "backend": backend}

    pause_seconds = cfg.get("pause_seconds", 0)

    processed = 0
    for i, row in enumerate(rows):
        try:
            if _enrich_one(row, db, config, summarizer_cfg):
                processed += 1
        except Exception as e:
            print(f"[enrich] Failed on item {row['id']}: {e}")
        # Gives the CPU a breather between LLM calls instead of hammering
        # it back-to-back for the whole batch — set enrichment.pause_seconds
        # in config.yaml if enrich runs are making the machine unusable.
        # Skipped after the last item so it doesn't delay returning.
        if pause_seconds and i < len(rows) - 1:
            time.sleep(pause_seconds)

    if processed < len(rows):
        message = f"Enriched {processed}/{len(rows)} — the rest failed mid-call and will retry next run (see server log)."
    else:
        message = f"Enriched {processed} item(s)."
    message += location_note
    print(f"[enrich] {message}")
    return {"processed": processed, "message": message, "backend": backend}


def enrich_items(config: Config, db: DB) -> int:
    """Same as enrich_items_detailed(), returning just the count — kept
    for existing callers (the post-fetch pipeline step)."""
    return enrich_items_detailed(config, db)["processed"]
