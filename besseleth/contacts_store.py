"""Friends/LinkedIn contacts — powers the report's "For you" section
(see personalize.py) by tracking who you know and where they work/
studied, so an item mentioning their company or alma mater gets
surfaced as relevant to you specifically.

Same satellite-file pattern as devices.yaml/companies.yaml/job_boards.yaml/
feeds.yaml: config.yaml stays the hand-edited, heavily-commented config;
this is small, UI-managed data the dashboard's Contacts tab reads and
writes wholesale. config.yaml's own (legacy) `contacts:` list, if you
have one, is still honored — see config.contacts, which merges both —
but the dashboard only ever writes here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import yaml


@dataclass
class Contact:
    name: str
    company: str = ""          # current employer — matched against item text (see personalize.py)
    role: str = ""              # job title, freeform
    school: str = ""            # college/university — also matched against item text
    linkedin_url: str = ""
    relationship: str = ""      # freeform, e.g. "college friend", "former coworker", "conference contact"
    notes: str = ""


def load_contacts(path: str | Path = "contacts.yaml") -> list[Contact]:
    p = Path(path)
    if not p.exists():
        return []
    with open(p) as f:
        raw = yaml.safe_load(f) or []
    return [Contact(**{k: v for k, v in c.items() if k in Contact.__dataclass_fields__}) for c in raw]


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
