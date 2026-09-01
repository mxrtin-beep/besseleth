"""Flags items that mention a company one of your contacts works at —
e.g. a job posting or news story about Neuralink when Jane Doe works there.
"""
from __future__ import annotations

import re

from .db import Item

JOB_HINT_RE = re.compile(
    r"\b(hiring|job posting|is hiring|now hiring|open role|careers page|"
    r"we're hiring|join our team|open position)\b",
    re.IGNORECASE,
)


def _company_mentioned(text: str, company: str) -> bool:
    if not company:
        return False
    return re.search(rf"\b{re.escape(company)}\b", text, re.IGNORECASE) is not None


def personalize_items(items: list[Item], contacts: list[dict]) -> list[Item]:
    """Mutates and returns items, setting matched_contact/matched_company
    when the item's title+summary mentions a contact's company."""
    for item in items:
        text = f"{item.title} {item.summary}"
        for contact in contacts:
            company = contact.get("company", "")
            if _company_mentioned(text, company):
                item.matched_contact = contact.get("name")
                item.matched_company = company
                break
    return items


def is_job_related(item: Item) -> bool:
    return bool(JOB_HINT_RE.search(f"{item.title} {item.summary}"))
