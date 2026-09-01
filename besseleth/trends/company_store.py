"""Company-level business metrics (funding, stock price) — kept separate
from devices.yaml since one company can ship several devices. Same
auto-extraction philosophy as store.py: `enrich.py` drafts new entries
from scraped items (tagged `auto_extracted: true`, `source_url` citing
the specific article, never a homepage) and appends them via
`auto_upsert_company()` — which only ever adds brand-new companies, never
overwrites an existing entry (hand-edited or previously auto-extracted),
so nothing you've corrected gets clobbered on the next fetch.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Company:
    name: str
    stock_ticker: str = ""
    stock_price: float | None = None
    stock_price_updated_at: str = ""
    funding_total_usd: float | None = None
    last_funding_round: str = ""
    last_funding_date: str = ""
    source_url: str = ""
    notes: str = ""
    auto_extracted: bool = False


def load_companies(path: str | Path = "companies.yaml") -> list[Company]:
    p = Path(path)
    if not p.exists():
        return []
    with open(p) as f:
        raw = yaml.safe_load(f) or []
    return [Company(**c) for c in raw]


def save_companies(companies: list[Company], path: str | Path = "companies.yaml"):
    p = Path(path)
    raw = [c.__dict__ for c in companies]
    with open(p, "w") as f:
        yaml.safe_dump(raw, f, sort_keys=False)


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def auto_upsert_company(
    path: str | Path,
    name: str,
    funding_total_usd: float | None,
    last_funding_round: str,
    last_funding_date: str,
    source_url: str,
) -> bool:
    """Appends a new company entry if `name` isn't already present
    (case/punctuation-insensitive); never overwrites an existing one.
    Returns True if it appended."""
    if not name or not (funding_total_usd or last_funding_round):
        return False
    companies = load_companies(path)
    key = _normalize(name)
    if any(_normalize(c.name) == key for c in companies):
        return False
    companies.append(
        Company(
            name=name,
            funding_total_usd=funding_total_usd,
            last_funding_round=last_funding_round or "",
            last_funding_date=last_funding_date or "",
            source_url=source_url,
            notes="Auto-extracted by besseleth from a scraped item — verify before trusting.",
            auto_extracted=True,
        )
    )
    save_companies(companies, path)
    return True


def refresh_stock_prices(path: str | Path = "companies.yaml") -> list[str]:
    """Fetches current stock prices (free, no API key) for every company
    with a `stock_ticker` set, via Stooq's public CSV quote endpoint.
    Returns a list of log lines describing what happened."""
    from datetime import datetime, timezone

    import requests

    companies = load_companies(path)
    log = []
    for c in companies:
        if not c.stock_ticker:
            continue
        try:
            resp = requests.get(
                "https://stooq.com/q/l/",
                params={"s": c.stock_ticker, "f": "sd2t2ohlcv", "h": "", "e": "csv"},
                timeout=15,
            )
            resp.raise_for_status()
            lines = resp.text.strip().splitlines()
            if len(lines) < 2:
                log.append(f"{c.stock_ticker}: no data returned")
                continue
            row = lines[1].split(",")
            # Columns: Symbol,Date,Time,Open,High,Low,Close,Volume
            close = row[6] if len(row) > 6 else None
            if not close or close == "N/D":
                log.append(f"{c.stock_ticker}: ticker not found or market data unavailable")
                continue
            c.stock_price = float(close)
            c.stock_price_updated_at = datetime.now(timezone.utc).isoformat()
            log.append(f"{c.stock_ticker}: ${c.stock_price}")
        except Exception as e:
            log.append(f"{c.stock_ticker}: failed ({e})")

    save_companies(companies, path)
    return log
