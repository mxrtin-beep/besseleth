"""Summarizes the week's items with an LLM — by default the free, hosted
Groq API (https://console.groq.com — fast, no install, doesn't tie up
your own machine), falling back automatically to a local Ollama
(https://ollama.ai — `ollama pull llama3.1 && ollama serve`) if one is
configured and reachable, whenever Groq is rate-limited or unreachable.
Set summarizer.backend: "ollama" to use Ollama only (no Groq calls at
all — fully offline/local), or "none" for a plain extractive fallback.

Falls back to a plain extractive summary (first sentences) if no backend
produced a result, so the report always generates."""
from __future__ import annotations

import json
import os
import re

import requests

from .db import Item

# Any backend that actually calls an LLM — used everywhere a call site
# used to check `backend != "ollama"` (or `== "ollama"`) as a stand-in
# for "is a real LLM configured", back when Ollama was the only option.
LLM_BACKENDS = ("groq", "ollama")

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"


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


def _groq_generate(prompt: str, api_key: str, model: str, timeout: int = 60) -> tuple[str | None, bool]:
    """Returns (text, rate_limited). text is None on any failure — the
    caller decides what to fall back to; rate_limited is True
    specifically for a 429, so a caller with Ollama also configured can
    fall back to it instead of just giving up (Groq's free tier is
    plenty for normal use but a big backfill/re-enrich run can hit its
    per-minute caps — see README)."""
    if not api_key:
        print("[summarizer] No Groq API key configured (summarizer.groq_api_key or GROQ_API_KEY env var); skipping Groq.")
        return None, False
    try:
        resp = requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2},
            timeout=timeout,
        )
        if resp.status_code == 429:
            print("[summarizer] Groq rate limit hit.")
            return None, True
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip(), False
    except requests.RequestException as e:
        print(f"[summarizer] Groq unreachable/failed ({e}).")
        return None, False


def _llm_generate(prompt: str, cfg: dict, timeout: int = 120, num_thread: int | None = None) -> str | None:
    """The one entry point every summarizer/enrich LLM call should go
    through — dispatches on summarizer.backend and, for "groq" (the
    default), falls back to Ollama automatically if one is configured
    (summarizer.ollama_url/model set, or just left at their defaults —
    Ollama has no separate "enabled" flag, if backend isn't "groq" alone
    it's assumed reachable) and Groq was rate-limited or unreachable.
    Returns None (never raises) if nothing worked, so every caller's
    existing "result or fallback" pattern keeps working unchanged."""
    backend = cfg.get("backend", "groq")
    if backend == "groq":
        api_key = cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY", "")
        model = cfg.get("groq_model", DEFAULT_GROQ_MODEL)
        result, rate_limited = _groq_generate(prompt, api_key, model, timeout=min(timeout, 60))
        if result:
            return result
        if not cfg.get("groq_fallback_to_ollama", True):
            return None
        # Falls through here for any Groq failure — rate-limited, no key
        # configured, or a plain network/5xx error — rather than trying
        # to distinguish every failure mode; Ollama is either reachable
        # (worth trying) or it isn't (fails fast, same as Groq did).
        print("[summarizer] Falling back to Ollama for this call.")
        return _ollama_generate(
            prompt, cfg.get("ollama_url", "http://localhost:11434"), cfg.get("model", "llama3.1"),
            timeout=timeout, num_thread=num_thread,
        )
    if backend == "ollama":
        return _ollama_generate(
            prompt, cfg.get("ollama_url", "http://localhost:11434"), cfg.get("model", "llama3.1"),
            timeout=timeout, num_thread=num_thread,
        )
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
    if backend not in LLM_BACKENDS:
        return _extractive_fallback(items, max_sentences=max_items if cite_style == "per_item" else 3)

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
    result = _llm_generate(prompt, cfg, num_thread=cfg.get("num_thread"))
    if not result:
        return _extractive_fallback(items, max_sentences=max_items if cite_style == "per_item" else 3)

    if cite_style == "per_item":
        # Defensive backstop, not just a prompt instruction: an LLM
        # skipping a citation here and there is common enough that "ask
        # nicely" alone isn't reliable — append a source line for any
        # paper whose link didn't make it into the generated prose, so
        # every item is still traceable back to its source even if the
        # model dropped one.
        missing = [i for i in items[:max_items] if i.url and i.url not in result]
        if missing:
            result += "\n\n**Sources:** " + " · ".join(f"[{i.title}]({i.url})" for i in missing)

    return result


def _one_sentence(item: Item, industry_name: str, cfg: dict) -> str:
    """One concise sentence stating this item's key point — no title
    repeated verbatim, no preamble. Used to build a numbered bullet list
    where each item gets its own LLM call rather than one combined call
    asked to cite N items correctly in one shot; a combined call over
    several items is exactly what was silently dropping citations (or
    the whole link) for some of them — asking per item, then attaching
    the number/link ourselves in code, can't drop one."""
    backend = cfg.get("backend", "none")
    if backend not in LLM_BACKENDS:
        return (" ".join(item.summary.split())[:200] or item.title).rstrip(".")

    prompt = (
        f"In one concise sentence, state the key point of this {industry_name} item — "
        f"don't repeat the title verbatim, no preamble like 'This article...':\n"
        f"Title: {item.title}\nText: {item.summary[:600]}\n\nSentence:"
    )
    result = _llm_generate(prompt, cfg, timeout=60, num_thread=cfg.get("num_thread"))
    return (result or item.summary[:200] or item.title).strip().rstrip(".")


def summarize_items_numbered(items: list[Item], industry_name: str, cfg: dict) -> str:
    """Renders items as a bullet list, one sentence per item, each ending
    in a numbered, linked citation — "- Some finding here [1]." — instead
    of an inline markdown link buried mid-sentence. The number is
    assigned in code from the item's position, not asked of the LLM, so
    a link can never go missing or land on the wrong item."""
    if not items:
        return ""
    max_items = cfg.get("max_items_per_summary_call", 8)
    lines = []
    for i, item in enumerate(items[:max_items], start=1):
        sentence = _one_sentence(item, industry_name, cfg)
        cite = f" [{i}]({item.url})" if item.url else f" [{i}]"
        lines.append(f"- {sentence}.{cite}")
    return "\n".join(lines)


def summarize_context(new_items: list[Item], history: dict, industry_name: str, cfg: dict) -> str:
    """The report's closing section: not a recap of what's already
    summarized above, and not a recitation of besseleth's own database
    stats either — what this run's new items actually MEAN set against
    everything accumulated across every past run (`history`, from
    DB.accumulated_knowledge_stats()): is a quiet org suddenly active
    again, does this continue a trend already being tracked, is it a
    genuinely new direction. Empty if there's nothing new to place."""
    if not new_items:
        return ""

    total_items = history.get("total_items", 0)
    total_orgs = history.get("total_orgs", 0)
    top_orgs = history.get("top_orgs", [])
    top_orgs_str = ", ".join(f"{org} ({n})" for org, n in top_orgs)
    earliest = history.get("earliest_date") or "an earlier date"

    # A concrete hook for the LLM to reason about, rather than just handing
    # it aggregate counts to paraphrase: which of today's items involve an
    # org that's normally one of the most active (a returning player worth
    # remarking on), and which involve one that's rare/new in the record
    # entirely (worth flagging as new, not just "also happened").
    top_org_names = {org for org, _ in top_orgs}
    new_item_orgs = {i.org for i in new_items if i.org}
    returning_orgs = sorted(new_item_orgs & top_org_names)
    fresh_orgs = sorted(new_item_orgs - top_org_names)

    fallback = (
        f"Besseleth has accumulated {total_items} items on {industry_name} since {earliest}, "
        f"across {total_orgs} organizations"
        + (f" — most active so far: {top_orgs_str}." if top_orgs_str else ".")
    )

    backend = cfg.get("backend", "none")
    if backend not in LLM_BACKENDS:
        return fallback

    max_items = cfg.get("max_items_per_summary_call", 8)
    new_titles = "\n".join(f"- {i.title}" for i in new_items[:max_items])
    returning_note = f" (here: {', '.join(returning_orgs)})" if returning_orgs else ""
    fresh_note = f" (here: {', '.join(fresh_orgs)})" if fresh_orgs else ""
    prompt = (
        f"You are writing the closing 'Big picture' section of a weekly {industry_name} "
        f"briefing — analysis for someone who already read the sections above, not a recap "
        f"of the database. You have background stats on everything besseleth has ever "
        f"tracked: {total_items} items since {earliest}, across {total_orgs} organizations, "
        f"most active historically: {top_orgs_str or 'none tracked yet'}.\n\n"
        f"Do NOT write a sentence that just restates these numbers (e.g. never write anything "
        f"shaped like 'Besseleth has accumulated N items... most active: X (Y), Z (W)' — that "
        f"is a database dump, not analysis, and the reader already has that data if they want "
        f"it). Instead, in 2-4 sentences, say what today's new items actually MEAN given that "
        f"history — pick ONE genuine angle and commit to it, e.g.: an org among today's items "
        f"that's normally one of the most active ones{returning_note} showing up again — is "
        f"this more of the same, or does it look like a shift in what they're doing; an org in "
        f"today's items that's rare or new in the whole record{fresh_note} — is this a new "
        f"entrant worth watching; or, if neither applies cleanly, whether today's items "
        f"continue a pattern you'd expect from this field's history or actually cut against "
        f"it. If you genuinely can't identify a real angle from what's given, say plainly that "
        f"this week doesn't point to a clear shift, rather than manufacturing one. No preamble "
        f"like 'Here is a summary', no fluff.\n\n"
        f"Today's new items:\n{new_titles}\n\nBig picture:"
    )
    result = _llm_generate(prompt, cfg, num_thread=cfg.get("num_thread"))
    return result or fallback


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def summarize_linkedin_item(item: Item, cfg: dict) -> str:
    """LinkedIn pastes are almost always a hiring post, and the pasted
    text is the raw post — long, sometimes cut off mid-sentence by
    whatever copied it, and headed by a useless generic page title
    ("Feed | LinkedIn") rather than anything specific. The report just
    wants the basics: role, company, location — not that raw text
    truncated. Falls back to a short plain snippet (not the useless
    title) if the LLM backend is off/unreachable or extraction fails."""
    text = " ".join(item.summary.split())
    fallback = (text[:150].rsplit(" ", 1)[0] + "…") if len(text) > 150 else text
    fallback = fallback or item.title

    backend = cfg.get("backend", "none")
    if backend not in LLM_BACKENDS:
        return fallback

    prompt = (
        "The following is a pasted LinkedIn post, almost always a hiring announcement. "
        'Extract just the basics as JSON: {"role": "...", "company": "...", "location": "..."} '
        "— use null for any field not clearly stated, don't guess. Output ONLY the JSON, no "
        f"other text.\n\nPost:\n{item.summary[:800]}\n\nJSON:"
    )
    result = _llm_generate(prompt, cfg, timeout=60, num_thread=cfg.get("num_thread"))
    match = _JSON_BLOCK_RE.search(result or "")
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        data = {}

    role = (data.get("role") or "").strip()
    company = (data.get("company") or "").strip()
    location = (data.get("location") or "").strip()
    if not role and not company:
        return fallback

    line = role or "Opportunity"
    if company:
        line += f" at {company}"
    if location:
        line += f" ({location})"
    return line


def summarize_item(item: Item, cfg: dict) -> str:
    """One-line 'why it matters' for a single high-priority item (e.g. a
    personalized match). Falls back to the raw summary if unavailable."""
    backend = cfg.get("backend", "none")
    if backend not in LLM_BACKENDS:
        return " ".join(item.summary.split())[:200]

    prompt = (
        f"In one concise sentence, explain why this item might matter to someone "
        f"tracking their industry:\nTitle: {item.title}\nDetails: {item.summary[:500]}\n\nSentence:"
    )
    result = _llm_generate(prompt, cfg, timeout=60, num_thread=cfg.get("num_thread"))
    return result or item.summary[:280]
