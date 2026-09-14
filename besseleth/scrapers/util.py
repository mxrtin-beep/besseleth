"""Shared helpers for scrapers."""
from __future__ import annotations

import hashlib
import re


def stable_id(source: str, key: str) -> str:
    h = hashlib.sha256(f"{source}:{key}".encode("utf-8")).hexdigest()[:24]
    return f"{source}_{h}"


def text_matches_keywords(text: str, keywords: list[str]) -> list[str]:
    """Case-insensitive substring match, tolerant of the keyword's last
    word being singular where the text has it plural or vice versa (a
    configured "neural implant" should still hit "...several neural
    implants were tested...", not require the exact singular/plural
    form guessed at config time) — tried first as an exact substring
    (the common, cheap case), falling back to stripping a trailing
    "s"/"es" from BOTH the keyword's last word and the matched
    position's text if that alone doesn't hit. This does NOT paper over
    a genuinely different word choice (e.g. "brain-machine interface"
    vs a configured "brain-computer interface" is a different phrase
    entirely, not a plural of one — no substring heuristic can find
    that; it has to be its own configured keyword). Returns the
    keywords that hit."""
    if not text:
        return []
    lowered = text.lower()
    hits = []
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in lowered:
            hits.append(kw)
            continue
        for variant in (re.sub(r"s$", "", kw_lower), kw_lower + "s", re.sub(r"s$", "es", kw_lower)):
            if variant != kw_lower and variant in lowered:
                hits.append(kw)
                break
    return hits


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()
