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

# The prompt's answer shapes use bracketed placeholder tokens
# (<University>, <Principal Investigator Last Name>, <Lab Name>,
# <PI/Lab Name>, <Company Name>) to show where a real value goes. A
# weak/local model sometimes echoes one of these literally instead of
# substituting - with or without the angle brackets, and with or
# without a trailing " Lab" if it was the lab-name slot - producing a
# stored org like "<University>, Deng Lab" or "Unknown Institution,
# Principal Investigator Last Name Lab". Neither of those is malformed
# by shape (they're made entirely of real letters, same as an actual
# answer), so the "does this contain at least one real letter/digit"
# check lets them straight through - this catches that specific
# failure mode instead.
_PLACEHOLDER_TOKENS = {
    "university",
    "principal investigator last name",
    "lab name",
    "pi/lab name",
    "company name",
}


def _snippets_mention_title(snippets: list[str], title: str) -> bool:
    """True if at least one search snippet plausibly discusses THIS
    paper, not just the general topic it's about — the same "the
    grounding must actually be about the thing being asked, not merely
    superficially related" principle web_lookup.py's Wikidata/Wikipedia
    entity-match checks apply to org locations. A DuckDuckGo response
    that comes back non-empty isn't necessarily a real match for the
    quoted-title search that was asked for — an exact-phrase search that
    silently degraded to a broad one (network trouble, no real hit) can
    still return snippets about something else in the same field
    entirely. Feeding those to the LLM anyway doesn't produce "no
    answer" — it produces a confident answer about whatever the
    snippets DO discuss, which for a well-covered field tends to be its
    single most prominent name (this is how "Shenoy Lab" — a genuinely
    famous BCI lab — ended up mislabeled onto several unrelated papers
    by different real authors, the exact same failure shape as a title
    word getting hallucinated into the lab-name slot). Requires a
    several-word run of the actual title to appear in at least one
    snippet; a title too short to build a reliable phrase from is let
    through rather than blocked on this check alone."""
    title_words = re.findall(r"[a-z0-9]+", title.lower())
    if len(title_words) < 4:
        return True
    normalized_snippets = [re.sub(r"[^a-z0-9]+", " ", s.lower()) for s in snippets]
    phrase_len = min(6, len(title_words))
    for n in range(phrase_len, 3, -1):
        for i in range(len(title_words) - n + 1):
            phrase = " ".join(title_words[i : i + n])
            if any(phrase in s for s in normalized_snippets):
                return True
    return False


def _is_unsubstituted_placeholder(segment: str) -> bool:
    text = (segment or "").strip().strip("<>").strip()
    text = re.sub(r"\s+lab$", "", text, flags=re.IGNORECASE)
    return text.lower() in _PLACEHOLDER_TOKENS


def _is_slot_confused_university(university: str) -> bool:
    """"Undetermined" is only ever valid as the LAB slot ("<University>,
    Undetermined Lab" — a known university but no specific lab/PI). A
    model that instead writes "Undetermined, <Lab> Lab" has put the
    same word in the wrong slot — still a real answer shape by every
    other check (real letters on both sides of the comma), but not a
    real university any more than "<University>" itself was."""
    return university.strip().lower() == "undetermined"


def _lab_name_is_title_word(lab: str, title: str, authors_institutions: list[tuple[str, list[str], bool]]) -> bool:
    """True if `lab` (the parsed "... Lab" segment, minus that suffix)
    is actually a word straight out of the paper's own title — the
    prompt explicitly warns the model that a title often names the
    paper's SUBJECT MATTER (a technique/architecture like "Mamba",
    "Transformer") in a way that grammatically fits the "<Lab Name>
    Lab" shape, but a weak/local model doesn't reliably follow that
    warning (same reason the markdown-bullet and placeholder-echo bugs
    exist). This is the deciding output-side check rather than relying
    on the prompt alone: if the "lab" word also appears in the title
    AND does not match any real author's name, it's the paper's own
    subject matter reflected back, not a real lab — e.g. many
    unrelated papers about the "Mamba" architecture all getting
    labeled "Mamba Lab" regardless of their actual, wildly different
    authors/institutions."""
    core = re.sub(r"\s+lab$", "", lab.strip(), flags=re.IGNORECASE).strip().lower()
    if not core:
        return False
    title_words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]*", title)}
    if core not in title_words:
        return False
    author_words = {
        w.lower()
        for name, _, _ in authors_institutions
        for w in re.findall(r"[A-Za-z][A-Za-z\-]*", name)
    }
    return core not in author_words


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
        f"IMPORTANT: the paper's title often names a technique, architecture, method, or model (e.g. "
        f'"Transformer", "Mamba", "Diffusion") as its SUBJECT MATTER — that is what the paper is ABOUT, never '
        f'the lab\'s own name, even though it fits grammatically into the "<Lab Name> Lab" shape. Only use a '
        f"name explicitly given to you above (an author, institution, or corresponding-author note) for the "
        f"lab/PI — never a term pulled from the title itself.\n"
        f'Never invent a name you are not reasonably confident in from what is actually given above — if you '
        f'genuinely cannot tell whether this is even academic or industry work, answer exactly "unknown".'
    )


def normalize_stored_paper_org(
    org: str | None, title: str = "", authors: list[str] | None = None
) -> tuple[str | None, str | None]:
    """Runs an already-stored org value back through the exact same
    parsing/validation real LLM output goes through (_parse_response),
    plus the same title-hallucination check resolve_paper_org()/
    resolve_paper_org_via_search() apply to fresh output — there is one
    set of rules for what a valid paper org looks like, applied
    identically whether the value was just generated or has been
    sitting in the database for months, rather than a separately
    hand-maintained "does this look malformed" checklist that can (and
    did) drift out of sync with the real rules.

    Returns (org, org_type) — possibly the same value back unchanged
    (already valid), possibly a FREE cosmetic fix recovered from the
    stored text itself (a leading "- \"" artifact stripped, an empty
    university salvaged into "Unknown Institution" — no LLM call
    needed, since the real answer was already sitting right there), or
    (None, None) when nothing in the stored text is recoverable at all
    (a placeholder echo, a title-hallucinated lab name, a bare hedge) —
    the caller's signal that only a genuine fresh resolution attempt
    (real institution data, then a web search) could possibly do
    better, not that the row should be cleared outright without trying.

    `authors` (the same raw author-name list resolve_paper_org_with_fallback
    would have had) is optional — pass it so a lab name that
    legitimately IS an author's own name isn't flagged just because it
    also happens to appear in the title."""
    if not org:
        return None, None
    parsed_org, parsed_type = _parse_response(org)
    if parsed_org and parsed_type == "academic" and "," in parsed_org:
        authors_institutions = [(name, [], False) for name in (authors or [])]
        if _lab_name_is_title_word(parsed_org.rpartition(",")[2], title, authors_institutions):
            return None, None
    return parsed_org, parsed_type


def is_valid_stored_paper_org(org: str | None, title: str = "", authors: list[str] | None = None) -> bool:
    """True if `org`, exactly as currently stored, needs no fix at all —
    see normalize_stored_paper_org. False covers both "needs a free
    cosmetic fix" and "needs a real re-resolution attempt"; callers that
    care about the difference should call normalize_stored_paper_org
    directly instead."""
    if not org:
        return True
    parsed_org, _ = normalize_stored_paper_org(org, title, authors)
    return parsed_org == org


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
    # An empty or punctuation-only university slot before the comma
    # ("- , Zhu Lab" — already collapsed by the bullet-strip above into
    # ", Zhu Lab" — or a bare ", Ortiz-Juza lab") still has a real
    # lab/PI name sitting right there. Salvage it into the ONE canonical
    # way this tool represents "we know the lab, not the university"
    # (the prompt's own explicit "Unknown Institution, <PI/Lab> Lab"
    # shape) instead of discarding a real, useful answer just because
    # the university half came back blank — losing the lab name too
    # over that is strictly worse than keeping it with an honest
    # "don't know the university" marker.
    text = re.sub(r"^[\s\-–—.]*,\s*", "Unknown Institution, ", text)
    # A "?" anywhere is the model hedging despite the prompt never
    # offering a hedge shape — it's told to use "Undetermined Lab"/
    # "unknown" instead, so a "?" surviving means the rest of the
    # answer is unreliable too; treat the whole thing as no answer
    # rather than store a half-hedged guess.
    if "?" in text:
        return None, None
    match = _LAB_SHAPE_RE.match(text)
    if match:
        university = match.group("university").strip()
        lab = match.group("lab").strip()
        # The regex matches "lab"/"Lab"/"LAB" case-insensitively but
        # captures whatever casing the model actually wrote — standardize
        # the trailing word to "Lab" every time so this doesn't become
        # one more thing that's inconsistent from one stored org to the
        # next depending on how a given model call happened to write it.
        lab = re.sub(r"\blab$", "Lab", lab, flags=re.IGNORECASE)
        # A bare "-"/"—"/"." etc. passes a plain truthiness check (it's a
        # non-empty string) but isn't a real university name — this is
        # exactly what produced stored garbage like "- , Zhu Lab" before
        # this check existed. Require at least one real letter/digit.
        if not re.search(r"[A-Za-z0-9]", university) or not re.search(r"[A-Za-z0-9]", lab):
            return None, None
        if _is_unsubstituted_placeholder(university) or _is_unsubstituted_placeholder(lab):
            return None, None
        if _is_slot_confused_university(university):
            return None, None
        return f"{university}, {lab}", "academic"
    # No ", ... Lab" shape — only valid as a bare company/org name, and
    # only if it actually reads like one (short, not a sentence the model
    # wrote instead of following the format, and not leading/trailing
    # punctuation left over from a malformed "<blank>, X Lab" attempt
    # that didn't even match the regex above).
    if (
        0 < len(text.split()) <= 6
        and "\n" not in text
        and re.match(r"^[A-Za-z0-9]", text)
        and not _is_unsubstituted_placeholder(text)
    ):
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
    org, org_type = _parse_response(result)
    if org_type == "academic" and "," in org and _lab_name_is_title_word(org.rpartition(",")[2], title, authors_institutions):
        return None, None
    return org, org_type


def resolve_paper_org_via_search(title: str, config: Config) -> tuple[str | None, str | None]:
    """Tier 2 (fallback) — see module docstring. Only reached when tier 1
    had literally no institution data to work with."""
    if config.summarizer.get("backend", "groq") not in summarizer_mod.LLM_BACKENDS:
        return None, None
    snippets = web_lookup.duckduckgo_search(f'"{title}" lab university')
    if not snippets or not _snippets_mention_title(snippets, title):
        return None, None
    context_block = "Web search snippets about this paper:\n" + "\n".join(f"- {s}" for s in snippets)
    prompt = _build_prompt(title, context_block, config)
    result = summarizer_mod._llm_generate(prompt, config.summarizer, timeout=60, num_thread=config.summarizer.get("num_thread"))
    if not result:
        return None, None
    org, org_type = _parse_response(result)
    # No author data at all reached this tier — there's nothing an
    # overlapping word COULD legitimately be except the title's own
    # subject matter, so no author-name exception here.
    if org_type == "academic" and "," in org and _lab_name_is_title_word(org.rpartition(",")[2], title, []):
        return None, None
    return org, org_type


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
