"""Loads and validates config.yaml."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config.yaml")


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path

    @property
    def industry_name(self) -> str:
        return self.raw["industry"]["name"]

    @property
    def keywords(self) -> list[str]:
        return list(self.raw["industry"]["keywords"])

    @property
    def arxiv_categories(self) -> list[str]:
        return list(self.raw["industry"].get("arxiv_categories", []))

    @property
    def trend_metrics(self) -> list[dict]:
        return list(self.raw["industry"].get("trend_metrics", []))

    @property
    def company_metrics(self) -> list[dict]:
        return list(self.raw["industry"].get("company_metrics", []))

    @property
    def devices_path(self) -> Path:
        """Devices/companies live in the main sqlite db now (see db.py) —
        a few data points didn't need their own hand-copied YAML file,
        and a sqlite table gets new columns via migration instead of
        needing devices.example.yaml re-copied by hand. This still
        returns the *db* path; legacy_devices_yaml_path below is the old
        file, imported once (see trends/store.py) if you have one."""
        return self.db_path

    @property
    def companies_path(self) -> Path:
        return self.db_path

    @property
    def legacy_devices_yaml_path(self) -> Path:
        return Path(self.raw.get("trends", {}).get("devices_path", "devices.yaml"))

    @property
    def legacy_companies_yaml_path(self) -> Path:
        return Path(self.raw.get("trends", {}).get("companies_path", "companies.yaml"))

    @property
    def job_boards_path(self) -> Path:
        return Path(self.raw.get("jobs", {}).get("manual_boards_path", "job_boards.yaml"))

    @property
    def feeds_path(self) -> Path:
        return Path(self.raw.get("feeds_path", "feeds.yaml"))

    @property
    def contacts_path(self) -> Path:
        return Path(self.raw.get("contacts_path", "contacts.yaml"))

    @property
    def contacts(self) -> list[dict]:
        """config.yaml's own `contacts:` list (legacy — still honored)
        plus contacts.yaml (the dashboard's Contacts tab writes only
        here). Both are supported so migrating isn't required, but new
        contacts should go through the tab."""
        from dataclasses import asdict

        from .contacts_store import load_contacts

        merged = list(self.raw.get("contacts", []))
        merged.extend(asdict(c) for c in load_contacts(self.contacts_path))
        return merged

    @property
    def interests_path(self) -> Path:
        return Path(self.raw.get("interests_path", "interests.yaml"))

    @property
    def interests(self) -> list[str]:
        from .interests_store import load_interests

        return load_interests(self.interests_path)

    @property
    def labs_path(self) -> Path:
        return Path(self.raw.get("labs_path", "labs.yaml"))

    @property
    def labs(self) -> list[dict]:
        from .labs_store import load_labs

        return load_labs(self.labs_path)

    def source(self, name: str) -> dict:
        return self.raw.get("sources", {}).get(name, {}) or {}

    @property
    def summarizer(self) -> dict:
        return self.raw.get("summarizer", {})

    @property
    def report(self) -> dict:
        return self.raw.get("report", {})

    @property
    def db_path(self) -> Path:
        return Path(self.raw.get("database", {}).get("path", "data/besseleth.db"))


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if not p.exists():
        example = Path("config.example.yaml")
        raise FileNotFoundError(
            f"Config file not found at {p}. Copy {example} to {p} and edit it "
            f"for your industry, contacts, and sources."
        )
    with open(p, "r") as f:
        raw = yaml.safe_load(f)
    return Config(raw=raw, path=p)


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def _set_scalar_in_block(text: str, top_key: str, field_key: str, value: str) -> str:
    """Rewrites a single `field_key: value` line inside a top-level
    `top_key:` block of raw config.yaml TEXT (not the parsed dict) —
    inserts it if missing, replaces just that line if present, and
    otherwise touches nothing else in the file: every comment and every
    other setting survives. Used by the dashboard's Settings tab to
    change summarizer.backend/groq_api_key without a full yaml dump,
    which would silently throw away every comment in a hand-edited
    config.yaml (see config.example.yaml — it's mostly comments)."""
    block_re = re.compile(rf"^{re.escape(top_key)}:[ \t]*\n((?:[ \t]+.*\n?|\n)*)", re.MULTILINE)
    match = block_re.search(text)
    quoted = json.dumps(value)  # always double-quoted, valid YAML scalar, no manual escaping needed
    if not match:
        # No such top-level block yet — append a brand new minimal one.
        return text.rstrip("\n") + f"\n\n{top_key}:\n  {field_key}: {quoted}\n"

    block = match.group(1)
    indent_match = re.search(r"^([ \t]+)\S", block, re.MULTILINE)
    indent = indent_match.group(1) if indent_match else "  "
    field_re = re.compile(rf"^{indent}{re.escape(field_key)}:.*$", re.MULTILINE)
    if field_re.search(block):
        new_block = field_re.sub(f"{indent}{field_key}: {quoted}", block, count=1)
    else:
        new_block = block.rstrip("\n") + f"\n{indent}{field_key}: {quoted}\n"
    return text[: match.start(1)] + new_block + text[match.end(1) :]


def update_summarizer_settings(config: "Config", **fields: str) -> None:
    """Persists one or more summarizer.yaml fields (e.g. backend,
    groq_api_key, ollama_url, model) straight into config.yaml on disk —
    surgical text edits (see _set_scalar_in_block), not a full re-dump,
    so a hand-written config.yaml's comments and formatting survive.
    Also updates `config.raw` in memory so the change takes effect
    immediately, without restarting the process."""
    text = config.path.read_text()
    for key, value in fields.items():
        if value is None:
            continue
        text = _set_scalar_in_block(text, "summarizer", key, value)
    config.path.write_text(text)
    config.raw.setdefault("summarizer", {}).update({k: v for k, v in fields.items() if v is not None})
