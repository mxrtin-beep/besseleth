"""Company-level business metrics (funding, stock price) — kept separate
from devices.yaml since one company can ship several devices. Same
philosophy as store.py: hand-maintained (with `source_url` citing the
specific press release/filing, not a homepage), accumulates over time,
never auto-written by the pipeline.
"""
from __future__ import annotations

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
