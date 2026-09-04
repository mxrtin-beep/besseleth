"""Summarizes the week's items with a free local LLM via Ollama
(https://ollama.ai — `ollama pull llama3.1 && ollama serve`).

Falls back to a plain extractive summary (first sentences) if Ollama is
unreachable or backend is set to "none", so the report always generates.
"""
from __future__ import annotations

import json
import re

import requests

from .db import Item


def _extractive_fallback(items: list[Item], max_sentences: int = 3) -> str:
    parts = [f"[{i.title}]({i.url})." if i.url else f"{i.title}." for i in items[:max_sentences]]
    return " ".join(parts) or "No notable developments this week."


def _ollama_generate(prompt: str, ollama_url: str, model: str, timeout: int = 120, num_thread: int | None = None) -> str | None:
    payload = {"model": model, "prompt": prompt, "stream": False}
    if num_thread:
        # Caps how many CPU threads Ollama uses for this call — set
        # summarizer.num_thread in config.yaml (e.g. to half your core
        # count) if enrichment runs are making the machine unusable.
        # Unset by default so behavior is unchanged unless you opt in.
        payload["options"] = {"num_thread": num_thread}
    try:
        resp = requests.post(
            f"{ollama_url.rstrip('/')}/api/generate",
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except requests.RequestException as e:
        print(f"[summarizer] Ollama unreachable ({e}); using extractive fallback.")
        return None


def summarize_section(items: list[Item], section_name: str, industry_name: str, cfg: dict, cite_style: str = "narrative") -> str:
    """Returns a short prose summary of the given items, each specific
    mention cited as an inline markdown link to its source.

    cite_style:
      - "narrative" (default — News, Blogs): a flowing paragraph that
        can blend related items into one sentence when they're part of
        the same story.
      - "per_item" (arXiv): research papers are usually distinct
        findings, not chapters of one story, so this asks for one clear
        sentence per notable paper instead — still prose, not a bullet
        list, but each sentence stands on its own with its own citation.
    """
    if not items:
        return ""

    max_items = cfg.get("max_items_per_summary_call", 8)
    backend = cfg.get("backend", "none")
    if backend != "ollama":
        return _extractive_fallback(items, max_sentences=max_items if cite_style == "per_item" else 3)

    model = cfg.get("model", "llama3.1")
    ollama_url = cfg.get("ollama_url", "http://localhost:11434")

    bullet_list = "\n".join(
        f"- {i.title} ({i.url or 'no link'}): {i.summary[:400]}" for i in items[:max_items]
    )
    if cite_style == "per_item":
        style_instruction = (
            f"Write one clear sentence per paper stating its key finding — these are "
            f"separate studies, not chapters of one story, so don't blend two papers "
            f"into a single sentence. Cover as many of the {len(items[:max_items])} papers "
            f"as you reasonably can, most important first."
        )
    else:
        style_instruction = (
            f"Summarize the following {len(items[:max_items])} items into a tight, "
            f"flowing paragraph (4-6 sentences) — related items can share a sentence "
            f"when they're the same story. Mention the most important 2-3 developments."
        )
    prompt = (
        f"You are writing the '{section_name}' section of a weekly industry "
        f"briefing about {industry_name} for a busy professional. {style_instruction} "
        f"Write as prose — not a list, and no separate list or bullet points after it. "
        f"Whenever you mention a specific item by name, cite it as an inline markdown "
        f'link using its EXACT url from below, e.g. "Neuralink [announced]'
        f'(https://example.com/article) a new device" — never invent a url, and skip '
        f"the citation for an item marked 'no link'. Be factual, no fluff, no preamble "
        f"like 'Here is a summary'.\n\n"
        f"Items:\n{bullet_list}\n\nSummary:"
    )
    result = _ollama_generate(prompt, ollama_url, model, num_thread=cfg.get("num_thread"))
    return result or _extractive_fallback(items, max_sentences=max_items if cite_style == "per_item" else 3)


def summarize_item(item: Item, cfg: dict) -> str:
    """One-line 'why it matters' for a single high-priority item (e.g. a
    personalized match). Falls back to the raw summary if unavailable."""
    backend = cfg.get("backend", "none")
    if backend != "ollama":
        return " ".join(item.summary.split())[:200]

    model = cfg.get("model", "llama3.1")
    ollama_url = cfg.get("ollama_url", "http://localhost:11434")
    prompt = (
        f"In one concise sentence, explain why this item might matter to someone "
        f"tracking their industry:\nTitle: {item.title}\nDetails: {item.summary[:500]}\n\nSentence:"
    )
    result = _ollama_generate(prompt, ollama_url, model, timeout=60, num_thread=cfg.get("num_thread"))
    return result or item.summary[:280]
