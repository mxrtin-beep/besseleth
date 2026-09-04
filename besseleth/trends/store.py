"""A dataset of devices/systems in your industry, tracked over time —
e.g. for neurotech, each BCI's information transfer rate, implant
longevity, and FDA regulatory status.

Lives in the main sqlite db (see db.py's `devices` table) rather than a
hand-copied devices.yaml — two reasons:

  - A new metric column (e.g. electrode_count) shows up automatically via
    the same ALTER TABLE migration the `items` table uses, instead of
    requiring devices.example.yaml to be re-copied by hand — the exact
    friction that meant new trend_metrics silently never appeared for
    anyone who'd already copied an older template.
  - The whole point of a *timeline* (e.g. comparing FDA designations
    across companies over time) needs more than one dated row per
    device. A YAML file keyed by (name, org) with "never overwrite an
    existing entry" can't hold that; a sqlite table keyed by
    (name, org, date_reported) can.

Two things still write here:

  - **Auto-extraction**, via `enrich.py`, after every fetch: when an
    arXiv/news/blog item reports concrete numbers for a metric in
    `industry.trend_metrics`, the local LLM drafts an entry and
    `auto_append_device()` adds it — tagged `auto_extracted=True` so it's
    visibly distinct from a hand-confirmed one, and skipped only if a row
    for the same (name, org, date_reported) already exists, so re-running
    enrichment never duplicates the same report, but a *later* report
    about the same device still adds a new dated row.
  - **You**, via the dashboard's Trends tab, adding/correcting an entry.

If you have an old devices.yaml (from before this moved to sqlite), it's
imported once, automatically, the first time this module is used — see
`_migrate_legacy_yaml()`.

The schema is intentionally generic (`metrics: {key: value}` + free-text
`fda_status`) so it isn't neurotech-specific — swap the metric keys for
whatever your industry tracks (e.g. "battery_life_hours", "accuracy_pct").
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Device:
    name: str
    org: str
    org_type: str  # "industry" | "academic"
    fda_status: str = "unknown"
    metrics: dict[str, float] = field(default_factory=dict)
    source_url: str = ""
    date_reported: str = ""
    notes: str = ""
    auto_extracted: bool = False


def _row_to_device(row) -> Device:
    return Device(
        name=row["name"],
        org=row["org"],
        org_type=row["org_type"] or "unknown",
        fda_status=row["fda_status"] or "unknown",
        metrics=json.loads(row["metrics_json"]) if row["metrics_json"] else {},
        source_url=row["source_url"] or "",
        date_reported=row["date_reported"] or "",
        notes=row["notes"] or "",
        auto_extracted=bool(row["auto_extracted"]),
    )


def _migrate_legacy_yaml(db, legacy_yaml_path: str | Path | None) -> None:
    """One-time import of an old devices.yaml into the devices table, if
    one exists and the table is still empty (so this never re-imports or
    clobbers anything once you're on sqlite)."""
    if not legacy_yaml_path:
        return
    p = Path(legacy_yaml_path)
    if not p.exists() or db.devices():
        return
    import yaml

    with open(p) as f:
        raw = yaml.safe_load(f) or []
    for d in raw:
        db.add_device(
            name=d.get("name", ""),
            org=d.get("org", ""),
            org_type=d.get("org_type", "unknown"),
            fda_status=d.get("fda_status", "unknown"),
            metrics_json=json.dumps(d.get("metrics") or {}),
            source_url=d.get("source_url", ""),
            date_reported=d.get("date_reported", ""),
            notes=d.get("notes", "") or f"Imported from {p.name}.",
            auto_extracted=bool(d.get("auto_extracted", False)),
        )
    print(f"[trends] Imported {len(raw)} device(s) from {p} into the database — you can delete that file now.")


def load_devices(db_path: str | Path, legacy_yaml_path: str | Path | None = None) -> list[Device]:
    from ..db import DB

    db = DB(Path(db_path))
    try:
        _migrate_legacy_yaml(db, legacy_yaml_path)
        return [_row_to_device(r) for r in db.devices()]
    finally:
        db.close()


def append_device(device: Device, db_path: str | Path) -> None:
    from ..db import DB

    db = DB(Path(db_path))
    try:
        db.add_device(
            name=device.name,
            org=device.org,
            org_type=device.org_type,
            fda_status=device.fda_status,
            metrics_json=json.dumps(device.metrics),
            source_url=device.source_url,
            date_reported=device.date_reported,
            notes=device.notes,
            auto_extracted=device.auto_extracted,
        )
    finally:
        db.close()


def auto_append_device(
    path: str | Path,
    name: str,
    org: str,
    org_type: str,
    fda_status: str,
    metrics: dict[str, Any],
    source_url: str,
    date_reported: str,
) -> bool:
    """Adds a new, LLM-drafted device row — unless a row for the same
    (name, org, date_reported) already exists, in which case this is a
    no-op (a re-run of enrichment on the same item shouldn't duplicate
    it). A *new* dated report about a device already tracked still adds
    a row, which is what lets a metric or FDA status be compared over
    time. Returns True if it added a row."""
    if not name or not org or not metrics:
        return False
    from ..db import DB

    db = DB(Path(path))
    try:
        if db.device_exists(name, org, date_reported):
            return False
        db.add_device(
            name=name,
            org=org,
            org_type=org_type or "unknown",
            fda_status=fda_status or "unknown",
            metrics_json=json.dumps({k: v for k, v in metrics.items() if v is not None}),
            source_url=source_url,
            date_reported=date_reported,
            notes="Auto-extracted by besseleth from a scraped item — verify before trusting.",
            auto_extracted=True,
        )
        return True
    finally:
        db.close()


def suggest_from_item(item, trend_metrics: list[dict], summarizer_cfg: dict) -> str | None:
    """Asks the local LLM to draft a Device block from a scraped item's
    text. Returns the raw drafted YAML (for reference/review), or None if
    it's unavailable/fails. A manual alternative to auto-extraction, for
    anyone who'd rather review every entry before it's added."""
    from .. import summarizer as summarizer_mod

    metric_keys = ", ".join(f"{m['key']} ({m.get('unit', '')})" for m in trend_metrics)
    prompt = (
        "Read this item about a device/system. If it reports concrete numbers for "
        f"any of these metrics — {metric_keys} — or a device's FDA regulatory status, "
        "draft a YAML block matching this schema (omit fields with no data found; "
        "do not invent numbers):\n\n"
        "name: <device/system name>\n"
        "org: <company or lab>\n"
        "org_type: industry|academic\n"
        "fda_status: <e.g. 'Breakthrough Device Designation', 'IDE approved', 'PMA approved', 'unknown'>\n"
        "metrics:\n  <metric_key>: <number>\n"
        f"source_url: {item.url}\n"
        f"date_reported: {item.published_at[:10]}\n\n"
        f"Title: {item.title}\nText: {item.summary[:1500]}\n\n"
        "If no relevant numbers or FDA status are mentioned, respond with exactly: NONE\n\nYAML:"
    )
    result = summarizer_mod._ollama_generate(
        prompt, summarizer_cfg.get("ollama_url", "http://localhost:11434"), summarizer_cfg.get("model", "llama3.1")
    )
    if not result or result.strip().upper() == "NONE":
        return None
    return result.strip()
