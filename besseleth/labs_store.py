"""A gazetteer of known PI-led labs you maintain by hand — real ground
truth (you supplied it), not an LLM guess. Used by enrich.py to
deterministically set an item's `org` when its text mentions a listed
PI's surname alongside their university, bypassing the LLM's own org
extraction for that item entirely: no ambiguity about how the lab's name
gets phrased ("the X lab" vs "X's lab" vs "X Lab at Y"), because the
canonical form comes straight from this file, not from whatever
sentence structure the source article happened to use.

Same satellite-file pattern as contacts.yaml/interests.yaml: a plain
list of {pi, university, focus, website} dicts, gitignored (yours to
own) with a matching labs.example.yaml template. Not exhaustive by
design — a lab not listed here just falls through to the LLM's own
extraction (and normalization/canonicalization) exactly as before; add
more labs whenever you notice one worth tracking accurately.
"""
from __future__ import annotations

from pathlib import Path

import yaml


def load_labs(path: str | Path = "labs.yaml") -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with open(p) as f:
        raw = yaml.safe_load(f) or []
    labs = []
    for entry in raw:
        if not isinstance(entry, dict) or not (entry.get("pi") or "").strip():
            continue
        labs.append({
            "pi": entry["pi"].strip(),
            "university": (entry.get("university") or "").strip(),
            "focus": (entry.get("focus") or "").strip(),
            "website": (entry.get("website") or "").strip(),
        })
    return labs
