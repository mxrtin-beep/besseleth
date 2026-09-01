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
    text = " ".join(f"{i.title}." for i in items[:max_sentences])
    return text or "No notable developments this week."


def _ollama_generate(prompt: str, ollama_url: str, model: str, timeout: int = 120) -> str | None:
    try:
        resp = requests.post(
            f"{ollama_url.rstrip('/')}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except requests.RequestException as e:
        print(f"[summarizer] Ollama unreachable ({e}); using extractive fallback.")
        return None


def summarize_section(items: list[Item], section_name: str, industry_name: str, cfg: dict) -> str:
    """Returns a short prose summary (2-5 sentences) of the given items."""
    if not items:
        return ""

    backend = cfg.get("backend", "none")
    if backend != "ollama":
        return _extractive_fallback(items)

    model = cfg.get("model", "llama3.1")
    ollama_url = cfg.get("ollama_url", "http://localhost:11434")
    max_items = cfg.get("max_items_per_summary_call", 8)

    bullet_list = "\n".join(
        f"- {i.title}: {i.summary[:400]}" for i in items[:max_items]
    )
    prompt = (
        f"You are writing the '{section_name}' section of a weekly industry "
        f"briefing about {industry_name} for a busy professional. Summarize the "
        f"following {len(items[:max_items])} items into a tight, informative "
        f"paragraph (4-6 sentences). Mention the most important 2-3 developments "
        f"by name. Be factual, no fluff, no preamble like 'Here is a summary'.\n\n"
        f"Items:\n{bullet_list}\n\nSummary:"
    )
    result = _ollama_generate(prompt, ollama_url, model)
    return result or _extractive_fallback(items)


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
    result = _ollama_generate(prompt, ollama_url, model, timeout=60)
    return result or item.summary[:280]
