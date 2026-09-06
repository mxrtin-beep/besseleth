"""An OPTIONAL, entirely inert-if-empty gazetteer of PI-led labs. Used
by enrich.py to deterministically set an item's `org` when its text
mentions a listed PI's surname alongside their university, bypassing
the LLM's own org extraction for that item.

Deliberately not meant as something to keep in sync with reality —
labs move, PIs change, and a hand-maintained list is exactly the kind
of upkeep the rest of enrichment (LLM extraction + OpenAlex author
affiliations + lab-name normalization/canonicalization) is supposed to
make unnecessary. Treat this as a spot-check tool rather than a
standing feature: hand it a few labs you know well to see whether the
generic pipeline already gets them right without an entry, and only add
one for a case it doesn't. Missing or empty has zero effect — every
lab just falls through to the generic extraction, exactly as if this
file didn't exist.

Same satellite-file pattern as contacts.yaml/interests.yaml: a plain
list of {pi, university, focus, website} dicts, gitignored (yours to
own) with a matching labs.example.yaml template.
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
