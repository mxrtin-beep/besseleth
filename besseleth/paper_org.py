"""Resolves the specific university/institute/hospital or company behind
a papers-source item — a total replacement, for papers only, of the old
org-extraction path (_match_known_lab/_clean_org_value/
_looks_like_a_named_org/_canonicalize_new_org in enrich.py), which was
producing confidently wrong answers (a London sleep-disorder paper
labeled "Stanford, Shenoy Lab" is what prompted this rewrite) — that
path asked the LLM to guess org from a short summary alone, with no real
grounding.

Deliberately resolves ONLY the institution/company, never a specific
PI-led lab within it. Naming the specific lab was tried first and spent
most of a session getting fixed and re-broken: a weak/local model
doesn't reliably follow "never invent a lab name" (it pulled the title's
own subject-matter word — "Mamba Lab" on unrelated papers), doesn't
reliably avoid echoing the prompt's own placeholder tokens
("<University>, Deng Lab"), and doesn't reliably keep "university" and
"lab" in the right slots ("Undetermined, Poon Lab" — Undetermined is
only ever valid as the LAB slot). Institution identity alone is a much
smaller, much more grounded surface: OpenAlex's affiliation data names
the institution directly (no LLM synthesis needed at all in the common
case — see resolve_paper_org()), and even the fallback web-search tier
only has to get ONE plain name right, not reconcile which of several
authors is the PI and invent a name for their lab.

Runs at FETCH time (see scrapers/openalex_scraper.py and
scrapers/arxiv_scraper.py), not in the later general enrichment pass —
because the one thing that actually grounds this reliably (each
author's real institution, from OpenAlex) only exists in the API
response at the moment a paper is fetched; by general-enrichment time
it would already be gone.

Two tiers, in order:
  1. resolve_paper_org() — picks DIRECTLY from real author-institution
     data (OpenAlex's own affiliation records) when there is any — no
     LLM call needed at all. Prefers the corresponding author's listed
     institution (most reliable signal for whose work this actually
     is), then the last-listed author's (the common last-author-is-PI
     convention in most STEM fields), then the first author's, as a
     descending-confidence fallback chain.
  2. resolve_paper_org_via_search() — for a paper where NO author has
     an institution on record (a fraction of OpenAlex works, and any
     arXiv-only paper), a real DuckDuckGo web search read by the LLM,
     same trust model as web_lookup.py's location tier 3: the search
     SNIPPETS are the grounding, never the model's own memory.

Either tier returns None, None on any failure or genuine uncertainty —
an unresolved org is left for a later run to retry, never a guess.
"""
from __future__ import annotations

import re

from . import summarizer as summarizer_mod
from . import web_lookup
from .config import Config

_NON_ANSWERS = {"unknown", "n/a", "none", "null", "unclear", "not specified"}

# A rough but reliable "is this name an academic/research institution"
# signal — universities, colleges, institutes, hospitals, and the like
# are essentially always named as such; anything else defaults to
# "industry". Good enough for a filter tag; not meant to be perfect.
_ACADEMIC_INSTITUTION_RE = re.compile(
    r"\b(university|univ\.?|college|institute|polytechnic|academy|"
    r"hospital|medical (?:center|centre|school)|school of medicine)\b",
    re.IGNORECASE,
)

# The prompt's one answer shape uses a bracketed placeholder token
# (<University or Company Name>) to show where the real value goes. A
# weak/local model sometimes echoes that literally instead of
# substituting a real value — this catches it; it's made entirely of
# real letters, so the "does this contain a real letter" shape check
# alone lets it straight through.
_PLACEHOLDER_TOKENS = {"university or company name", "university", "company name", "institution name"}


def classify_org_type(org: str) -> str:
    """"academic" if `org`'s name itself reads like a university/
    institute/hospital, else "industry" — a simple, deterministic name-
    pattern check rather than one more thing to ask an LLM to guess."""
    return "academic" if _ACADEMIC_INSTITUTION_RE.search(org) else "industry"


def _is_unsubstituted_placeholder(text: str) -> bool:
    return text.strip().strip("<>").strip().lower() in _PLACEHOLDER_TOKENS


def _org_is_title_word(org: str, title: str, authors_institutions: list[tuple[str, list[str], bool]]) -> bool:
    """True if `org` is actually a word straight out of the paper's own
    title — the prompt explicitly warns the model that a title often
    names the paper's SUBJECT MATTER (a technique/architecture like
    "Mamba", "Transformer"), but a weak/local model doesn't reliably
    follow that warning (same reason the markdown-bullet and
    placeholder-echo bugs exist). This is the deciding output-side
    check rather than relying on the prompt alone: if `org` also
    appears in the title AND doesn't match any real author's name
    (a legitimately eponymous institution — "Broad Institute" from an
    author literally named Broad is vanishingly rare but not
    impossible — errs toward not flagging a real author-name overlap),
    it's the paper's own subject matter reflected back, not a real
    institution."""
    core = org.strip().lower()
    if not core or " " in core:
        # A multi-word org ("Stanford University") is not a single
        # title word by construction — only a single bare word (the
        # shape a hallucinated technique name like "Mamba" actually
        # takes) is ever at risk of this.
        return False
    # Letter runs only, not hyphenated compounds as one token — "Mamba"
    # must be found as its own word even in a title like "A Mamba-based
    # approach", where a hyphen-inclusive pattern would only ever see
    # the compound "Mamba-based" and miss the plain "Mamba" it contains.
    title_words = {w.lower() for w in re.findall(r"[A-Za-z]+", title)}
    if core not in title_words:
        return False
    author_words = {
        w.lower()
        for name, _, _ in authors_institutions
        for w in re.findall(r"[A-Za-z]+", name)
    }
    return core not in author_words


def _snippets_mention_title(snippets: list[str], title: str) -> bool:
    """True if at least one search snippet plausibly discusses THIS
    paper, not just the general topic it's about — the same "the
    grounding must actually be about the thing being asked, not merely
    superficially related" principle web_lookup.py's Wikidata/Wikipedia
    entity-match checks apply to org locations. A DuckDuckGo response
    that comes back non-empty isn't necessarily a real match for the
    quoted-title search that was asked for — an exact-phrase search
    that silently degraded to a broad one (network trouble, no real
    hit) can still return snippets about something else in the same
    field entirely, and the LLM reading them doesn't answer "I don't
    know" — it answers confidently about whatever the snippets DO
    discuss. Requires a several-word run of the actual title to appear
    in at least one snippet; a title too short to build a reliable
    phrase from is let through rather than blocked on this check
    alone."""
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


def _parse_response(raw: str) -> tuple[str | None, str | None]:
    """Returns (org, org_type) for tier 2's LLM-generated text — a
    plain institution/company name, nothing else."""
    text = raw.strip()
    # A weaker/local model doesn't reliably follow "ONLY the answer, no
    # extra words" — it sometimes wraps the answer in a markdown bullet
    # ("- \"Some University\""), which used to become part of the org
    # verbatim (real bug: this produced a stored org of literally
    # "- \"İstanbul Gelişim Üniversitesi" that then got sent to the NIH
    # RePORTER API and 400'd). Strip leading list markers/quotes and
    # trailing quotes before validating.
    text = re.sub(r"^[\s\-\*•>]+", "", text)
    # None of a real answer's characters legitimately include a DOUBLE
    # quote — a stray one is always leftover quoting artifact, so drop
    # those outright. A single quote/apostrophe can be real content
    # ("Xi'an Jiaotong University"), so that one's only trimmed from
    # the ends, not mid-string.
    text = text.replace('"', "").strip("'").strip()
    text = text.rstrip(".")
    if not text or text.lower() in _NON_ANSWERS:
        return None, None
    # A "?" anywhere is the model hedging despite the prompt never
    # offering a hedge shape — it's told to answer exactly "unknown"
    # instead, so a "?" surviving means the rest of the answer is
    # unreliable too.
    if "?" in text:
        return None, None
    if _is_unsubstituted_placeholder(text):
        return None, None
    # Backward-compat: this module used to also identify a specific
    # PI-led lab within the institution ("<University>, <PI> Lab" —
    # dropped, see module docstring for why: the "Stanford, Shenoy Lab"
    # bug that originally motivated this whole module — a paper
    # actually from a Shenzhen group, mislabeled Stanford — is exactly
    # a case where BOTH halves of that old shape could be wrong, the
    # university included, not just the lab). A value in this old shape
    # is ALWAYS treated as needing a genuine fresh resolution — never
    # salvaged by keeping the university half and discarding the lab
    # half, since there's no way to tell from the string alone whether
    # the university part was ever actually correct. An earlier version
    # of this function DID salvage it, on the theory that the
    # institution "was probably fine" — that produced exactly this bug
    # again, just with the tell (", Shenoy Lab") stripped off and the
    # wrong "Stanford" left standing alone, now looking perfectly valid
    # and never up for re-resolution again. Detecting the old shape and
    # rejecting it outright forces a real re-check against actual
    # OpenAlex author-institution data instead.
    if re.match(r"^.+?,\s*.+?\bLab(?:oratory)?\.?$", text, re.IGNORECASE):
        return None, None
    # A real institution/company name is a short phrase, never a
    # sentence the model wrote instead of following the format, and
    # never leading punctuation left over from a malformed attempt.
    if 0 < len(text.split()) <= 8 and "\n" not in text and re.match(r"^[A-Za-z0-9]", text):
        return text, classify_org_type(text)
    return None, None


def normalize_stored_paper_org(
    org: str | None, title: str = "", authors: list[str] | None = None
) -> tuple[str | None, str | None]:
    """Runs an already-stored org value back through the exact same
    parsing/validation real LLM output goes through (_parse_response),
    plus the same title-hallucination check resolve_paper_org_via_search()
    applies to fresh output — there is one set of rules for what a
    valid paper org looks like, applied identically whether the value
    was just generated or has been sitting in the database for months,
    rather than a separately hand-maintained "does this look malformed"
    checklist that can (and did) drift out of sync with the real rules.

    Returns (org, org_type) — possibly the same value back unchanged
    (already valid), possibly a FREE cosmetic fix recovered from the
    stored text itself (a leading "- \"" artifact stripped — no LLM
    call needed, since the real answer was already sitting right
    there), or (None, None) when nothing in the stored text is
    recoverable at all (a placeholder echo, a title-hallucinated name,
    a bare hedge) — the caller's signal that only a genuine fresh
    resolution attempt could possibly do better, not that the row
    should be cleared outright without trying.

    `authors` is optional — pass it so a name that legitimately IS an
    author's own name isn't flagged just because it also happens to
    appear in the title."""
    if not org:
        return None, None
    parsed_org, parsed_type = _parse_response(org)
    if parsed_org:
        authors_institutions = [(name, [], False) for name in (authors or [])]
        if _org_is_title_word(parsed_org, title, authors_institutions):
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


def resolve_paper_org(
    title: str, authors_institutions: list[tuple[str, list[str], bool]], config: Config
) -> tuple[str | None, str | None]:
    """Tier 1 — see module docstring. `authors_institutions` is
    [(author_name, [institution_name, ...], is_corresponding), ...].
    Picks the institution DIRECTLY from this real data — no LLM call,
    no synthesis — preferring, in order: a corresponding author's
    institution (real paper metadata, usually the PI/lab head), then
    the last-listed author's (the common last-author-is-PI convention
    in most STEM fields when no corresponding-author data exists), then
    the first-listed author's, as a descending-confidence fallback.
    Skips a candidate author with no institution data at all rather
    than stopping — a corresponding author OpenAlex has no affiliation
    for shouldn't block falling through to whoever DOES have one.
    `title` is only used for the title-hallucination guard on the
    result (a single-word institution name that happens to also be the
    title's own subject matter); it plays no role in the direct-pick
    logic itself, unlike the old LLM-driven version."""
    if not authors_institutions:
        return None, None
    corresponding = [a for a in authors_institutions if a[2]]
    ordered_candidates = corresponding or (
        [authors_institutions[-1], authors_institutions[0]] if len(authors_institutions) > 1 else authors_institutions
    )
    for _, institutions, _ in ordered_candidates:
        for institution in institutions:
            org = institution.strip()
            if org and not _org_is_title_word(org, title, authors_institutions):
                return org, classify_org_type(org)
    return None, None


def resolve_paper_org_via_search(title: str, config: Config) -> tuple[str | None, str | None]:
    """Tier 2 (fallback) — see module docstring. Only reached when tier 1
    had literally no institution data to work with."""
    if config.summarizer.get("backend", "groq") not in summarizer_mod.LLM_BACKENDS:
        return None, None
    snippets = web_lookup.duckduckgo_search(f'"{title}" university institution')
    if not snippets or not _snippets_mention_title(snippets, title):
        return None, None
    context_block = "Web search snippets about this paper:\n" + "\n".join(f"- {s}" for s in snippets)
    prompt = (
        f'Paper title: "{title}"\n{context_block}\n\n'
        f"Which specific university, research institute, hospital, or company produced this paper? "
        f'Answer with ONLY the institution or company name, nothing else — no explanation, no extra words. '
        f"IMPORTANT: the paper's title often names a technique, architecture, method, or model (e.g. "
        f'"Transformer", "Mamba", "Diffusion") as its SUBJECT MATTER — that is what the paper is ABOUT, never '
        f"the institution's own name. Only use a name explicitly given to you above — never a term pulled from "
        f"the title itself.\n"
        f'Never invent a name you are not reasonably confident in — if you genuinely cannot tell, answer exactly '
        f'"unknown".'
    )
    result = summarizer_mod._llm_generate(prompt, config.summarizer, timeout=60, num_thread=config.summarizer.get("num_thread"))
    if not result:
        return None, None
    # No author data at all reached this tier — there's nothing an
    # overlapping word COULD legitimately be except the title's own
    # subject matter, so no author-name exception here.
    org, org_type = _parse_response(result)
    if org and _org_is_title_word(org, title, []):
        return None, None
    return org, org_type


def resolve_paper_org_with_fallback(
    title: str, authors_institutions: list[tuple[str, list[str], bool]], config: Config
) -> tuple[str | None, str | None]:
    """Both tiers, in order — the one entry point scrapers should call."""
    org, org_type = resolve_paper_org(title, authors_institutions, config)
    if org:
        return org, org_type
    return resolve_paper_org_via_search(title, config)
