"""Loads and validates config.yaml."""
from __future__ import annotations

import os
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
    def devices_path(self) -> Path:
        return Path(self.raw.get("trends", {}).get("devices_path", "devices.yaml"))

    @property
    def contacts(self) -> list[dict]:
        return list(self.raw.get("contacts", []))

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
