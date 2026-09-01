"""A small, hand-maintained (or LLM-assisted) dataset of devices/systems
in your industry, tracked over time — e.g. for neurotech, each BCI's
information transfer rate, implant longevity, and FDA regulatory status.

This is deliberately NOT scraped automatically: extracting a real number
like "information transfer rate: 62 bits/min" reliably out of a paper's
prose is exactly the kind of thing that goes quietly wrong if fully
automated, and industry-trend charts are the kind of thing you want to
trust. Instead:

  - `devices.yaml` (gitignored, copy from `devices.example.yaml`) is a
    flat list you maintain by hand as you read papers/news — a couple of
    minutes per device.
  - `suggest_from_item()` gives you a head start: it asks the local LLM to
    *draft* a device record from a scraped item's text (a new arXiv paper,
    a news story) so you're editing/confirming numbers instead of typing
    them from scratch. Nothing is added to devices.yaml automatically —
    drafts are printed for you to review and paste in yourself.

The schema is intentionally generic (`metrics: {key: value}` + free-text
`fda_status`) so it isn't neurotech-specific — swap the metric keys for
whatever your industry tracks (e.g. "battery_life_hours", "accuracy_pct").
"""
from __future__ import annotations

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


def suggest_from_item(item, trend_metrics: list[dict], summarizer_cfg: dict) -> str | None:
    """Asks the local LLM to draft a Device YAML block from a scraped
    item's text. Returns the raw drafted YAML (for the user to review and
    paste into devices.yaml), or None if it's unavailable/fails."""
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
