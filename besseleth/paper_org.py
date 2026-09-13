"""Resolves the specific lab/company behind a papers-source item — a
total replacement, for papers only, of the old org-extraction path
(_match_known_lab/_clean_org_value/_looks_like_a_named_org/
_canonicalize_new_org in enrich.py), which was producing confidently
wrong answers (a London sleep-disorder paper labeled "Stanford, Shenoy
Lab" is what prompted this rewrite) — that path asked the LLM to guess
org from a short summary alone, with no real grounding. News/blog items
are UNCHANGED — they still go through enrich.py's own org path.

Runs at FETCH time (see scrapers/openalex_scraper.py and
scrapers/arxiv_scraper.py), not in the later general enrichment pass —
because the one thing that actually grounds this reliably (each
author's real institution, from OpenAlex) only exists in the API
response at the moment a paper is fetched; by general-enrichment time
it would already be gone.

Two tiers, in order:
  1. resolve_paper_org() — an LLM call grounded in real author-
     institution data (OpenAlex's own affiliation records) when there
     is any. This is NOT "ask the LLM to recall who wrote this" — the
     institutions are handed to it as fact; it's only being asked to
     format/summarize what's already given, occasionally reconciling
     multiple authors at different institutions into one likely lead
     lab. Still asked (institution-less) for an arXiv item with no
     OpenAlex match at all — arXiv itself never has affiliation data,
     so there's nothing to ground it with in that case.
  2. resolve_paper_org_via_search() — for a paper where NO author has
     an institution on record (a fraction of OpenAlex works, and any
     arXiv-only paper), a real DuckDuckGo web search read by the LLM,
     same trust model as web_lookup.py's location tier 3: the search
     SNIPPETS are the grounding, never the model's own memory. Neither
     tier ever asks the LLM to just recall a fact with nothing to back
     it — that bare-guess shape is exactly what this module exists to
     replace.

Either tier returns None, None on any failure or genuine uncertainty —
an unresolved org is left for a later run to retry, never a guess.
"""
from __future__ import annotations

import re

from . import summarizer as summarizer_mod
from . import web_lookup
from .config import Config

# Same shape the LLM is asked to answer in: "<University>, <PI/Lab name> Lab".
# A bare company name (no ", ... Lab" suffix) is the other valid shape —
# see _parse_response.
_LAB_SHAPE_RE = re.compile(r"^(?P<university>.+?),\s*(?P<lab>.+?\bLab)\.?$", re.IGNORECASE)

_NON_ANSWERS = {"unknown", "n/a", "none", "null", "unclear", "not specified"}


def _format_authors_with_institutions(authors_institutions: list[tuple[str, list[str], bool]]) -> str:
    parts = []
    last_i = len(authors_institutions) - 1
    for i, (name, institutions, is_corresponding) in enumerate(authors_institutions):
        inst_text = ", ".join(institutions) if institutions else "institution unknown"
        markers = []
        if is_corresponding:
            markers.append("CORRESPONDING AUTHOR")
        if i == 0 and last_i > 0:
            markers.append("listed first")
        if i == last_i and last_i > 0:
            markers.append("listed last")
        marker = f" [{', '.join(markers)}]" if markers else ""
        parts.append(f"{name} ({inst_text}){marker}")
    return "; ".join(parts) or "(no author information)"


def _build_prompt(
    title: str, context_block: str, config: Config, has_corresponding: bool = False, multiple_authors: bool = False
) -> str:
    if has_corresponding:
        author_note = (
            " The author marked CORRESPONDING AUTHOR above is real metadata from the paper itself (who to "
            "contact about it) and is usually the PI/lab head — weight them heavily for the PI/lab name over "
            "mere list position."
        )
    elif multiple_authors:
        author_note = (
            " No corresponding-author data is available for this one, so fall back on the general convention "
            "in most STEM fields: the author listed LAST is usually the senior investigator/PI who leads the "
            "lab, while the author listed FIRST is very often a student, postdoc, or research assistant who "
            "did the hands-on work, not the lab's lead — prefer the last-listed author for the PI/lab name "
            "unless something else in the title clearly points elsewhere. This is a convention, not a certainty "
            "— use null/'Undetermined Lab' rather than force a confident answer you're not sure of."
        )
    else:
        author_note = ""
    return (
        f'Paper title: "{title}"\n'
        f"{context_block}\n\n"
        f"Which specific {config.industry_name} lab or company produced this paper?{author_note} "
        f"Answer with ONLY one of these exact shapes, nothing else — no explanation, no extra words:\n"
        f'- "<University>, <Principal Investigator Last Name> Lab" if a specific PI-led academic lab is clear\n'
        f'- "<University>, <Lab Name> Lab" if the lab has its own name not tied to one PI\n'
        f'- "<University>, Undetermined Lab" if it is clearly academic work at a KNOWN university but no '
        f'specific lab/PI is clear\n'
        f'- "Unknown Institution, <PI/Lab Name> Lab" if a PI/lab name is reasonably clear but the specific '
        f'university/institution is NOT — use the literal words "Unknown Institution", never a placeholder '
        f'like "-", a blank, or punctuation for the university part\n'
        f'- "<Company Name>" ALONE (no "Lab" suffix, no university) if this is company/industry research\n'
        f'Never invent a name you are not reasonably confident in from what is actually given above — if you '
        f'genuinely cannot tell whether this is even academic or industry work, answer exactly "unknown".'
    )


def looks_like_malformed_paper_org(org: str | None) -> bool:
    """True for a stored org value that could only have come from a
    parsing bug that has since been fixed (a leading "- \"" markdown-
    bullet artifact, an empty/punctuation-only university before the
    comma) — used by enrich.py's retroactive cleanup sweep to clear out
    already-stored garbage from before _parse_response's cleaning/
    validation existed, since a later re-resolution only overwrites a
    stored value when it finds something NEW to replace it with, and
    silently leaves old garbage in place forever otherwise."""
    if not org:
        return False
    if not re.match(r"^[A-Za-z0-9]", org.strip()):
        return True
    if "," in org:
        university, _, lab = org.partition(",")
        if not re.search(r"[A-Za-z0-9]", university) or not re.search(r"[A-Za-z0-9]", lab):
            return True
    return False


def _parse_response(raw: str) -> tuple[str | None, str | None]:
    """Returns (org, org_type). org_type is "academic" for the
    "<University>, ... Lab" shape, "industry" for a bare name — never
    guessed beyond what the shape itself already tells us."""
    text = raw.strip()
    # A weaker/local model doesn't reliably follow "ONLY the answer, no
    # extra words" — it sometimes wraps the answer in a markdown bullet
    # ("- \"University, X Lab\""), which used to become part of the
    # "university" capture verbatim (real bug: this produced a stored
    # org of literally "- \"İstanbul ... Üniversitesi, Karaçay Lab" that
    # then got sent to the NIH RePORTER API and 400'd). Strip leading
    # list markers/quotes and trailing quotes before matching, same idea
    # as _clean_org_value's hedging-prose recovery in enrich.py.
    text = re.sub(r"^[\s\-\*•>]+", "", text)
    # None of the valid answer shapes legitimately contain a DOUBLE quote
    # anywhere — a stray one (leading, trailing, or mid-string right
    # before the comma, e.g. `"Penn State", Nguyen Lab`) is always
    # leftover quoting artifact, never real content, so drop all of
    # those outright. A single quote/apostrophe, unlike a double quote,
    # can be real content ("O'Brien Lab", "Xi'an Jiaotong University"),
    # so that one's only trimmed from the very ends, not mid-string.
    text = text.replace('"', "").strip("'").strip()
    text = text.rstrip(".")
    if not text or text.lower() in _NON_ANSWERS:
        return None, None
    match = _LAB_SHAPE_RE.match(text)
    if match:
        university = match.group("university").strip()
        lab = match.group("lab").strip()
        # A bare "-"/"—"/"." etc. passes a plain truthiness check (it's a
        # non-empty string) but isn't a real university name — this is
        # exactly what produced stored garbage like "- , Zhu Lab" before
        # this check existed. Require at least one real letter/digit.
        if not re.search(r"[A-Za-z0-9]", university) or not re.search(r"[A-Za-z0-9]", lab):
            return None, None
        return f"{university}, {lab}", "academic"
    # No ", ... Lab" shape — only valid as a bare company/org name, and
    # only if it actually reads like one (short, not a sentence the model
    # wrote instead of following the format, and not leading/trailing
    # punctuation left over from a malformed "<blank>, X Lab" attempt
    # that didn't even match the regex above).
    if 0 < len(text.split()) <= 6 and "\n" not in text and re.match(r"^[A-Za-z0-9]", text):
        return text, "industry"
    return None, None


def resolve_paper_org(
    title: str, authors_institutions: list[tuple[str, list[str], bool]], config: Config
) -> tuple[str | None, str | None]:
    """Tier 1 — see module docstring. `authors_institutions` is
    [(author_name, [institution_name, ...], is_corresponding), ...]; an
    empty inner institutions list per author (or the whole thing empty)
    is fine — it's what an arXiv item with no OpenAlex match has to
    offer, and the prompt still asks, just with nothing to ground the
    answer beyond author names."""
    if config.summarizer.get("backend", "groq") not in summarizer_mod.LLM_BACKENDS:
        return None, None
    has_corresponding = any(is_corresponding for _, _, is_corresponding in authors_institutions)
    context_block = (
        f"Authors and their institutions (from OpenAlex — factual, not a guess):\n"
        f"{_format_authors_with_institutions(authors_institutions)}"
        if authors_institutions
        else "(no author or institution information available)"
    )
    prompt = _build_prompt(
        title, context_block, config, has_corresponding=has_corresponding,
        multiple_authors=len(authors_institutions) > 1,
    )
    result = summarizer_mod._llm_generate(prompt, config.summarizer, timeout=60, num_thread=config.summarizer.get("num_thread"))
    if not result:
        return None, None
    return _parse_response(result)


def resolve_paper_org_via_search(title: str, config: Config) -> tuple[str | None, str | None]:
    """Tier 2 (fallback) — see module docstring. Only reached when tier 1
    had literally no institution data to work with."""
    if config.summarizer.get("backend", "groq") not in summarizer_mod.LLM_BACKENDS:
        return None, None
    snippets = web_lookup.duckduckgo_search(f'"{title}" lab university')
    if not snippets:
        return None, None
    context_block = "Web search snippets about this paper:\n" + "\n".join(f"- {s}" for s in snippets)
    prompt = _build_prompt(title, context_block, config)
    result = summarizer_mod._llm_generate(prompt, config.summarizer, timeout=60, num_thread=config.summarizer.get("num_thread"))
    if not result:
        return None, None
    return _parse_response(result)


def resolve_paper_org_with_fallback(
    title: str, authors_institutions: list[tuple[str, list[str], bool]], config: Config
) -> tuple[str | None, str | None]:
    """Both tiers, in order — the one entry point scrapers should call.
    Skips straight to tier 2 when there's no institution data at all to
    ground tier 1 with (asking the LLM the same question twice with the
    same nothing-to-go-on isn't worth a second call)."""
    has_institutions = any(institutions for _, institutions, _ in authors_institutions)
    if has_institutions:
        org, org_type = resolve_paper_org(title, authors_institutions, config)
        if org:
            return org, org_type
    return resolve_paper_org_via_search(title, config)
