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


def _mentioned(text: str, phrase: str) -> bool:
    if not phrase:
        return False
    return re.search(rf"\b{re.escape(phrase)}\b", text, re.IGNORECASE) is not None


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
        if item.matched_contact or item.matched_reason:
            continue
        text = f"{item.title} {item.summary}"
        matched = next((phrase for phrase in interests if _mentioned(text, phrase)), None)
        if matched:
            item.matched_company = matched
            item.matched_reason = "interest"
    return items


def is_job_related(item: Item) -> bool:
    return bool(JOB_HINT_RE.search(f"{item.title} {item.summary}"))
