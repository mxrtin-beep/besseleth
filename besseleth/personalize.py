"""Flags items relevant to one of your contacts — an item mentioning any
of their (current or past) workplaces, or a school they attended doing
something newsworthy (e.g. a paper out of their alma mater). Powers the
report's "For you" section; contacts come from contacts_store.py (the
dashboard's Contacts tab) plus config.yaml's legacy `contacts:` list —
see config.contacts.

flag_interests() below adds a second, independent way into "For you":
a personal topic (see interests_store.py) that isn't tied to any
contact — checked only for items a contact match didn't already claim.
"""
from __future__ import annotations

import re

from .db import Item

JOB_HINT_RE = re.compile(
    r"\b(hiring|job posting|is hiring|now hiring|open role|careers page|"
    r"we're hiring|join our team|open position)\b",
    re.IGNORECASE,
)

# A social platform's generic page title ("Neuralink (@neuralink) / X"),
# not any actual post content — happens when a paste or clip only
# captured the page's <title> tag rather than the post text (X's own
# markup for this is a common source). Naming the org in the title alone
# is enough to trip a company-mention match, so this is excluded
# up front rather than surfacing a link with nothing behind it.
_SOCIAL_PROFILE_TITLE_RE = re.compile(r"\(@[\w.]+\)\s*/\s*(x|twitter)\s*$", re.IGNORECASE)


# A workplace/school name that's JUST one of these words (not part of a
# longer name — "International Neuromodulation Society" is fine, bare
# "International" is not) is almost certainly bad/truncated data, not a
# real, specific company — word-boundary matching a single common English
# word like this against arbitrary article text produces exactly the
# "why did this match" false positives it looks like on the surface (a
# paper mentioning "an international collaboration," a journal with
# "International" in its name, etc.). Skipped rather than matched, same
# spirit as enrich.py rejecting a bare university/media-outlet name as an
# "org" — a company name this generic isn't specific enough to act on.
_TOO_GENERIC_WORKPLACE_NAMES = {
    "international", "global", "national", "group", "holdings", "partners",
    "solutions", "systems", "ventures", "enterprises", "industries", "corp",
    "inc", "llc", "ltd", "company", "the", "worldwide", "consulting",
}


def _mentioned(text: str, phrase: str) -> bool:
    if not phrase or phrase.strip().lower() in _TOO_GENERIC_WORKPLACE_NAMES:
        return False
    return re.search(rf"\b{re.escape(phrase)}\b", text, re.IGNORECASE) is not None


def _is_real_content(item: Item) -> bool:
    return not _SOCIAL_PROFILE_TITLE_RE.search(item.title or "")


def _workplace_names(contact: dict) -> list[str]:
    """Every company name on a contact, old shape (singular `company`)
    or new (a `workplaces` list) — a contact loaded from config.yaml's
    legacy list only ever has the old shape; one from contacts.yaml has
    the new one. Supporting both here means personalize_items doesn't
    care which source a contact came from."""
    names = [c for c in [contact.get("company")] if c]
    names += [w.get("company") for w in (contact.get("workplaces") or []) if w.get("company")]
    return names


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
    place your friend studied is doing something notable"). Any mention
    of the company counts — deliberately not filtered by the contact's
    specific role there (surfacing everything about a friend's employer
    is the point; narrowing it down is a judgment call for the reader,
    not something to guess at silently)."""
    for item in items:
        if not _is_real_content(item):
            continue
        text = f"{item.title} {item.summary}"
        for contact in contacts:
            matched_workplace = next((c for c in _workplace_names(contact) if _mentioned(text, c)), None)
            if matched_workplace:
                item.matched_contact = contact.get("name")
                item.matched_company = matched_workplace
                item.matched_reason = "company"
                break
            matched_school = next((s for s in _school_names(contact) if _mentioned(text, s)), None)
            if matched_school:
                item.matched_contact = contact.get("name")
                item.matched_company = matched_school
                item.matched_reason = "school"
                break
    return items


def flag_interests(items: list[Item], interests: list[str]) -> list[Item]:
    """Flags items matching a personal interest phrase — same whole-
    phrase, case-insensitive mention check as a contact's workplace, but
    not tied to any person. Only checked for an item nothing else has
    already claimed (a contact match is more specific/actionable, so it
    takes priority when both would apply)."""
    for item in items:
        if item.matched_contact or item.matched_reason or not _is_real_content(item):
            continue
        text = f"{item.title} {item.summary}"
        matched = next((phrase for phrase in interests if _mentioned(text, phrase)), None)
        if matched:
            item.matched_company = matched
            item.matched_reason = "interest"
    return items


def is_job_related(item: Item) -> bool:
    return bool(JOB_HINT_RE.search(f"{item.title} {item.summary}"))
