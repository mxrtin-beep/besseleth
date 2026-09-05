"""Flags items relevant to one of your contacts — an item mentioning any
of their (current or past) workplaces, or a school they attended doing
something newsworthy (e.g. a paper out of their alma mater). Powers the
report's "For you" section; contacts come from contacts_store.py (the
dashboard's Contacts tab) plus config.yaml's legacy `contacts:` list —
see config.contacts.
"""
from __future__ import annotations

import re

from .db import Item

JOB_HINT_RE = re.compile(
    r"\b(hiring|job posting|is hiring|now hiring|open role|careers page|"
    r"we're hiring|join our team|open position)\b",
    re.IGNORECASE,
)


def _mentioned(text: str, phrase: str) -> bool:
    if not phrase:
        return False
    return re.search(rf"\b{re.escape(phrase)}\b", text, re.IGNORECASE) is not None


def _workplace_entries(contact: dict) -> list[dict]:
    """Every {"company", "role"} on a contact, old shape (singular
    `company`/`role`) or new (a `workplaces` list) — a contact loaded
    from config.yaml's legacy list only ever has the old shape; one from
    contacts.yaml has the new one. Supporting both here means
    personalize_items doesn't care which source a contact came from."""
    entries = []
    if contact.get("company"):
        entries.append({"company": contact["company"], "role": contact.get("role", "")})
    entries += [w for w in (contact.get("workplaces") or []) if w.get("company")]
    return entries


# Generic job-title words that say nothing about WHAT the person actually
# works on — "Engineer"/"Manager"/"Senior" appear in a huge range of
# unrelated roles, so they're dropped rather than used to require a text
# match. What's left ("electrical", "software", "clinical", ...) is
# usually the one word that actually says what to filter for.
_ROLE_STOPWORDS = {
    "the", "of", "and", "a", "an", "at", "in", "on", "for", "to", "i", "ii", "iii", "iv", "sr", "jr",
    "engineer", "engineering", "scientist", "manager", "director", "lead", "senior", "staff", "principal",
    "specialist", "associate", "analyst", "consultant", "officer", "president", "vp", "head", "coordinator",
    "intern", "researcher", "member", "technical", "professional",
}

# A role's own word ("electrical") rarely appears verbatim in an article
# about the actual work ("Neuralink's new chip has a faster wireless
# link") — this expands a handful of common engineering-role words to
# terms an article on that specialty is actually likely to use. Not
# exhaustive; a role word with no entry here still works, just on an
# exact-word match alone (see _is_role_relevant).
_ROLE_SYNONYMS: dict[str, set[str]] = {
    "electrical": {"circuit", "chip", "asic", "pcb", "hardware", "voltage", "amplifier", "signal", "wireless", "battery", "power", "sensor", "semiconductor"},
    "mechanical": {"housing", "enclosure", "actuator", "motor", "mechanism", "packaging"},
    "software": {"firmware", "algorithm", "code", "application", "platform", "sdk", "api"},
    "firmware": {"embedded", "microcontroller", "chip"},
    "hardware": {"chip", "circuit", "pcb", "asic", "device", "electrical"},
    "biomedical": {"implant", "device", "clinical", "biocompatible", "fda"},
    "clinical": {"trial", "patient", "fda", "clinic"},
    "chemical": {"material", "coating", "polymer", "biocompatible"},
    "materials": {"material", "coating", "polymer", "substrate"},
    "data": {"dataset", "model", "algorithm", "analytics"},
    "machine": {"model", "algorithm", "neural"},
}


def _role_keywords(role: str) -> list[str]:
    words = re.findall(r"[a-zA-Z]+", (role or "").lower())
    return [w for w in words if w not in _ROLE_STOPWORDS and len(w) > 2]


def _is_role_relevant(item: Item, role: str) -> bool:
    """When a contact's role has a distinctive word ("electrical" from
    "Electrical Engineer"), require the item to actually look relevant
    to that specialty — not just any mention of the company at all —
    checked against the item's own text plus whatever enrichment already
    extracted (modality, org_description often use the vocabulary a role
    title wouldn't literally repeat). No distinctive word (a bare
    "Engineer", or no role given) means nothing to filter on, so any
    mention of the company still counts, same as before this existed."""
    keywords = _role_keywords(role)
    if not keywords:
        return True
    text = f"{item.title} {item.summary} {item.modality or ''}".lower()
    return any(kw in text or any(syn in text for syn in _ROLE_SYNONYMS.get(kw, ())) for kw in keywords)


def _school_names(contact: dict) -> list[str]:
    names = [s for s in [contact.get("school")] if s]
    names += [s.get("name") for s in (contact.get("schools") or []) if s.get("name")]
    return names


def personalize_items(items: list[Item], contacts: list[dict]) -> list[Item]:
    """Mutates and returns items, setting matched_contact/matched_company/
    matched_reason when the item's title+summary mentions one of a
    contact's workplaces or schools. Workplaces are checked first (a
    closer, more-actionable match — "your friend's employer is in the
    news") before falling back to schools (more serendipitous — "the
    place your friend studied is doing something notable"). A workplace
    match also has to look relevant to that contact's specific role
    there when the role says anything distinctive (see
    _is_role_relevant) — otherwise every mention of a big employer like
    Neuralink would surface for a contact whose actual job has nothing
    to do with the story."""
    for item in items:
        text = f"{item.title} {item.summary}"
        for contact in contacts:
            matched_workplace = next(
                (
                    w for w in _workplace_entries(contact)
                    if _mentioned(text, w["company"]) and _is_role_relevant(item, w.get("role", ""))
                ),
                None,
            )
            if matched_workplace:
                item.matched_contact = contact.get("name")
                item.matched_company = matched_workplace["company"]
                item.matched_reason = "company"
                break
            matched_school = next((s for s in _school_names(contact) if _mentioned(text, s)), None)
            if matched_school:
                item.matched_contact = contact.get("name")
                item.matched_company = matched_school
                item.matched_reason = "school"
                break
    return items


def is_job_related(item: Item) -> bool:
    return bool(JOB_HINT_RE.search(f"{item.title} {item.summary}"))
