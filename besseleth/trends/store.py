"""A dataset of devices/systems in your industry, tracked over time —
e.g. for neurotech, each BCI's information transfer rate, implant
longevity, and FDA regulatory status.

`devices.yaml` (gitignored, copy from `devices.example.yaml`) is the
single file, but two things write to it:

  - **Auto-extraction**, via `enrich.py`, after every fetch: when an
    arXiv/news/blog item reports concrete numbers for a metric in
    `industry.trend_metrics`, the local LLM drafts an entry and
    `auto_append_device()` appends it — tagged `auto_extracted: true` so
    it's visibly distinct from a hand-confirmed one, and skipped if an
    entry with the same name+org already exists (so it never overwrites
    something you've edited, and never re-adds a duplicate).
  - **You**, by hand — editing an auto-extracted entry to mark it
    reviewed (flip `auto_extracted` to `false`, or just fix a wrong
    number), or adding one from a source that predates besseleth.

This is genuinely automatic now, but stays auditable: every entry keeps
its `source_url` (the actual paper/article, never a homepage) so a wrong
auto-extracted number is easy to spot-check and fix, rather than
untraceable. `suggest_from_item()` remains available for a manual,
review-before-adding path if you'd rather not trust auto-extraction for
a given item.

The schema is intentionally generic (`metrics: {key: value}` + free-text
`fda_status`) so it isn't neurotech-specific — swap the metric keys for
whatever your industry tracks (e.g. "battery_life_hours", "accuracy_pct").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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


def load_devices(path: str | Path = "devices.yaml") -> list[Device]:
    p = Path(path)
    if not p.exists():
        return []
    with open(p) as f:
        raw = yaml.safe_load(f) or []
    return [Device(**d) for d in raw]


def save_devices(devices: list[Device], path: str | Path = "devices.yaml"):
    p = Path(path)
    raw = [d.__dict__ for d in devices]
    with open(p, "w") as f:
        yaml.safe_dump(raw, f, sort_keys=False)


def append_device(device: Device, path: str | Path = "devices.yaml"):
    devices = load_devices(path)
    devices.append(device)
    save_devices(devices, path)


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


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
    """Appends a new, LLM-drafted device entry — unless one with the same
    (normalized) name+org already exists, in which case this is a no-op:
    existing entries (auto-extracted or hand-edited) are never overwritten.
    Returns True if it appended."""
    if not name or not org or not metrics:
        return False
    devices = load_devices(path)
    key = (_normalize(name), _normalize(org))
    if any((_normalize(d.name), _normalize(d.org)) == key for d in devices):
        return False
    devices.append(
        Device(
            name=name,
            org=org,
            org_type=org_type or "unknown",
            fda_status=fda_status or "unknown",
            metrics={k: v for k, v in metrics.items() if v is not None},
            source_url=source_url,
            date_reported=date_reported,
            notes="Auto-extracted by besseleth from a scraped item — verify before trusting.",
            auto_extracted=True,
        )
    )
    save_devices(devices, path)
    return True


def suggest_from_item(item, trend_metrics: list[dict], summarizer_cfg: dict) -> str | None:
    """Asks the local LLM to draft a Device YAML block from a scraped
    item's text. Returns the raw drafted YAML (for the user to review and
    paste into devices.yaml), or None if it's unavailable/fails. A manual
    alternative to auto-extraction, for anyone who'd rather review every
    entry before it's added."""
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
