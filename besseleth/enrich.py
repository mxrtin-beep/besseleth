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

import requests

from .config import Config, env
from .db import DB
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
        '  "org": the primary company, lab, or institution the item is about, or null\n'
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
    location_text = data.get("location") or None
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

    sources = cfg.get("sources", DEFAULT_SOURCES)
    max_items = cfg.get("max_items_per_run", 20)

    rows = db.unenriched_items(sources, max_items)
    if not rows:
        return {
            "processed": 0,
            "message": "Nothing to enrich — every item in enrichment.sources is already tagged (or unknown).",
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
            f"actually extract org/modality/location/etc."
        )
        print(f"[enrich] {msg}")
        return {"processed": len(rows), "message": msg, "backend": backend}

    ok, status_msg = ollama_status(summarizer_cfg)
    if not ok:
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
    print(f"[enrich] {message}")
    return {"processed": processed, "message": message, "backend": backend}


def enrich_items(config: Config, db: DB) -> int:
    """Same as enrich_items_detailed(), returning just the count — kept
    for existing callers (the post-fetch pipeline step)."""
    return enrich_items_detailed(config, db)["processed"]
