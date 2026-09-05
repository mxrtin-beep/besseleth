"""Collapses near-duplicate items across sources before they hit the
report — e.g. a story you paste from LinkedIn that's also the subject of
a scraped news article shouldn't appear twice. This runs across ALL
sources together (not just within one), since that's exactly the case
that matters: the same underlying fact reaching besseleth two different
ways (a feed and a paste) is one item, not two.

Deliberately simple and deterministic (title-similarity via difflib, no
LLM call) rather than another Ollama round-trip per report — this is a
cheap, fast, and auditable pass, not a fuzzy "AI merge".
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .db import Item

SIMILARITY_THRESHOLD = 0.78


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def _similarity(a: str, b: str) -> float:
    """Takes the better of two signals: SequenceMatcher (good for near-
    identical strings) and word-set Jaccard (robust to reordering,
    truncation, or a paste that paraphrases a headline rather than
    quoting it verbatim — e.g. "Synchron raises $75M Series D funding
    round" vs. a LinkedIn post's "Synchron raises $75M Series D")."""
    seq_ratio = SequenceMatcher(None, a, b).ratio()
    words_a, words_b = set(a.split()), set(b.split())
    jaccard = len(words_a & words_b) / len(words_a | words_b) if (words_a or words_b) else 0.0
    return max(seq_ratio, jaccard)


def group_near_duplicates(items: list[Item]) -> list[list[Item]]:
    """Groups items by the same near-duplicate title similarity as
    merge_near_duplicates(), but without picking a winner or dropping
    anything — for a caller that wants to reconcile a field ACROSS a
    group of rows it's keeping (e.g. syncing novelty_score so the same
    story rated by two different feeds' rows shows the same score),
    rather than collapsing the group into one row. Singletons come back
    as their own one-item group."""
    normalized = [(_normalize_title(i.title), i) for i in items]
    groups: list[list[Item]] = []
    consumed: set[str] = set()

    for idx, (norm_a, item_a) in enumerate(normalized):
        if item_a.id in consumed:
            continue
        group = [item_a]
        consumed.add(item_a.id)
        for norm_b, item_b in normalized[idx + 1 :]:
            if item_b.id in consumed:
                continue
            if not norm_a or not norm_b:
                continue
            if _similarity(norm_a, norm_b) >= SIMILARITY_THRESHOLD:
                group.append(item_b)
                consumed.add(item_b.id)
        groups.append(group)

    return groups


def merge_near_duplicates(items: list[Item]) -> tuple[list[Item], dict[str, list[str]]]:
    """Returns (deduped_items, {kept_item_id: [dropped_item_id, ...]}).
    The kept item is whichever has the longer summary (more detail);
    its .summary gets a short note when the dropped duplicate(s) came
    from a different source, so the merge is visible, not silent."""
    normalized = [(_normalize_title(i.title), i) for i in items]
    kept: list[Item] = []
    dropped_by_kept: dict[str, list[str]] = {}
    consumed: set[str] = set()

    for idx, (norm_a, item_a) in enumerate(normalized):
        if item_a.id in consumed:
            continue
        group = [item_a]
        for norm_b, item_b in normalized[idx + 1 :]:
            if item_b.id in consumed:
                continue
            if not norm_a or not norm_b:
                continue
            if _similarity(norm_a, norm_b) >= SIMILARITY_THRESHOLD:
                group.append(item_b)
                consumed.add(item_b.id)

        if len(group) == 1:
            kept.append(item_a)
            continue

        winner = max(group, key=lambda i: len(i.summary or ""))
        others = [i for i in group if i.id != winner.id]

        # Fold in any materially different detail from the others rather
        # than silently dropping it — a paste and a scraped article about
        # the same story often each have something the other doesn't.
        seen_summaries = {(winner.summary or "").strip()}
        extra_details = []
        for o in others:
            snippet = (o.summary or "").strip()
            if snippet and snippet not in seen_summaries and SequenceMatcher(None, winner.summary or "", snippet).ratio() < 0.9:
                extra_details.append(f"_Additional detail via {o.source}:_ {snippet[:400]}")
                seen_summaries.add(snippet)

        other_sources = sorted({o.source for o in others if o.source != winner.source})
        parts = [winner.summary or ""]
        if other_sources:
            parts.append(f"_(Also reported via: {', '.join(other_sources)}.)_")
        parts.extend(extra_details)
        winner.summary = "\n\n".join(p for p in parts if p)

        dropped_by_kept[winner.id] = [o.id for o in others]
        kept.append(winner)

    return kept, dropped_by_kept
