"""Enriches papers/news/blog items with structured metadata (org, org
type, modality, therapeutic target, location) and a novelty score — this
is what powers the dashboard's filterable Papers table and Map tab. When
an item reports concrete numbers, it also drafts and auto-appends entries
to devices.yaml/companies.yaml, so those datasets build themselves from
the same pass instead of needing a separate manual step per paper.

Runs automatically after each fetch (bounded by
`enrichment.max_items_per_run` so one fetch cycle can't trigger an
unbounded number of LLM calls), best-effort:

  - No Ollama running / backend "none" → every eligible item is marked
    enriched with "unknown" fields rather than left to retry forever —
    there's nothing more to learn without an LLM.
  - Ollama running but this call fails → the item is left un-enriched
    (no `enriched_at`) so the next fetch retries it instead of silently
    giving up.

`enrich_items()` returns just a count (0 is ambiguous — disabled? nothing
pending? Ollama down?) for backward compatibility; `enrich_items_detailed()`
returns *why*, and is what the CLI and dashboard "Enrich now" button use
so a 0 always comes with an explanation instead of silently doing nothing.

Auto-extraction into devices.yaml/companies.yaml stays auditable rather
than "trust the AI": every auto-added entry is tagged `auto_extracted:
true`, keeps its `source_url` (the actual item, not a homepage) so a
wrong number is easy to spot-check, and is skipped entirely if an entry
for that name+org already exists — it only ever adds new rows, never
silently overwrites one you've corrected.

The vocab for org_type/modality/therapeutic_target is a *suggestion* in
the prompt, not a hard enum — freeform values still get stored, so an
unusual paper isn't forced into the wrong bucket; the dashboard's filter
dropdowns are simply populated from whatever values actually appear.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

from . import web_lookup
from .config import Config, env
from .db import DB
from .feeds_store import load_feeds
from .geocode import geocode
from .trends import company_store
from .trends.company_store import auto_mark_ipo, auto_upsert_company
from .trends.store import auto_append_device
from . import summarizer as summarizer_mod

DEFAULT_SOURCES = ["papers", "news", "blog"]

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    match = _JSON_BLOCK_RE.search(text or "")
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def ollama_status(summarizer_cfg: dict) -> tuple[bool, str]:
    """Quick reachability + model check. Returns (ok, message)."""
    ollama_url = summarizer_cfg.get("ollama_url", "http://localhost:11434")
    model = summarizer_cfg.get("model", "llama3.1")
    try:
        resp = requests.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=5)
        resp.raise_for_status()
    except requests.RequestException as e:
        return False, (
            f"Can't reach Ollama at {ollama_url} ({e}). Install it from https://ollama.ai, "
            f"then run `ollama serve` (or it's already running as a background service on Mac/Windows)."
        )
    tags = resp.json().get("models", [])
    have = {t.get("name", "").split(":")[0] for t in tags}
    if model.split(":")[0] not in have:
        return False, (
            f"Ollama is running, but model {model!r} isn't pulled. Run: ollama pull {model}"
        )
    return True, f"Ollama reachable at {ollama_url}, model {model!r} available."


def _build_prompt(row, config: Config, context: str, author_affiliations: str = "") -> str:
    metric_keys = ", ".join(f"{m['key']} ({m.get('unit', '')})" for m in config.trend_metrics if m.get("type", "numeric") == "numeric")
    categorical_keys = ", ".join(m["key"] for m in config.trend_metrics if m.get("type") == "categorical")

    affiliations_block = (
        f"\nReal author affiliation data (from OpenAlex — factual, not a guess; use it to help identify the org, "
        f"e.g. pair an author's institution here with a lab/PI name mentioned in the item text, but still follow "
        f'the "org" rule below — a bare institution name with no specific lab identified is still null):\n'
        f"{author_affiliations}\n"
        if author_affiliations
        else ""
    )

    return (
        f"Read this {row['source']} item about {config.industry_name}. Extract structured metadata as JSON with "
        "exactly these keys (use null for anything not present or unclear — never invent a number):\n"
        f'  "org": the primary company, lab, or institution the item is about — a specific NAMED organization only, '
        f'e.g. "Neuralink". For academic work, this means the specific LAB or research group — ideally named after '
        f'its lead/PI (e.g. "Poon Lab", "the Shenoy Lab at Stanford", "Smith\'s lab") — NEVER the university alone '
        f'("Stanford University", "MIT" by itself); if the text only names the university with no specific lab or '
        f"lead identifiable, that's null, not the university's name. Use null for anything else too, INCLUDING: "
        f'the general field/industry itself (never "{config.industry_name}" or a synonym for it); a vague group '
        f'description like "Chinese scientists", "researchers", "a team at the university", or "the company"; and '
        f'— a common mistake — the PUBLICATION or news outlet reporting the story (e.g. if the text says '
        f'"according to TechCrunch..." or "36Kr reports that...", that outlet is NOT the org; keep looking for who '
        f"the story is actually about). If the text doesn't name the specific organization, that's null, not your "
        f"best guess at a description of one. This field is a NAME ONLY — a few words, never a sentence, never "
        f"your reasoning about it: if you're unsure, or the org is only implied/mentioned in a roundabout way, "
        f'use null rather than writing out your uncertainty (e.g. never "Unknown (possibly X)" or "X, but the '
        f'text doesn\'t clearly say so" — either commit to the plain name or use null, nothing in between)\n'
        '  "org_description": at most 5 words on what that org is/does, e.g. "BCI implant company" or '
        '"Academic neuroscience lab" — null if "org" is null\n'
        '  "org_type": one of "industry", "academic", "government", "nonprofit", "general", or "unknown". Use '
        '"general" (not "unknown") whenever "org" above is null BECAUSE the item is about the technology/field '
        f'broadly rather than any specific organization — an industry trend piece, a review of {config.industry_name} '
        'progress overall, policy/regulatory coverage not tied to one company, etc. Reserve "unknown" for when a '
        "specific org clearly IS involved but you can't tell what kind of org it is (rare, since org_type is usually "
        'inferable once "org" is non-null) — "unknown" should almost never co-occur with a null "org"\n'
        '  "modality": a JSON ARRAY of the technical approach(es)/category(ies) actually used, e.g. ["EEG"], '
        '["ECoG"], ["CNS implant"], ["PNS implant"], ["EMG"], ["fMRI"], ["fNIRS"], ["eye movement (EM)"], or '
        'another short label if none fit — MULTIPLE entries when the item genuinely combines more than one, e.g. '
        'a study using both EEG and eye-tracking is ["EEG", "eye movement (EM)"], never a single combined string '
        'like "EEG + eye movement (EM)". Make your best-effort call from what the text actually describes (the '
        'device/method used) even if that word never appears verbatim — e.g. "electrodes implanted in the motor '
        'cortex" is "CNS implant" even without that exact phrase. NEVER use "BCI", "brain-computer interface", '
        '"brain-machine interface", or a synonym for the field itself as an entry here — that names the whole '
        f'topic ({config.industry_name}), not a specific technique, so it is true of nearly everything and useless '
        'as a category; name the actual technique(s) instead. Two different "nothing specific" cases, don\'t '
        'conflate them: ["general"] when the item discusses the technology/field broadly, spanning modalities or '
        'not tied to one — e.g. an industry overview, a funding-market roundup, a policy piece — that\'s a genuine '
        'answer, not a gap. ["unknown"] only when the item is clearly about a SPECIFIC technique/device but the '
        "text just never says which one — a real gap, not merely because it isn't spelled out casually (in which "
        "case still make your best-effort call per the instructions above)\n"
        '  "therapeutic_target": what it addresses, e.g. "motor", "speech", "vision", "hearing", "memory", '
        '"mood/psychiatric", "epilepsy", "pain", "other", "general", or "unknown". Same standard as modality — '
        'infer from what\'s described (a paralyzed patient regaining hand control is "motor") rather than requiring '
        'the word itself. "general" when the item is about the technology/field broadly, not addressing any one '
        'condition/target (an industry overview, a funding piece, a policy piece) — that\'s a real answer. '
        '"unknown" only when a specific application IS clearly being discussed but the target genuinely can\'t be '
        "determined from the text — not simply because it isn't spelled out explicitly\n"
        '  "novelty_score": integer 1-5 — how surprising/novel this is COMPARED TO the other recent items on the '
        "same topic listed below (1 = incremental/expected, 5 = a genuine surprise or breakthrough relative to them)\n"
        '  "novelty_rationale": one concise sentence justifying the novelty_score\n'
        '  "location": the city and country of the org\'s relevant site/HQ mentioned or clearly implied by the '
        'text, as "City, Country" (e.g. "San Francisco, USA") — null if not mentioned or you would be guessing\n'
        '  "device_name": the specific product/device name this item is actually about, e.g. "Stentrode", "N1", '
        '"UCSF speech decoder" — null if the item is about the org/company in general rather than one named '
        "device or system (do NOT put the org's own name here as a stand-in — that's what \"org\" is for)\n"
        '  "device_metrics": an object with any of these keys the text reports concrete numbers/values for — '
        f"{metric_keys}, {categorical_keys} — omit keys with no data, use {{}} if none reported. Only meaningful "
        'if "device_name" is set\n'
        '  "company_funding": an object {"funding_total_usd": number or null, "last_funding_round": string or '
        'null, "last_funding_date": "YYYY-MM-DD" or null, "ipo_date": "YYYY-MM-DD" or null, "stock_exchange": '
        'string or null} — funding_total_usd/last_funding_round/last_funding_date if this item reports a specific '
        'funding amount/round for "org"; ipo_date/stock_exchange only if this item reports "org" actually going '
        'public (an IPO that happened or a completed direct listing — e.g. "NASDAQ: XYZ" starts trading), NOT a '
        'mere announcement/rumor of a planned future IPO — use {} if none of this applies\n'
        f"{affiliations_block}\n"
        f"Item title: {row['title']}\nItem text: {(row['summary'] or '')[:1500]}\n\n"
        f"Other recent items on the same topic (for novelty comparison):\n{context}\n\n"
        "Respond with ONLY the JSON object, no other text."
    )


_NON_ORG_EXACT = {
    "unknown", "n/a", "na", "none", "null", "nil", "various", "unspecified", "not specified", "not mentioned",
    "not applicable", "researchers", "scientists", "the researchers", "the scientists", "authors",
    "the authors", "the team", "the company", "the companies", "the university", "the lab", "the labs",
    "investigators", "academics",
}
# Generic media/journal-publisher names common enough across almost any
# science/tech-news feed mix that they're worth rejecting outright,
# rather than relying solely on _known_publisher_names (which only
# catches a hostname you've explicitly configured as a feed — useless
# for a publisher reached via an aggregator/search feed like Google News
# search or NewsAPI, which was never itself configured anywhere) or the
# per-item hostname/domain-shape checks (which miss a publisher whose
# brand name isn't domain-shaped and doesn't match this specific item's
# own url, e.g. a syndicated repost). Not exhaustive — add to this list,
# or better, add the outlet's own feed to `sources.news.feeds`, so
# _known_publisher_names catches its other spellings/variants too.
_KNOWN_MEDIA_OUTLETS = {
    "nature", "science", "cell", "the lancet", "nejm", "pnas",
    "hcplive", "pandaily", "36kr", "cgtn", "tech times", "techtimes",
    "rockefeller university press", "baishideng publishing group",
    "mercator institute for china studies", "the milelion", "milelion", "moomoo",
}
# "<Demonym/adjective> <generic role noun>" — e.g. "Chinese scientists",
# "European researchers". Deliberately doesn't include "lab(s)"/"labs" or
# "institute" etc. in the role-noun list: those are common LEGITIMATE org
# name endings (e.g. "Merge Labs"), unlike "scientists"/"researchers"/
# "team", which are never part of an actual org's name.
_GENERIC_GROUP_RE = re.compile(
    r"^(the\s+)?[A-Za-z]+\s+(scientists|researchers|engineers|team|teams|group|groups|authors|academics|"
    r"investigators|physicians|doctors|clinicians|developers|students|professors)$",
    re.IGNORECASE,
)

# A country is never itself the "specific named organization" an item is
# about — but an LLM extraction sometimes latches onto a country/national
# framing instead of the actual company buried in the text (e.g. a story
# about "Adi Neuroscience" headlined around "India's $1B neurotech fund"
# gets "org" extracted as "India" or "India's Neurotechnology Fund"
# instead). Caught here as a defensive backstop, same idea as
# _is_bare_university: a bare country name, or "<Country>'s ..." (almost
# never how a real org's name starts), is rejected outright.
_COUNTRIES = {
    "afghanistan", "albania", "algeria", "argentina", "armenia", "australia", "austria", "azerbaijan",
    "bahrain", "bangladesh", "belarus", "belgium", "bolivia", "bosnia", "brazil", "bulgaria", "cambodia",
    "cameroon", "canada", "chile", "china", "colombia", "costa rica", "croatia", "cuba", "cyprus",
    "czechia", "czech republic", "denmark", "ecuador", "egypt", "estonia", "ethiopia", "finland", "france",
    "georgia", "germany", "ghana", "greece", "hungary", "iceland", "india", "indonesia", "iran", "iraq",
    "ireland", "israel", "italy", "japan", "jordan", "kazakhstan", "kenya", "kuwait", "latvia", "lebanon",
    "lithuania", "luxembourg", "malaysia", "mexico", "morocco", "myanmar", "nepal", "netherlands",
    "new zealand", "nigeria", "north korea", "norway", "oman", "pakistan", "panama", "peru",
    "philippines", "poland", "portugal", "qatar", "romania", "russia", "saudi arabia", "serbia",
    "singapore", "slovakia", "slovenia", "south africa", "south korea", "spain", "sri lanka", "sweden",
    "switzerland", "syria", "taiwan", "thailand", "tunisia", "turkey", "uae", "ukraine",
    "united arab emirates", "united kingdom", "united states", "uk", "usa", "u.s.", "u.s.a.", "u.k.",
    "uruguay", "venezuela", "vietnam",
}
_COUNTRY_POSSESSIVE_RE = re.compile(
    r"^(" + "|".join(re.escape(c) for c in sorted(_COUNTRIES, key=len, reverse=True)) + r")'s\b",
    re.IGNORECASE,
)


_NON_LOCATION_EXACT = {
    "unknown", "n/a", "na", "none", "unspecified", "not specified", "not mentioned", "not applicable",
    "remote", "global", "worldwide", "international", "online", "virtual", "earth", "various", "multiple",
    "various locations", "multiple locations", "tbd", "n/a, n/a",
}


def _looks_like_a_real_location(location_text: str) -> bool:
    """Same idea as _looks_like_a_named_org: rejects a vague/non-answer
    the LLM handed back instead of null (e.g. "Remote", "Global") before
    it ever reaches geocoding — this is what was landing orgs at
    implausible points (an ocean, a country's random centroid) instead
    of just staying unlocated."""
    normalized = location_text.strip().lower().strip(",. ")
    if not normalized or normalized in _NON_LOCATION_EXACT:
        return False
    # A real "City, Country" (or just a country/region) answer has some
    # alphabetic content; a bare punctuation/number string isn't one.
    if not any(c.isalpha() for c in normalized):
        return False
    return True


def _hostname(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return re.sub(r"^www\.", "", host).split(":")[0]


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _known_publisher_names(config: Config) -> set[str]:
    """Every configured NEWS feed's hostname (e.g. "bioengineer.org") and
    squashed base name (e.g. "techtimes", from "techtimes.com" — so it
    matches the human-written form "Tech Times" too), from both
    config.yaml and anything submitted via the dashboard's Feeds tab.
    News feeds are third-party outlets reporting ON companies, so this
    is a safe list of "definitely not the org" — an LLM extraction
    naming one means it picked up on "according to Tech Times..." and
    named who's reporting the story instead of who it's about.

    Deliberately excludes blog feeds: unlike news, a configured blog is
    routinely a company's OWN blog (e.g. neuralink.com/blog/feed), where
    the feed owner and the story's subject are legitimately the same
    org — applying this exclusion there would wrongly null out a
    correct extraction."""
    names: set[str] = set()
    for feed_url in config.source("news").get("feeds", []):
        host = _hostname(feed_url)
        if host:
            names.add(host)
            names.add(_squash(host.split(".")[0]))
    try:
        submitted = load_feeds(config.feeds_path)
        for entry in submitted.get("news", []):
            host = _hostname(entry.get("url", ""))
            if host:
                names.add(host)
                names.add(_squash(host.split(".")[0]))
    except Exception:
        pass  # feeds.yaml missing/unreadable — just skip this signal, not fatal
    return names


# A bare institution name with no specific lab/center/group named — e.g.
# "Stanford University", "University of Tokyo", "MIT", "Caltech". Not
# rejected if it also names a specific unit (contains "Lab"/"Institute"/
# "Center"/etc, or a possessive — "Poon Lab", "Wu Tsai Neurosciences
# Institute", "Smith's lab at Stanford" all pass through fine).
_BARE_UNIVERSITY_RE = re.compile(
    r"^(the\s+)?[\w&,.\-' ]+?\s(university|college)$"
    r"|^university of [\w&,.\-' ]+$"
    r"|^(mit|caltech|ucla|ucsd|ucsf|ucb)$",
    re.IGNORECASE,
)
_SPECIFIC_UNIT_RE = re.compile(
    r"\b(lab|labs|laboratory|institute|center|centre|group|department|dept|program|initiative)\b",
    re.IGNORECASE,
)


def _is_bare_university(org: str) -> bool:
    """True for a university/college name with nothing more specific
    attached — the point isn't that the university is wrong, it's that
    it's too coarse to be useful as "the org": besseleth should track
    the specific lab or research group (ideally named after its PI/
    lead) doing the actual work, the way it already does for a company.
    Only fires when the string is JUST the institution — any of the
    words above, or a possessive ('X's lab'), means a specific unit was
    already named and this doesn't apply."""
    stripped = org.strip()
    if not _BARE_UNIVERSITY_RE.match(stripped):
        return False
    if _SPECIFIC_UNIT_RE.search(stripped) or "'s" in stripped:
        return False
    return True


_HEDGING_PHRASES = (
    "not a specific", "not specific", "no specific", "didn't mention", "did not mention",
    "doesn't mention", "does not mention", "unspecified", "n/a", "note:", "possibly",
    "according to another", "isn't clear", "is not clear", "unclear from",
)


def _clean_org_value(raw: str | None) -> str | None:
    """A weaker local model doesn't reliably follow "respond with just
    the name" — it sometimes wraps the real answer in commentary instead
    of using null: a trailing parenthetical hedge ("Wu's lab (unspecified
    location, possibly China)"), a garbage prefix wrapping the real
    answer ("Unknown (Poon Lab at UC Berkeley)"), or a full explanatory
    sentence with the actual name tacked on after a colon ("Pandaily
    didn't mention a specific organization, but according to another
    news item: Merge Labs"). This tries to recover the real name from
    each of those shapes; if what's left still reads like hedging prose
    rather than a name (a hedging phrase survives, or it's just too long
    /many words to be a name), returns None rather than guessing which
    part was meant. Runs before _looks_like_a_named_org, so a name it
    recovers still goes through all the normal validity checks after."""
    if not raw:
        return None
    org = raw.strip()

    # "Unknown (Poon Lab at UC Berkeley)" — the real answer is what's
    # wrapped in parens after a non-answer prefix; unwrap it.
    wrapped = re.match(r"^(?:unknown|n/a|none|null)\s*\((.+)\)$", org, re.IGNORECASE)
    if wrapped:
        org = wrapped.group(1).strip()

    # "...but according to another news item: Merge Labs" — take
    # whatever's after the last colon if it's short enough to plausibly
    # be a name on its own, rather than another clause of the sentence.
    if ":" in org:
        tail = org.rsplit(":", 1)[1].strip()
        if 0 < len(tail.split()) <= 6:
            org = tail

    # "Wu's lab (unspecified location, possibly China)" — a trailing
    # parenthetical is usually commentary, not part of a real org's name
    # — EXCEPT "Chang Lab (UCSF)" is a legitimate way of writing
    # "Chang Lab at UCSF"; stripping that blindly would throw the
    # institution away rather than the hedging. Only strip it as
    # commentary when it actually reads like commentary (a hedging
    # phrase, or more than a couple words — a real institution name in
    # parens is short).
    trailing_paren = re.search(r"\s*\(([^)]*)\)\s*$", org)
    if trailing_paren:
        inner = trailing_paren.group(1).strip()
        if any(phrase in inner.lower() for phrase in _HEDGING_PHRASES) or len(inner.split()) > 3:
            org = org[: trailing_paren.start()].strip()

    if not org:
        return None
    lowered = org.lower()
    if any(phrase in lowered for phrase in _HEDGING_PHRASES):
        return None
    # A real org/lab name is a few words, never a full sentence — this
    # catches hedging prose the checks above didn't happen to unwrap.
    if len(org.split()) > 8:
        return None
    return org


def _match_known_lab(text: str, config: Config) -> str | None:
    """Deterministic override using labs.yaml (see labs_store.py) — real
    ground truth you supplied, not an LLM guess. If `text` mentions a
    listed PI's surname, AND (when the entry gives one) their university,
    returns the canonical "<PI> Lab at <University>" form directly;
    checked BEFORE the LLM's own org extraction is used, so a listed lab
    is never subject to however the LLM's phrasing or normalization
    happens to shake out. Requiring the university too (when given) is
    what keeps a common surname from matching every item that happens to
    share it — "Chen" alone proves nothing, "Chen" + "USC" is specific.
    None if nothing in labs.yaml matches (falls through to the LLM's own
    extraction, exactly as before labs.yaml existed)."""
    for lab in config.labs:
        pi = lab["pi"]
        if not pi:
            continue
        surname = pi.split()[-1]
        if not re.search(rf"\b{re.escape(surname)}\b", text, re.IGNORECASE):
            continue
        university = lab["university"]
        if university and not re.search(rf"\b{re.escape(university)}\b", text, re.IGNORECASE):
            continue
        return f"{pi} Lab at {university}" if university else f"{pi} Lab"
    return None


_LOOKS_LIKE_A_DOMAIN_RE = re.compile(
    r"^([a-z0-9][a-z0-9-]*\.)+(com|org|net|io|co|info|biz|news|press|tech|ai)$",
    re.IGNORECASE,
)


def _looks_like_a_named_org(org: str, config: Config) -> bool:
    """False for anything that isn't naming a specific organization: the
    industry name/a keyword verbatim, an explicit non-answer ('unknown',
    'n/a', ...), a vague group description ('Chinese scientists', 'the
    researchers'), one of besseleth's own configured news/blog feed
    sources (e.g. "Tech Times", "36Kr", "bioengineer.org") — those are
    who reported the story, not who it's about — a bare university/
    college name with no specific lab named (see _is_bare_university), or
    a bare domain-shaped string ("bioengineer.org", "techtimes.com").

    That last check matters beyond the configured-feed list above: a news
    item reached via an aggregator/search feed (Google News search,
    NewsAPI) comes from a publisher that was never itself configured
    anywhere, so _known_publisher_names has no way to know it — this
    catches the common failure mode (the LLM naming the outlet, not the
    subject) on shape alone, independent of what's configured. A real
    org's name is essentially never written as a bare "word.tld" string
    in running prose (a company styled "x.ai" gets referred to as "xAI"
    in text, not literally "x.ai"), so the false-positive risk here is
    low. All prompted against directly too (see _build_prompt) — this is
    the defensive backstop for when the LLM ignores that instruction
    anyway."""
    normalized = org.strip().lower()
    if not normalized:
        return False
    non_orgs = (
        _NON_ORG_EXACT | _KNOWN_MEDIA_OUTLETS
        | {config.industry_name.strip().lower()} | {k.strip().lower() for k in config.keywords}
    )
    if normalized in non_orgs or _squash(org) in {_squash(n) for n in _KNOWN_MEDIA_OUTLETS}:
        return False
    if _GENERIC_GROUP_RE.match(org.strip()):
        return False
    if normalized in _COUNTRIES or _COUNTRY_POSSESSIVE_RE.match(org.strip()):
        return False
    if _is_bare_university(org):
        return False
    if _LOOKS_LIKE_A_DOMAIN_RE.match(org.strip()):
        return False
    publisher_names = _known_publisher_names(config)
    if normalized in publisher_names or _squash(org) in publisher_names:
        return False
    return True


# "BCI"/"brain-computer interface" describes the entire field — true of
# nearly every item this tool tracks, so it's useless as a specific
# modality tag (same reasoning as rejecting the industry name/a keyword
# as an "org" above). Checked in addition to config.industry_name/
# keywords (also rejected, dynamically) since these exact phrases are
# the field's name regardless of what a given config calls the industry.
_TOO_BROAD_MODALITY_TERMS = {
    "bci", "bcis", "brain-computer interface", "brain computer interface",
    "brain-machine interface", "brain machine interface", "bmi", "bmis",
}


def _clean_modality_tags(raw, config: Config) -> str:
    """Normalizes the LLM's `modality` response — expected to be a JSON
    array of one tag per distinct technique actually used (see
    _build_prompt) — into a comma-separated string for storage: strips
    each tag, drops empties and duplicates, and drops anything that's
    just the field's own name (see _TOO_BROAD_MODALITY_TERMS above) or
    this config's own industry name. Deliberately does NOT reject a
    configured keyword in general the way "org" does: `industry.keywords`
    routinely includes specific modality names themselves (EEG, ECoG,
    TMS, DBS, ...), so those need to survive as valid tags here, unlike
    an org name (which should never equal a search keyword). Also
    accepts a bare string for backward compatibility with an older
    single-value response shape. Returns "unknown" if nothing survives."""
    if isinstance(raw, str):
        raw = [raw]
    elif not isinstance(raw, list):
        raw = []

    too_broad = _TOO_BROAD_MODALITY_TERMS | {config.industry_name.strip().lower()}

    tags: list[str] = []
    for tag in raw:
        if not isinstance(tag, str):
            continue
        tag = tag.strip()
        if not tag or tag.lower() in too_broad:
            continue
        if tag not in tags:
            tags.append(tag)

    return ", ".join(tags) if tags else "unknown"


_LAB_NAME_RE = re.compile(
    r"^(?:the\s+)?(?P<pi>[A-Za-z][\w-]*)(?:'s)?\s+lab(?:oratory)?"
    r"(?:\s*(?:at|@|,|\()\s*(?P<inst>[^)]+?)\)?)?$",
    re.IGNORECASE,
)


def _normalize_lab_name(org: str) -> str:
    """Collapses the handful of ways a PI-named lab gets phrased — "the
    Shenoy Lab at Stanford", "Shenoy's lab at Stanford", "Shenoy Lab",
    "the Shenoy Laboratory", "Shenoy Lab (Stanford)", "Shenoy Lab,
    Stanford" — into one consistent "<PI> Lab[ at <institution>]" form,
    so the same lab doesn't fork into multiple Orgs-table rows just
    because the LLM (or the source text) phrased it differently from one
    item to the next. A no-op (returns `org` unchanged) for anything
    that doesn't match this specific shape — never guesses at a name it
    isn't confident is a PI-named lab."""
    match = _LAB_NAME_RE.match(org.strip())
    if not match:
        return org
    pi = match.group("pi").strip()
    if pi.islower() or pi.isupper():
        pi = pi.capitalize()  # leaves mixed-case names ("McCarthy") alone
    inst = re.sub(r"\s+", " ", (match.group("inst") or "").strip()).rstrip(".")
    canonical = f"{pi} Lab"
    if inst:
        canonical += f" at {inst}"
    return canonical


def _canonicalize_new_org(org: str, db: DB) -> str:
    """Normalizes lab-name phrasing first (see _normalize_lab_name), then:
    if an org that's letters/digits-equivalent to the result (ignoring
    case, spacing, punctuation) is already stored under different
    casing/spacing, reuses that exact existing spelling instead of
    adding a near-duplicate ("Ability Neurotech" vs "Ability NeuroTech"
    from two separate LLM calls, which otherwise show up as two
    different Orgs-table rows). Whichever spelling was seen first wins
    and stays canonical going forward; a genuinely new lab is stored
    already in its normalized form rather than however this one mention
    happened to phrase it."""
    org = _normalize_lab_name(org)
    target = _squash(org)
    if not target:
        return org
    for existing in db.distinct_orgs():
        if _squash(existing) == target:
            return existing
    return org


def _self_referential_org_ids(db: DB) -> list[str]:
    """Retroactive counterpart to the self-referential-hostname check in
    _enrich_one (see its comment): item ids whose stored `org` squashes to
    the same base name as their OWN url's hostname — org="36Kr" on an
    item from 36kr.com, say. Unlike the industry-name/domain-shape checks
    in _looks_like_a_named_org, this can't be swept by org name alone
    (the same org string could be legitimate on a different item), so it
    has to look at each item's own url."""
    ids = []
    for row in db.items_with_org():
        host_base = _hostname(row["url"] or "").split(".")[0]
        if host_base and _squash(row["org"]) == _squash(host_base):
            ids.append(row["id"])
    return ids


def _canonicalize_existing_orgs(db: DB) -> int:
    """Retroactive sweep: clusters every currently-stored org by the same
    normalize-then-squash equivalence as _canonicalize_new_org() — so
    "the Shenoy Lab at Stanford" and "Shenoy's lab at Stanford" land in
    the same cluster even though they're not letters/digits-equivalent —
    and renames every variant in a cluster to the normalized form of
    whichever spelling has the most items (a tiebreak that's stable and
    doesn't need any judgment call). Returns how many rows were renamed."""
    counts = db.org_item_counts()
    clusters: dict[str, list[str]] = {}
    for org in counts:
        clusters.setdefault(_squash(_normalize_lab_name(org)), []).append(org)

    renamed = 0
    for variants in clusters.values():
        if len(variants) < 2:
            continue
        # Prefer the most-used spelling, but always collapse its own
        # whitespace to single spaces — a tie between "Ability Neurotech"
        # and "Ability  NeuroTech" (double space) shouldn't crown the
        # double-space one just because it happened to sort higher — then
        # normalize it, so the cluster settles on a clean "<PI> Lab at
        # <institution>" form regardless of which raw phrasing had the
        # most items.
        winner = re.sub(r"\s+", " ", max(variants, key=lambda o: counts[o])).strip()
        canonical = _normalize_lab_name(winner)
        for variant in variants:
            if variant != canonical:
                renamed += db.rename_org(variant, canonical)
    return renamed


_ARXIV_ID_RE = re.compile(r"arxiv\.org/abs/([\w.\-/]+?)(?:v\d+)?/?$", re.IGNORECASE)


def _arxiv_id_from_url(url: str) -> str | None:
    match = _ARXIV_ID_RE.search(url or "")
    return match.group(1) if match else None


def _author_affiliations_block(row) -> str:
    """Real author-institution data for an arXiv item (see
    web_lookup.lookup_arxiv_authorships's docstring for why this is a
    lookup, not something left to the LLM to recall) — "" for a non-
    arXiv item, a paper OpenAlex doesn't have, or on any lookup failure,
    so this is always safe to splice into the prompt unconditionally.
    Gated on the URL being an arxiv.org one (not row["source"], which is
    "papers" for both arXiv and OpenAlex-sourced items now — see
    pipeline.py) since that's what actually determines whether an arXiv
    id can even be extracted; a non-arXiv "papers" item's url just won't
    match and this returns "" the same way."""
    arxiv_id = _arxiv_id_from_url(row["url"] or "")
    if not arxiv_id:
        return ""
    authorships = web_lookup.lookup_arxiv_authorships(arxiv_id)
    if not authorships:
        return ""
    lines = [
        f"- {name}: {', '.join(institutions)}" if institutions else f"- {name}: (institution not on record)"
        for name, institutions in authorships
    ]
    return "\n".join(lines)


def _enrich_one(row, db: DB, config: Config, summarizer_cfg: dict) -> bool:
    """Returns True if enrichment was saved (success or graceful
    'unknown' fallback), False if it should be retried next time."""
    context_rows = db.recent_items_for_context(
        row["source"], (row["matched_keywords"] or "").split(","), exclude_id=row["id"]
    )
    context = "\n".join(f"- {r['title']}: {(r['summary'] or '')[:200]}" for r in context_rows) or "(no similar recent items yet)"
    author_affiliations = _author_affiliations_block(row)

    prompt = _build_prompt(row, config, context, author_affiliations)
    result = summarizer_mod._ollama_generate(
        prompt,
        summarizer_cfg.get("ollama_url", "http://localhost:11434"),
        summarizer_cfg.get("model", "llama3.1"),
        timeout=60,
        num_thread=summarizer_cfg.get("num_thread"),
    )
    if result is None:
        return False  # Ollama unreachable — retry next time

    data = _extract_json(result) or {}
    novelty = data.get("novelty_score")
    try:
        novelty = int(novelty) if novelty is not None else None
        if novelty is not None and not (1 <= novelty <= 5):
            novelty = None
    except (TypeError, ValueError):
        novelty = None

    modality = _clean_modality_tags(data.get("modality"), config)

    org = _clean_org_value(data.get("org"))
    if org and not _looks_like_a_named_org(org, config):
        org = None
    if org and _squash(org) == _squash(_hostname(row["url"] or "").split(".")[0]):
        # The org the LLM named squashes to the same base name as the
        # item's OWN url's hostname — e.g. org="36Kr" on an item from
        # 36kr.com. This is the same "who reported it, not who it's
        # about" mistake _known_publisher_names guards against, but
        # catches it for a publisher reached via an aggregator/search
        # feed (Google News search, NewsAPI) that was never itself
        # configured anywhere, so that list has no way to know about it.
        org = None
    known_lab = _match_known_lab(f"{row['title']} {row['summary'] or ''}", config)
    if known_lab:
        org = known_lab  # real ground truth (labs.yaml) wins over the LLM's own extraction
    if org:
        org = _canonicalize_new_org(org, db)
    org_description = (data.get("org_description") or "").strip() or None
    if org_description and org:
        words = org_description.split()
        if len(words) > 5:
            org_description = " ".join(words[:5])
    elif not org:
        org_description = None
    location_text = data.get("location") or None
    if location_text and not _looks_like_a_real_location(location_text):
        location_text = None
    lat = lon = None
    if location_text:
        coords = geocode(location_text)
        if coords:
            lat, lon = coords

    db.save_enrichment(
        row["id"],
        org=org,
        org_type=data.get("org_type") or "unknown",
        modality=modality,
        therapeutic_target=data.get("therapeutic_target") or "unknown",
        novelty_score=novelty,
        novelty_rationale=data.get("novelty_rationale") or None,
        location_text=location_text,
        lat=lat,
        lon=lon,
        org_description=org_description,
    )

    # Fold concrete numbers into the devices/companies tables — additive
    # only, never overwrites an existing entry (see trends/store.py's
    # docstring). Requires an actual named device — "org" alone (e.g. the
    # LLM defaulting to just the company name when it can't name a
    # specific product) isn't a device, and used to silently create a
    # device row with the org's own name, cluttering the FDA timeline
    # with entries that were never really about a device.
    device_name = (data.get("device_name") or "").strip()
    device_metrics = data.get("device_metrics") or {}
    if org and device_name and device_name.lower() != org.lower():
        auto_append_device(
            config.devices_path,
            name=device_name,
            org=org,
            org_type=data.get("org_type") or "unknown",
            fda_status=device_metrics.get("fda_status", "unknown"),
            metrics={k: v for k, v in device_metrics.items() if k not in ("fda_status",)},
            source_url=row["url"] or "",
            date_reported=(row["published_at"] or "")[:10],
        )

    funding = data.get("company_funding") or {}
    if org and funding.get("funding_total_usd"):
        auto_upsert_company(
            config.companies_path,
            name=org,
            funding_total_usd=funding.get("funding_total_usd"),
            last_funding_round=funding.get("last_funding_round") or "",
            # Fall back to the item's own published date when the LLM
            # didn't pin an exact funding date — "reported around this
            # date" beats leaving it blank, which used to mean a company
            # with real funding data just never appeared on the Trends
            # tab's date-axis chart (it had table entries but no point to
            # plot).
            last_funding_date=funding.get("last_funding_date") or (row["published_at"] or "")[:10],
            source_url=row["url"] or "",
        )
    if org and funding.get("ipo_date"):
        auto_mark_ipo(
            config.companies_path,
            name=org,
            ipo_date=funding["ipo_date"],
            stock_exchange=funding.get("stock_exchange") or "",
        )

    return True


def _search_org_location(org: str, summarizer_cfg: dict) -> tuple[str, float, float] | None:
    """Tier 2: a general web search (DuckDuckGo) plus the local LLM to
    read the results, for an org Wikidata doesn't know about — covers
    the small/early-stage companies tier 1 misses, at the cost of an
    LLM call, so this only runs after that one comes back empty.
    Requires Ollama; returns None on any failure at any step (no
    results, Ollama unreachable, the model saying it can't tell, or a
    location that fails the same validity check as the LLM's own item-
    level extraction)."""
    if summarizer_cfg.get("backend") != "ollama":
        return None
    snippets = web_lookup.duckduckgo_search(f"{org} headquarters location city")
    if not snippets:
        return None

    prompt = (
        f'Based on these web search result snippets, what city and country is "{org}"\'s headquarters or main '
        f'office in? Respond with ONLY "City, Country" (e.g. "San Francisco, USA"), or exactly "unknown" if the '
        f"snippets don't make it clear — never guess.\n\nSnippets:\n" + "\n".join(f"- {s}" for s in snippets)
    )
    result = summarizer_mod._ollama_generate(
        prompt,
        summarizer_cfg.get("ollama_url", "http://localhost:11434"),
        summarizer_cfg.get("model", "llama3.1"),
        timeout=30,
        num_thread=summarizer_cfg.get("num_thread"),
    )
    if not result:
        return None

    location_text = result.strip().strip('"')
    if not _looks_like_a_real_location(location_text):
        return None
    coords = geocode(location_text)
    if not coords:
        return None
    return (location_text, *coords)


def _backfill_org_locations(config: Config, db: DB, max_lookups_override: int | None = None) -> int:
    """Fills in a missing location for orgs that have none, independent
    of the LLM pass above (that one only ever knows what a given item's
    own text says, so an org whose location was never mentioned in any
    item stays unlocated forever without this): tries a free Wikidata/
    Wikipedia lookup first, then a general web search read by the local
    LLM if that comes back empty — see web_lookup.py's docstring for why
    in that order. Bounded per run (enrichment.max_org_lookups_per_run,
    shared across both tiers) and cached — found or not — so a miss
    isn't re-queried every run; a cached hit is reapplied for free if a
    newer item for the same org shows up without its own location.
    Returns how many orgs got newly filled in."""
    cfg = config.raw.get("enrichment", {}) or {}
    max_lookups = max_lookups_override if max_lookups_override is not None else cfg.get("max_org_lookups_per_run", 8)
    recheck_days = cfg.get("location_recheck_days", 30)
    if max_lookups <= 0:
        return 0

    filled = 0
    attempted = 0
    for org in db.orgs_missing_location():
        cached = db.get_org_location_cache(org)
        if cached and cached["found"]:
            # Already know this one — reapply from cache, free (no web call,
            # doesn't count against this run's lookup budget).
            db.set_org_location(org, cached["location_text"], cached["lat"], cached["lon"])
            filled += 1
            continue
        if cached and not cached["found"]:
            checked_at = datetime.fromisoformat(cached["checked_at"])
            if datetime.now(timezone.utc) - checked_at < timedelta(days=recheck_days):
                continue  # checked recently, nothing found — don't re-probe yet

        if attempted >= max_lookups:
            continue
        attempted += 1
        result = web_lookup.lookup_org_location(org) or _search_org_location(org, config.summarizer)
        if result:
            label, lat, lon = result
            db.set_org_location(org, label, lat, lon)
            db.set_org_location_cache(org, found=True, location_text=label, lat=lat, lon=lon)
            filled += 1
        elif config.summarizer.get("backend") == "ollama":
            # Only cache a miss once tier 3 (the LLM-read web search) got
            # a genuine shot — with backend != "ollama", _search_org_location
            # returns None immediately without trying, so caching that as
            # "not found" would wrongly lock the org out of a real check
            # for location_recheck_days once Ollama is actually available.
            db.set_org_location_cache(org, found=False)

    return filled


def _backfill_contact_locations(config: Config, db: DB, max_lookups_override: int | None = None) -> int:
    """Same free Wikidata/Wikipedia (then web-search+LLM) lookup as
    _backfill_org_locations, but for your contacts' current employers —
    powers the Map tab's "friends" layer, which needs a location for a
    company even if that company was never mentioned in any scraped item
    (so db.orgs_missing_location() alone wouldn't ever surface it).
    Shares the same org_location_cache table (and lookup budget) as org
    backfill — a company that's both a contact's employer and a source
    org only ever gets looked up once. Returns how many got newly filled."""
    cfg = config.raw.get("enrichment", {}) or {}
    max_lookups = max_lookups_override if max_lookups_override is not None else cfg.get("max_org_lookups_per_run", 8)
    if max_lookups <= 0:
        return 0

    # "Current" employer = the first workplace listed (see contacts_store's
    # Contact docstring — order is up to you, current-first if it matters).
    orgs = set()
    for contact in config.contacts:
        workplaces = contact.get("workplaces") or []
        if workplaces and workplaces[0].get("company"):
            orgs.add(workplaces[0]["company"])

    filled = 0
    attempted = 0
    for org in orgs:
        cached = db.get_org_location_cache(org)
        if cached:
            if cached["found"]:
                filled += 1
            continue  # already resolved (or already tried and came up empty) — cache handles recheck
        if attempted >= max_lookups:
            continue
        attempted += 1
        result = web_lookup.lookup_org_location(org) or _search_org_location(org, config.summarizer)
        if result:
            label, lat, lon = result
            db.set_org_location_cache(org, found=True, location_text=label, lat=lat, lon=lon)
            filled += 1
        elif config.summarizer.get("backend") == "ollama":
            db.set_org_location_cache(org, found=False)  # see _backfill_org_locations's comment on this condition

    return filled


def _standardize_location_names(db: DB) -> int:
    """Different items naming the same real-world place at different
    levels of detail — "Mountain View", "Mountain View, CA", "Mountain
    View, California, United States" — all geocode to the same (or a
    near-identical) point, but stayed as different-looking rows/markers
    since nothing reconciled the TEXT, only the coordinates came from
    geocoding. Clusters items by lat/lon rounded to 2 decimal places
    (~1km — same city, not just same country) and snaps every variant in
    a cluster to whichever (text, lat, lon) is most common, the same
    "most-used spelling wins" idiom _canonicalize_existing_orgs uses for
    org names. lat/lon only ever gets replaced by another item's own
    already-geocoded point within that ~1km cluster (never guessed), so
    this stays safe to run unattended — worst case it nudges a marker by
    under a kilometer to match its neighbors, never invents a location.
    Returns how many items were changed. Runs BEFORE
    _apply_location_consensus so that sweep isn't fooled into seeing
    "one org, several disagreeing locations" when it's really "one
    location, several spellings/jittered coordinates"."""
    clusters: dict[tuple[float, float], list[sqlite3.Row]] = {}
    for row in db.location_text_variants():
        key = (round(row["lat"], 2), round(row["lon"], 2))
        clusters.setdefault(key, []).append(row)

    renamed = 0
    for variants in clusters.values():
        if len(variants) < 2:
            continue
        canonical = max(variants, key=lambda r: r["n"])
        for v in variants:
            if (v["location_text"], v["lat"], v["lon"]) == (canonical["location_text"], canonical["lat"], canonical["lon"]):
                continue
            renamed += db.standardize_location(
                v["location_text"], v["lat"], v["lon"], canonical["location_text"], canonical["lat"], canonical["lon"]
            )
    return renamed


def _apply_location_consensus(db: DB) -> int:
    """Each item gets its location guessed independently (from its own
    text, by whichever LLM call enriched it) — so one org's items can
    end up disagreeing, e.g. 10 items correctly say San Francisco and 1
    misreads something as Brazil. db.locations() (the Map tab's data
    source) groups by (org, lat, lon), so disagreement literally means
    that org gets plotted as two markers instead of one. This applies
    the majority location to every item for an org whenever there IS a
    clear majority — a tie is left alone rather than guessing which
    side is right. Returns how many orgs were reconciled."""
    reconciled = 0
    for org in db.distinct_orgs():
        votes = db.org_location_votes(org)
        if len(votes) < 2:
            continue  # already unanimous, or unlocated — nothing to reconcile
        top, runner_up = votes[0], votes[1]
        if top["n"] <= runner_up["n"]:
            continue  # no strict majority — don't guess which is right
        db.set_org_location_all(org, top["location_text"], top["lat"], top["lon"])
        reconciled += 1
    return reconciled


def _sync_duplicate_novelty(config: Config, db: DB) -> int:
    """Multiple rows for the same story are expected — the same news
    inevitably reaches besseleth via more than one feed — and this
    deliberately does NOT merge or drop any of them (that's a separate,
    report-time-only concern — see dedupe.merge_near_duplicates(), used
    when rendering the weekly report, not here). What's wrong is each
    row getting independently novelty-scored: 'how surprising compared
    to other recent items' depends on exactly which other items happened
    to be in context at that moment, so two rows for one story can end
    up with two different scores. This groups recent items by the same
    near-duplicate title match, and — within a group — copies whichever
    novelty_score/novelty_rationale is already set onto every other
    member so they read the same, regardless of source. Returns how
    many rows got a score synced onto them."""
    from .dedupe import group_near_duplicates

    cfg = config.raw.get("enrichment", {}) or {}
    sources = cfg.get("sources", DEFAULT_SOURCES)
    items = db.recent_items_for_dedupe(sources)
    if len(items) < 2:
        return 0

    synced = 0
    for group in group_near_duplicates(items):
        if len(group) < 2:
            continue
        canonical = next((i for i in group if i.novelty_score is not None), None)
        if canonical is None:
            continue  # nobody in this group has been scored yet — nothing to sync
        for item in group:
            if item.id == canonical.id:
                continue
            if item.novelty_score != canonical.novelty_score or item.novelty_rationale != canonical.novelty_rationale:
                db.sync_novelty(item.id, canonical.novelty_score, canonical.novelty_rationale)
                synced += 1
    return synced


def enrich_items_detailed(
    config: Config, db: DB, force: bool = False, run_until_done: bool = False, background: bool = False,
    progress_cb=None,
) -> dict:
    """Returns {"processed": int, "message": str, "backend": str} — the
    message always explains a 0, so 'nothing happened' is never silent:
    enrichment disabled in config, nothing left to enrich (already
    caught up), no LLM configured (marked unknown instead), or Ollama
    unreachable (left pending — will retry once it's back).

    Modes:
      - default, interactive (force=False, run_until_done=False,
        background=False — the dashboard's "Enrich now" and a plain
        `cli enrich`): everything unenriched from the last
        `enrichment.default_days_back` days (default 14), no count cap
        — this is what a normal fetch/paste cycle leaves behind, so
        there's no need to cap it; an older backlog beyond that window
        is left alone rather than crowding out what you actually just
        added.
      - background=True (the automatic post-fetch enrichment step —
        `enrich_items()`, run unattended every fetch): the original
        behavior, capped at `enrichment.max_items_per_run` with no day
        window — deliberately conservative since this runs on its own,
        possibly on a schedule, with nobody watching CPU load.
      - run_until_done=True: ignores the day window and the per-run cap
        — keeps pulling and enriching batch after batch (still
        `enrichment.max_items_per_run` at a time) until the never-
        enriched queue is genuinely empty, however old. For "kick this
        off and walk away, come back to the whole backlog filled in"
        rather than clicking Enrich repeatedly.
      - force=True: re-checks items that already have enrichment too
        (oldest-checked first), instead of only ones that have never
        been enriched — for catching up already-stored data after
        enrich.py's extraction logic improves (a new validity rule, a
        prompt change, ...). Ignores the day window (there's no "recent"
        backlog here, only "all of it"); without run_until_done, cycles
        one capped batch per call, since that queue never runs dry on
        its own (the batch you just re-checked is simply the newest-
        checked now, not gone). Combined with run_until_done, keeps
        going until it's cycled through every item currently in
        `enrichment.sources` once — bounded by a count taken at the
        start, not "until empty" (which would never arrive) — so
        "re-check everything" actually means everything, not one batch."""
    cfg = config.raw.get("enrichment", {}) or {}
    summarizer_cfg = config.summarizer
    backend = summarizer_cfg.get("backend", "none")

    if not cfg.get("enabled", True):
        return {"processed": 0, "message": "enrichment.enabled is false in config.yaml — nothing to do.", "backend": backend}

    # Rows for the same story across multiple feeds are expected and
    # stay separate — this only makes sure they agree on novelty. See
    # _sync_duplicate_novelty()'s docstring.
    synced = _sync_duplicate_novelty(config, db)
    if synced:
        print(f"[enrich] Synced novelty score across {synced} duplicate item(s) of the same story.")

    # Self-healing cleanup for items enriched before this guard existed
    # (or before it covered vague-group phrasing like "Chinese scientists")
    # — sweeps every org value currently stored against the same check a
    # fresh enrichment applies. Cheap (one query for the distinct list,
    # then an indexed exact-match update), safe to run every call.
    #
    # Also retroactively applies _clean_org_value (existing rows saved
    # before that existed can still have the raw hedging-prose org value
    # verbatim): a value it can't recover a name from joins invalid_orgs
    # below (nulled); one it rewrites to something shorter gets renamed
    # to the cleaned form instead of nulled.
    existing_orgs = db.distinct_orgs()
    invalid_orgs = []
    for o in existing_orgs:
        cleaned = _clean_org_value(o)
        if cleaned is None:
            invalid_orgs.append(o)
        elif cleaned != o:
            db.rename_org(o, cleaned)
        elif not _looks_like_a_named_org(o, config):
            invalid_orgs.append(o)
    cleared = db.clear_org_matches(invalid_orgs)
    if cleared:
        print(f"[enrich] Cleared {cleared} item(s) whose 'org' was actually the industry name/a keyword (or unrecoverable hedging text).")

    self_referential = _self_referential_org_ids(db)
    cleared_self_ref = db.clear_org_matches_by_id(self_referential)
    if cleared_self_ref:
        print(f"[enrich] Cleared {cleared_self_ref} item(s) whose 'org' was actually who reported the story (its own url's publisher).")

    renamed = _canonicalize_existing_orgs(db)
    if renamed:
        print(f"[enrich] Merged {renamed} item(s) into an existing org's canonical spelling (casing/spacing variants).")

    # A device row that's just the org's own name (an older bug — see
    # _enrich_one's device_name gate) never belonged on the FDA timeline.
    # Safe to auto-remove: unlike a company-name typo, "name == org" is
    # unambiguous, no judgment call involved.
    bogus_devices = db.delete_bogus_devices()
    if bogus_devices:
        print(f"[enrich] Removed {bogus_devices} device row(s) whose 'name' was just the org's own name.")

    # NOT auto-merged, deliberately: two similar company names ("Precision
    # Neuroscience" vs. "Precision Neurotech") are just as often two real
    # companies as one typo'd twice, and a wrong auto-merge would silently
    # fold one company's data into another's. This only flags candidates
    # for a human to confirm — see find_possible_duplicate_companies's
    # docstring — surfaced in the dashboard's Trends tab, never merged here.
    possible_dupes = company_store.find_possible_duplicate_companies(config.companies_path)
    if possible_dupes:
        print(f"[enrich] {len(possible_dupes)} possible duplicate company pair(s) flagged for review in the dashboard.")

    dated_companies = company_store.backfill_missing_funding_dates(config.companies_path)
    if dated_companies:
        print(f"[enrich] Backfilled a funding date for {dated_companies} compan(ies) that had funding data but no date.")

    # One-time recovery for negative org_location_cache rows written while
    # summarizer.backend wasn't "ollama" (tier 3 never actually ran then,
    # so "not found" wasn't a real answer — see the comment where this is
    # now guarded against in _backfill_org_locations). Gated on a meta
    # flag so this only ever runs once, not every enrich call.
    if config.summarizer.get("backend") == "ollama" and not db.get_meta("location_cache_backend_fix_applied"):
        reset = db.clear_negative_location_cache()
        db.set_meta("location_cache_backend_fix_applied", "1")
        if reset:
            print(f"[enrich] Reset {reset} cached 'not found' location(s) so they get a real check now that Ollama is available.")

    invalid_locations = [loc for loc in db.distinct_locations() if not _looks_like_a_real_location(loc)]
    locations_cleared = db.clear_location_matches(invalid_locations)
    if locations_cleared:
        print(f"[enrich] Cleared {locations_cleared} item(s) with a vague location guess (e.g. 'Remote'/'Global').")

    standardized_locations = _standardize_location_names(db)
    if standardized_locations:
        print(f"[enrich] Standardized {standardized_locations} item(s)' location label to match others at the same place.")

    reconciled_locations = _apply_location_consensus(db)
    if reconciled_locations:
        print(f"[enrich] Reconciled {reconciled_locations} org(s) whose items disagreed on location to the majority guess.")

    # Independent of the LLM pass below — a free web lookup (Wikidata)
    # for orgs whose location was never mentioned in any item's own
    # text, so those don't just stay unlocated forever. The per-run
    # budget (enrichment.max_org_lookups_per_run, default 8) is sized for
    # a normal interactive click, nowhere near enough to work through
    # hundreds of orgs in one go — raised for run_until_done (since
    # "enrich everything" implies "look up everything you can too," not
    # just the item-extraction pass), but configurable and modest by
    # default: each lookup is a web request (+ an LLM call for tier 3),
    # and on a machine already tight on RAM (Ollama holding a model
    # resident, a browser open) hundreds of them back-to-back is real
    # sustained load, not free just because no single one is large.
    # Lower enrichment.run_until_done_location_lookup_cap if this run is
    # too heavy for your machine.
    location_lookup_cap = cfg.get("run_until_done_location_lookup_cap", 50) if run_until_done else None
    locations_filled = _backfill_org_locations(config, db, max_lookups_override=location_lookup_cap)
    contact_locations_filled = _backfill_contact_locations(config, db, max_lookups_override=location_lookup_cap)
    location_note = (
        f" Filled in a location for {locations_filled} org(s) via web lookup." if locations_filled else ""
    )
    if contact_locations_filled:
        location_note += f" Filled in a location for {contact_locations_filled} contact employer(s)."

    sources = cfg.get("sources", DEFAULT_SOURCES)
    max_items = cfg.get("max_items_per_run", 20)
    default_days_back = cfg.get("default_days_back", 14)
    keep_going = run_until_done
    # Only meaningful for force+run_until_done — a one-time snapshot of
    # how many items exist right now, so that combo means "one full pass
    # over everything currently stored," not "forever" (the re-check
    # queue has no natural empty state to stop at on its own).
    force_pool_size = db.count_items(sources) if (force and run_until_done) else None

    if force:
        first_rows = db.items_to_reenrich(sources, max_items)
    elif run_until_done or background:
        first_rows = db.unenriched_items(sources, max_items)  # batched below — the whole backlog, any age
    else:
        # The common interactive case: whatever's unenriched from normal
        # recent use (a fetch or two, a few pastes), not a years-old
        # backlog — so no count cap, just a time window
        # (enrichment.default_days_back).
        first_rows = db.unenriched_items(sources, None, days_back=default_days_back)
    if not first_rows:
        message = (
            f"Nothing to re-enrich — enrichment.sources is empty.{location_note}" if force else
            f"Nothing to enrich — every item in enrichment.sources is already tagged (or unknown).{location_note}"
        )
        return {
            "processed": 0,
            "message": message,
            "backend": backend,
        }

    if backend != "ollama":
        rows = first_rows
        for row in rows:
            db.save_enrichment(
                row["id"], org=None, org_type="unknown", modality="unknown",
                therapeutic_target="unknown", novelty_score=None, novelty_rationale=None,
            )
        msg = (
            f"summarizer.backend is {backend!r}, not 'ollama' — marked {len(rows)} item(s) 'unknown' rather than "
            f"leaving them pending. Set summarizer.backend: \"ollama\" in config.yaml and have Ollama running to "
            f"actually extract org/modality/location/etc.{location_note}"
        )
        print(f"[enrich] {msg}")
        return {"processed": len(rows), "message": msg, "backend": backend}

    ok, status_msg = ollama_status(summarizer_cfg)
    if not ok:
        status_msg += location_note
        print(f"[enrich] {status_msg}")
        return {"processed": 0, "message": status_msg, "backend": backend}

    pause_seconds = cfg.get("pause_seconds", 0)

    total_processed = 0
    total_attempted = 0
    work_seconds = 0.0  # excludes the deliberate pause_seconds sleeps — this is actual enrich time, not throttling
    rows = first_rows
    # A very basic total estimate — exact for force+run_until_done (a
    # snapshot count taken above) and for the plain interactive/background
    # cases (the whole queue is already in `first_rows`); for plain
    # run_until_done (no force) it's just this first batch, since the real
    # backlog size isn't known until it runs dry — so the bar undershoots a
    # bit there rather than promising a total it can't back up.
    progress_total = force_pool_size if force_pool_size is not None else len(first_rows)
    while rows:
        total_attempted += len(rows)
        batch_processed = 0
        for i, row in enumerate(rows):
            if progress_cb:
                progress_cb(f"Enriching item {total_attempted - len(rows) + i + 1}", total_attempted - len(rows) + i + 1, progress_total)
            item_start = time.time()
            try:
                if _enrich_one(row, db, config, summarizer_cfg):
                    total_processed += 1
                    batch_processed += 1
            except Exception as e:
                print(f"[enrich] Failed on item {row['id']}: {e}")
            work_seconds += time.time() - item_start
            # Gives the CPU a breather between LLM calls instead of hammering
            # it back-to-back for the whole batch — set enrichment.pause_seconds
            # in config.yaml if enrich runs are making the machine unusable.
            # Skipped after the last item so it doesn't delay returning.
            if pause_seconds and i < len(rows) - 1:
                time.sleep(pause_seconds)

        if not keep_going:
            break
        if not batch_processed:
            # Nothing in this batch actually got enriched (Ollama up, but
            # every item errored/timed out) — the next batch would just
            # hand back this same stuck item(s), so stop instead of
            # spinning. Whatever succeeded elsewhere is still kept.
            print(f"[enrich] Stopping: {len(rows)} item(s) in the last batch made no progress (see errors above).")
            break
        if force_pool_size is not None and total_attempted >= force_pool_size:
            print(f"[enrich] Stopping: completed one full pass over all {force_pool_size} item(s) in scope.")
            break
        rows = db.items_to_reenrich(sources, max_items) if force else db.unenriched_items(sources, max_items)
        if rows and force_pool_size is None:
            # Growing backlog (plain run_until_done) — extend the estimate
            # rather than let progress "overshoot" past a too-small total.
            progress_total = total_attempted + len(rows)
        if rows:
            print(f"[enrich] Batch done ({total_processed} so far) — {len(rows)}+ item(s) left, continuing...")
            ok, status_msg = ollama_status(summarizer_cfg)
            if not ok:
                print(f"[enrich] Stopping: {status_msg}")
                break

    if total_processed:
        db.record_enrich_run(total_processed, work_seconds)
    stats = db.get_enrich_stats()

    if total_processed < total_attempted:
        message = f"Enriched {total_processed}/{total_attempted} — the rest failed mid-call and will retry next run (see server log)."
    else:
        message = f"Enriched {total_processed} item(s)."
    if total_processed:
        message += f" ({work_seconds:.1f}s, {work_seconds / total_processed:.1f}s/item)"
    message += location_note
    print(f"[enrich] {message}")
    return {
        "processed": total_processed,
        "message": message,
        "backend": backend,
        "elapsed_seconds": work_seconds,
        "stats": stats,
    }


def enrich_items(config: Config, db: DB) -> int:
    """Same as enrich_items_detailed(background=True), returning just the
    count — kept for existing callers (the post-fetch pipeline step, run
    unattended after every fetch, so stays capped rather than picking up
    the interactive default's uncapped day window)."""
    return enrich_items_detailed(config, db, background=True)["processed"]
