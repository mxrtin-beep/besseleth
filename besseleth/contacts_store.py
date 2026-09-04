"""Friends/LinkedIn contacts — powers the report's "For you" section
(see personalize.py) by tracking who you know, where they work(ed)/
studied, so an item mentioning any of their employers or schools gets
surfaced as relevant to you specifically.

Same satellite-file pattern as devices.yaml/companies.yaml/job_boards.yaml/
feeds.yaml: config.yaml stays the hand-edited, heavily-commented config;
this is small, UI-managed data the dashboard's Contacts tab reads and
writes wholesale. config.yaml's own (legacy) `contacts:` list, if you
have one, is still honored — see config.contacts, which merges both —
but the dashboard only ever writes here.

Workplaces and schools are each a *list*, not one field — most people
have more than one of both (a past job and a current one; undergrad and
grad school), and news can just as easily mention a former employer or
an alma mater as a current one. Kept as plain {name/company, role/level}
dicts rather than their own dataclasses — this file has no need to
validate them beyond "has a name", and a dict round-trips through YAML
and JSON without any extra plumbing.
"""
from __future__ import annotations

import csv
import io
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml


@dataclass
class Contact:
    name: str
    emails: list[str] = field(default_factory=list)
    linkedin_url: str = ""
    # Each: {"company": str, "role": str}. Order is whatever you entered
    # — put current first if it matters to you, nothing here assumes one
    # is "the" current job.
    workplaces: list[dict] = field(default_factory=list)
    # Each: {"name": str, "level": str} — level is freeform (e.g.
    # "Undergrad", "PhD", "MBA"), not a fixed set.
    schools: list[dict] = field(default_factory=list)
    relationship: str = ""      # freeform, e.g. "college friend", "former coworker", "conference contact"
    notes: str = ""


def _migrate_legacy_fields(raw: dict) -> dict:
    """A contact saved before workplaces/schools/emails existed used
    singular `company`/`school`/no email field at all — fold those into
    the new list-shaped fields so old data (including anything already
    added through the Contacts tab before this change) keeps working
    without a manual re-entry pass."""
    raw = dict(raw)
    if raw.get("company") and not raw.get("workplaces"):
        raw["workplaces"] = [{"company": raw["company"], "role": raw.pop("role", "")}]
    raw.pop("company", None)
    raw.pop("role", None)
    if raw.get("school") and not raw.get("schools"):
        raw["schools"] = [{"name": raw["school"], "level": ""}]
    raw.pop("school", None)
    if raw.get("email") and not raw.get("emails"):
        raw["emails"] = [raw["email"]]
    raw.pop("email", None)
    return raw


def load_contacts(path: str | Path = "contacts.yaml") -> list[Contact]:
    p = Path(path)
    if not p.exists():
        return []
    with open(p) as f:
        raw = yaml.safe_load(f) or []
    contacts = []
    for c in raw:
        c = _migrate_legacy_fields(c)
        contacts.append(Contact(**{k: v for k, v in c.items() if k in Contact.__dataclass_fields__}))
    return contacts


def save_contacts(contacts: list[Contact], path: str | Path = "contacts.yaml") -> None:
    with open(path, "w") as f:
        yaml.safe_dump([asdict(c) for c in contacts], f, sort_keys=False)


def add_contact(path: str | Path, contact: Contact) -> None:
    contacts = load_contacts(path)
    contacts.append(contact)
    save_contacts(contacts, path)


def update_contact(path: str | Path, index: int, contact: Contact) -> bool:
    contacts = load_contacts(path)
    if not (0 <= index < len(contacts)):
        return False
    contacts[index] = contact
    save_contacts(contacts, path)
    return True


def remove_contact(path: str | Path, index: int) -> bool:
    contacts = load_contacts(path)
    if not (0 <= index < len(contacts)):
        return False
    contacts.pop(index)
    save_contacts(contacts, path)
    return True


# --- LinkedIn connections import ---------------------------------------
#
# LinkedIn has no API (official or otherwise, without violating its ToS)
# for a third-party app to pull your connections — there's no live
# "sync" to build here. What LinkedIn DOES offer, officially, is letting
# you export your own connections as a CSV: Settings & Privacy -> Data
# privacy -> "Get a copy of your data" -> check "Connections" -> request
# archive -> LinkedIn emails you a download link (can take a few minutes
# to a day). That CSV is the input this reads — a one-time or occasional
# bulk import, not automatic, but far faster than adding people one at a
# time. Columns as of writing: First Name, Last Name, URL, Email
# Address, Company, Position, Connected On — LinkedIn's export format
# has changed before and could again, so this looks up columns by name
# (case-insensitive) rather than assuming a fixed order, and skips
# anything it can't make sense of instead of failing the whole import.

_LINKEDIN_COLUMN_ALIASES = {
    "first_name": ["first name"],
    "last_name": ["last name"],
    "url": ["url", "profile url"],
    "email": ["email address", "email"],
    "company": ["company"],
    "position": ["position", "title"],
}


def _find_header_row(rows: list[list[str]]) -> int | None:
    """LinkedIn's export prepends a few "Notes:" lines before the real
    header — find the row that actually looks like one (has both a
    First Name and a Last Name column) instead of assuming row 0."""
    for i, row in enumerate(rows):
        lowered = [c.strip().lower() for c in row]
        if "first name" in lowered and "last name" in lowered:
            return i
    return None


def parse_linkedin_connections_csv(csv_text: str) -> list[Contact]:
    """Parses a LinkedIn "Connections.csv" export into Contact objects
    (name, email, linkedin_url, one workplace from Company/Position — no
    school; LinkedIn's export doesn't include education). Returns []
    if it doesn't look like a LinkedIn connections export at all (no
    recognizable header) rather than guessing."""
    rows = list(csv.reader(io.StringIO(csv_text)))
    header_idx = _find_header_row(rows)
    if header_idx is None:
        return []

    header = [c.strip().lower() for c in rows[header_idx]]
    col_index = {}
    for field_name, aliases in _LINKEDIN_COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in header:
                col_index[field_name] = header.index(alias)
                break

    if "first_name" not in col_index or "last_name" not in col_index:
        return []

    contacts = []
    for row in rows[header_idx + 1 :]:
        if len(row) <= max(col_index.values(), default=-1):
            continue

        def get(field_name: str) -> str:
            idx = col_index.get(field_name)
            return row[idx].strip() if idx is not None and idx < len(row) else ""

        first, last = get("first_name"), get("last_name")
        name = f"{first} {last}".strip()
        if not name:
            continue

        company, position = get("company"), get("position")
        workplaces = [{"company": company, "role": position}] if company else []
        email = get("email")

        contacts.append(
            Contact(
                name=name,
                emails=[email] if email else [],
                linkedin_url=get("url"),
                workplaces=workplaces,
            )
        )
    return contacts


def import_linkedin_csv(path: str | Path, csv_text: str) -> int:
    """Appends every contact parsed from `csv_text` that isn't already
    present (matched by linkedin_url if both have one, else by exact
    name) — safe to import the same export twice without duplicating
    everyone. Returns how many new contacts were added."""
    existing = load_contacts(path)
    existing_urls = {c.linkedin_url for c in existing if c.linkedin_url}
    existing_names = {c.name.strip().lower() for c in existing}

    added = 0
    for contact in parse_linkedin_connections_csv(csv_text):
        if contact.linkedin_url and contact.linkedin_url in existing_urls:
            continue
        if not contact.linkedin_url and contact.name.strip().lower() in existing_names:
            continue
        existing.append(contact)
        if contact.linkedin_url:
            existing_urls.add(contact.linkedin_url)
        existing_names.add(contact.name.strip().lower())
        added += 1

    if added:
        save_contacts(existing, path)
    return added
