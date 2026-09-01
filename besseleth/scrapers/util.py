"""Shared helpers for scrapers."""
from __future__ import annotations

import hashlib
import re


def stable_id(source: str, key: str) -> str:
    h = hashlib.sha256(f"{source}:{key}".encode("utf-8")).hexdigest()[:24]
    return f"{source}_{h}"


def text_matches_keywords(text: str, keywords: list[str]) -> list[str]:
    """Case-insensitive substring match. Returns the keywords that hit."""
    if not text:
        return []
    lowered = text.lower()
    hits = []
    for kw in keywords:
        if kw.lower() in lowered:
            hits.append(kw)
    return hits


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()
