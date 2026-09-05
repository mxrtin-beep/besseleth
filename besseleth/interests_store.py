"""A short list of topics you personally care about, independent of any
contact — e.g. "speech decoding", "wireless power transfer". Powers the
report's "For you" section alongside contacts (see personalize.py):
an item mentioning one of these gets flagged even if it has nothing to
do with anyone in your Contacts list.

Same satellite-file pattern as contacts.yaml/feeds.yaml: a plain list of
strings, editable in the dashboard's Contacts tab, gitignored (yours to
own) with a matching interests.example.yaml template.

Deliberately just a flat list of phrases matched the same simple way a
contact's workplace is (see personalize.py's _mentioned) — no per-item
LLM call, no resume parsing. Cheap and fully transparent: you can always
see exactly which phrase in this list caused a match.
"""
from __future__ import annotations

from pathlib import Path

import yaml


def load_interests(path: str | Path = "interests.yaml") -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    with open(p) as f:
        raw = yaml.safe_load(f) or []
    return [s.strip() for s in raw if isinstance(s, str) and s.strip()]


def save_interests(interests: list[str], path: str | Path = "interests.yaml") -> None:
    cleaned = [s.strip() for s in interests if isinstance(s, str) and s.strip()]
    with open(path, "w") as f:
        yaml.safe_dump(cleaned, f, sort_keys=False)
