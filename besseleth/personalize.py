"""Flags items relevant to one of your contacts — an item mentioning
their current company, or their alma mater doing something newsworthy
(e.g. a paper out of the university they went to). Powers the report's
"For you" section; contacts come from contacts_store.py (the dashboard's
Contacts tab) plus config.yaml's legacy `contacts:` list — see
config.contacts.
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


def personalize_items(items: list[Item], contacts: list[dict]) -> list[Item]:
    """Mutates and returns items, setting matched_contact/matched_company/
    matched_reason when the item's title+summary mentions a contact's
    current company or school. Company is checked first (a closer,
    more-actionable match — "your friend's employer is in the news")
    before falling back to school (more serendipitous — "the place your
    friend studied is doing something notable")."""
    for item in items:
        text = f"{item.title} {item.summary}"
        for contact in contacts:
            company = contact.get("company", "")
            school = contact.get("school", "")
            if _mentioned(text, company):
                item.matched_contact = contact.get("name")
                item.matched_company = company
                item.matched_reason = "company"
                break
            if _mentioned(text, school):
                item.matched_contact = contact.get("name")
                item.matched_company = school
                item.matched_reason = "school"
                break
    return items


def is_job_related(item: Item) -> bool:
    return bool(JOB_HINT_RE.search(f"{item.title} {item.summary}"))
