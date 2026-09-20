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

    def _resolve(self, raw_path: str) -> Path:
        """Resolves a config-declared relative path (db, contacts.yaml,
        etc.) against THIS config file's own directory, not the
        process's working directory — same result as before for the
        single-industry case (where they're the same directory), but
        what makes discover_industries() safe: two industries each
        under their own industries/<slug>/ folder get their own
        self-contained data (db, contacts, devices, ...) without
        colliding, even though both configs are loaded by the same
        running process with one shared CWD. An absolute path in
        config.yaml is left as-is (honors an explicit override)."""
        p = Path(raw_path)
        return p if p.is_absolute() else self.path.parent / p

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
        return self._resolve(self.raw.get("trends", {}).get("devices_path", "devices.yaml"))

    @property
    def legacy_companies_yaml_path(self) -> Path:
        return self._resolve(self.raw.get("trends", {}).get("companies_path", "companies.yaml"))

    @property
    def job_boards_path(self) -> Path:
        return self._resolve(self.raw.get("jobs", {}).get("manual_boards_path", "job_boards.yaml"))

    @property
    def feeds_path(self) -> Path:
        return self._resolve(self.raw.get("feeds_path", "feeds.yaml"))

    @property
    def contacts_path(self) -> Path:
        return self._resolve(self.raw.get("contacts_path", "contacts.yaml"))

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
        return self._resolve(self.raw.get("interests_path", "interests.yaml"))

    @property
    def interests(self) -> list[str]:
        from .interests_store import load_interests

        return load_interests(self.interests_path)

    @property
    def labs_path(self) -> Path:
        return self._resolve(self.raw.get("labs_path", "labs.yaml"))

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
        return self._resolve(self.raw.get("database", {}).get("path", "data/besseleth.db"))


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if not p.exists():
        example = Path("config.example.yaml")
        raise FileNotFoundError(
            f"Config file not found at {p}. Copy {example} to {p} and edit it "
            f"for your industry, contacts, and sources."
        )
    with open(p, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config(raw=raw, path=p)


DEFAULT_INDUSTRY_SLUG = "default"


def discover_industries(primary: Config, industries_dir: str | Path = "industries") -> dict[str, Config]:
    """First step toward multiple industries in ONE running app (e.g. a
    Neurotech dashboard and an AI dashboard, switchable without
    restarting anything) — see the `industries/` layout below. Returns
    `{slug: Config}`, always including `primary` under
    DEFAULT_INDUSTRY_SLUG so a caller with no industries/ directory at
    all (every existing single-industry install) gets back exactly one
    entry and nothing about today's behavior changes.

    Layout: `industries/<slug>/config.yaml`, one subfolder per
    industry — each config.yaml's own relative paths (db, contacts.yaml,
    etc.) resolve against ITS OWN subfolder (see Config._resolve), so
    two industries loaded by the same process never share a database or
    a contacts list just because they share one working directory.
    `<slug>` is whatever the folder is named (e.g. "neurotech", "ai") —
    that's what a caller passes as `?industry=<slug>` once routes start
    accepting one; nothing reads that query param yet.

    A broken industries/<slug>/config.yaml (missing file mid-edit, bad
    YAML) is skipped with a printed warning rather than crashing the
    whole app over one bad subfolder — every OTHER industry, and the
    primary config, still come up fine."""
    result: dict[str, Config] = {DEFAULT_INDUSTRY_SLUG: primary}
    base = Path(industries_dir)
    if not base.is_dir():
        return result
    for sub in sorted(base.iterdir()):
        if not sub.is_dir():
            continue
        config_path = sub / "config.yaml"
        if not config_path.exists():
            continue
        try:
            result[sub.name] = load_config(config_path)
        except Exception as e:
            print(f"[config] Skipping industries/{sub.name}/config.yaml — failed to load: {e}")
    return result


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def _find_block(text: str, top_key: str) -> re.Match | None:
    return re.compile(rf"^{re.escape(top_key)}:[ \t]*\n((?:[ \t]+.*\n?|\n)*)", re.MULTILINE).search(text)


def _block_indent(block: str) -> str:
    indent_match = re.search(r"^([ \t]+)\S", block, re.MULTILINE)
    return indent_match.group(1) if indent_match else "  "


def _set_scalar_in_block(text: str, top_key: str, field_key: str, rendered_value: str) -> str:
    """Rewrites a single `field_key: <rendered_value>` line inside a
    top-level `top_key:` block of raw config.yaml TEXT (not the parsed
    dict) — inserts it if missing, replaces just that line if present,
    and otherwise touches nothing else in the file: every comment and
    every other setting survives. `rendered_value` is the literal text
    to place after the colon — callers pass `json.dumps(x)` for a string
    (always double-quoted, no manual escaping needed) or just `str(x)`
    for a number/bool, since YAML's bare (unquoted) form IS how PyYAML
    renders those types too. Used by the dashboard's Settings tab so a
    hand-edited config.yaml's comments and formatting survive a save,
    unlike a full yaml dump would (see config.example.yaml — it's mostly
    comments)."""
    match = _find_block(text, top_key)
    if not match:
        # No such top-level block yet — append a brand new minimal one.
        return text.rstrip("\n") + f"\n\n{top_key}:\n  {field_key}: {rendered_value}\n"

    block = match.group(1)
    indent = _block_indent(block)
    field_re = re.compile(rf"^{indent}{re.escape(field_key)}:.*$", re.MULTILINE)
    if field_re.search(block):
        new_block = field_re.sub(f"{indent}{field_key}: {rendered_value}", block, count=1)
    else:
        new_block = block.rstrip("\n") + f"\n{indent}{field_key}: {rendered_value}\n"
    return text[: match.start(1)] + new_block + text[match.end(1) :]


def _set_list_in_block(text: str, top_key: str, field_key: str, items: list[str]) -> str:
    """Same idea as _set_scalar_in_block, for a YAML block-style list
    value (e.g. industry.keywords) instead of a scalar — replaces
    whichever form is already there (a `field_key: [a, b]` one-liner, or
    a `field_key:` header followed by indented `- "item"` lines) with a
    canonical block-style list, and otherwise leaves the rest of the
    top-level block untouched."""
    match = _find_block(text, top_key)
    rendered_items = lambda item_indent: "".join(f"{item_indent}- {json.dumps(it)}\n" for it in items)  # noqa: E731
    if not match:
        return text.rstrip("\n") + f"\n\n{top_key}:\n  {field_key}:\n" + rendered_items("    ")

    block = match.group(1)
    indent = _block_indent(block)
    item_indent = indent + "  "
    new_field_block = f"{indent}{field_key}:\n" + rendered_items(item_indent)

    lines = block.splitlines(keepends=True)
    field_line_re = re.compile(rf"^{re.escape(indent)}{re.escape(field_key)}:")
    start_idx = next((i for i, line in enumerate(lines) if field_line_re.match(line)), None)
    if start_idx is None:
        new_block = block.rstrip("\n") + ("\n" if block.strip() else "") + new_field_block
    else:
        # Consume every line right after the field header that belongs to
        # its OLD value: a blank line, or one indented deeper than the
        # field itself (a `- item` line, or a wrapped inline value) —
        # stopping at the first sibling field (same indent) or dedent.
        end_idx = start_idx + 1
        while end_idx < len(lines):
            line = lines[end_idx]
            if line.strip() == "":
                end_idx += 1
                continue
            if len(line) - len(line.lstrip(" \t")) > len(indent):
                end_idx += 1
                continue
            break
        new_block = "".join(lines[:start_idx]) + new_field_block + "".join(lines[end_idx:])
    return text[: match.start(1)] + new_block + text[match.end(1) :]


def update_summarizer_settings(config: "Config", **fields: str) -> None:
    """Persists one or more summarizer.yaml fields (e.g. backend,
    groq_api_key, ollama_url, model) straight into config.yaml on disk —
    surgical text edits (see _set_scalar_in_block), not a full re-dump,
    so a hand-written config.yaml's comments and formatting survive.
    Also updates `config.raw` in memory so the change takes effect
    immediately, without restarting the process."""
    text = config.path.read_text(encoding="utf-8")
    for key, value in fields.items():
        if value is None:
            continue
        text = _set_scalar_in_block(text, "summarizer", key, json.dumps(value))
    config.path.write_text(text, encoding="utf-8")
    config.raw.setdefault("summarizer", {}).update({k: v for k, v in fields.items() if v is not None})


def update_schedule_settings(
    config: "Config", fetch_interval_hours: float | None = None,
    report_cron: str | None = None, timezone: str | None = None,
) -> None:
    """Persists schedule.fetch_interval_hours/report_cron/timezone into
    config.yaml (surgical edit — see _set_scalar_in_block) and updates
    config.raw in memory. Doesn't restart the scheduler itself — see
    besseleth/scheduler.py; the running APScheduler job needs to be
    re-armed with the new interval/cron, which the caller (the
    /api/settings/schedule route) does via SchedulerStatus/reschedule
    rather than this function, which only touches config."""
    text = config.path.read_text(encoding="utf-8")
    if fetch_interval_hours is not None:
        text = _set_scalar_in_block(text, "schedule", "fetch_interval_hours", str(fetch_interval_hours))
    if report_cron:
        text = _set_scalar_in_block(text, "schedule", "report_cron", json.dumps(report_cron))
    if timezone is not None:
        # "" means "clear it / use host local time" — write null rather
        # than an empty string, since that's what config.raw.get(...) or
        # an unset key both already mean throughout scheduler.py.
        text = _set_scalar_in_block(text, "schedule", "timezone", json.dumps(timezone) if timezone else "null")
    config.path.write_text(text, encoding="utf-8")
    sched = config.raw.setdefault("schedule", {})
    if fetch_interval_hours is not None:
        sched["fetch_interval_hours"] = fetch_interval_hours
    if report_cron:
        sched["report_cron"] = report_cron
    if timezone is not None:
        sched["timezone"] = timezone or None


def update_industry_settings(config: "Config", name: str | None = None, keywords: list[str] | None = None) -> None:
    """Persists industry.name/keywords into config.yaml (surgical edit)
    and updates config.raw in memory — takes effect on the next
    fetch/enrich pass, which all read config fresh rather than caching
    these at startup."""
    text = config.path.read_text(encoding="utf-8")
    if name:
        text = _set_scalar_in_block(text, "industry", "name", json.dumps(name))
    if keywords is not None:
        text = _set_list_in_block(text, "industry", "keywords", keywords)
    config.path.write_text(text, encoding="utf-8")
    industry = config.raw.setdefault("industry", {})
    if name:
        industry["name"] = name
    if keywords is not None:
        industry["keywords"] = keywords
