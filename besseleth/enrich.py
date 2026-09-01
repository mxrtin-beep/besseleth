"""Enriches papers/news/blog items with structured metadata (org, org
type, modality, therapeutic target) and a novelty score, via the local
LLM — this is what powers the dashboard's filterable Papers table.

Runs automatically after each fetch (bounded by
`enrichment.max_items_per_run` so one fetch cycle can't trigger an
unbounded number of LLM calls), best-effort:

  - No Ollama running / backend "none" → every eligible item is marked
    enriched with "unknown" fields rather than left to retry forever —
    there's nothing more to learn without an LLM.
  - Ollama running but this call fails → the item is left un-enriched
    (no `enriched_at`) so the next fetch retries it instead of silently
    giving up.

The vocab for org_type/modality/therapeutic_target is a *suggestion* in
the prompt, not a hard enum — freeform values still get stored, so an
unusual paper isn't forced into the wrong bucket; the dashboard's filter
dropdowns are simply populated from whatever values actually appear.
"""
from __future__ import annotations

import json
import re

from .config import Config
from .db import DB
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


def _enrich_one(row, db: DB, config: Config, summarizer_cfg: dict) -> bool:
    """Returns True if enrichment was saved (success or graceful
    'unknown' fallback), False if it should be retried next time."""
    context_rows = db.recent_items_for_context(
        row["source"], (row["matched_keywords"] or "").split(","), exclude_id=row["id"]
    )
    context = "\n".join(f"- {r['title']}: {(r['summary'] or '')[:200]}" for r in context_rows) or "(no similar recent items yet)"

    prompt = (
        f"Read this {row['source']} item about {config.industry_name}. Extract structured metadata as JSON with "
        "exactly these keys:\n"
        '  "org": the primary company, lab, or institution the item is about (best guess from the text, or null)\n'
        '  "org_type": one of "industry", "academic", "government", "nonprofit", or "unknown"\n'
        '  "modality": the technical approach/category, e.g. "EEG", "ECoG", "CNS implant", "PNS implant", "EMG", '
        '"fMRI", "fNIRS", or another short label if none fit; "unknown" if unclear\n'
        '  "therapeutic_target": what it addresses, e.g. "motor", "speech", "vision", "hearing", "memory", '
        '"mood/psychiatric", "epilepsy", "pain", "other", or "unknown" if not applicable/unclear\n'
        '  "novelty_score": integer 1-5 — how surprising/novel this is COMPARED TO the other recent items on the '
        "same topic listed below (1 = incremental/expected, 5 = a genuine surprise or breakthrough relative to them)\n"
        '  "novelty_rationale": one concise sentence justifying the novelty_score\n\n'
        f"Item title: {row['title']}\nItem text: {(row['summary'] or '')[:1500]}\n\n"
        f"Other recent items on the same topic (for novelty comparison):\n{context}\n\n"
        "Respond with ONLY the JSON object, no other text."
    )

    result = summarizer_mod._ollama_generate(
        prompt, summarizer_cfg.get("ollama_url", "http://localhost:11434"), summarizer_cfg.get("model", "llama3.1"), timeout=60
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

    db.save_enrichment(
        row["id"],
        org=data.get("org") or None,
        org_type=data.get("org_type") or "unknown",
        modality=data.get("modality") or "unknown",
        therapeutic_target=data.get("therapeutic_target") or "unknown",
        novelty_score=novelty,
        novelty_rationale=data.get("novelty_rationale") or None,
    )
    return True


def enrich_items(config: Config, db: DB) -> int:
    """Enriches up to `enrichment.max_items_per_run` unenriched items.
    Returns the count actually processed (saved, success or fallback)."""
    cfg = config.raw.get("enrichment", {}) or {}
    if not cfg.get("enabled", True):
        return 0

    sources = cfg.get("sources", DEFAULT_SOURCES)
    max_items = cfg.get("max_items_per_run", 20)
    summarizer_cfg = config.summarizer

    rows = db.unenriched_items(sources, max_items)
    if not rows:
        return 0

    if summarizer_cfg.get("backend") != "ollama":
        # No LLM available — mark everything "unknown" so these items
        # aren't retried forever; there's nothing more to learn without one.
        for row in rows:
            db.save_enrichment(
                row["id"], org=None, org_type="unknown", modality="unknown",
                therapeutic_target="unknown", novelty_score=None, novelty_rationale=None,
            )
        print(f"[enrich] No LLM configured; marked {len(rows)} item(s) unknown rather than leaving them unprocessed.")
        return len(rows)

    processed = 0
    for row in rows:
        try:
            if _enrich_one(row, db, config, summarizer_cfg):
                processed += 1
        except Exception as e:
            print(f"[enrich] Failed on item {row['id']}: {e}")

    print(f"[enrich] Enriched {processed}/{len(rows)} item(s).")
    return processed
