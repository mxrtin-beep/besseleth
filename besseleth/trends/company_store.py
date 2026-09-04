"""Company-level business metrics (funding, stock price, IPO status) —
kept separate from devices since one company can ship several devices.

Lives in the main sqlite db (see db.py's `companies` table), for the same
reason as trends/store.py's Device data: a new field (like the
ipo_date/stock_exchange/is_public columns below) shows up automatically
via migration instead of needing companies.example.yaml re-copied by
hand. If you have an old companies.yaml, it's imported once, the first
time this module is used.

Same auto-extraction philosophy as store.py: `enrich.py` drafts new
entries from scraped items (tagged `auto_extracted=True`, `source_url`
citing the specific article, never a homepage) and adds them via
`auto_upsert_company()` — which only ever adds brand-new companies, never
overwrites an existing row (hand-edited or previously auto-extracted), so
nothing you've corrected gets clobbered on the next fetch.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Company:
    name: str
    stock_ticker: str = ""
    stock_price: float | None = None
    stock_price_updated_at: str = ""
    funding_total_usd: float | None = None
    last_funding_round: str = ""
    last_funding_date: str = ""
    ipo_date: str = ""              # ISO8601 date, if it's gone public
    stock_exchange: str = ""        # e.g. "NASDAQ" — set once ipo_date is
    is_public: bool = False
    source_url: str = ""
    notes: str = ""
    auto_extracted: bool = False


def _row_to_company(row) -> Company:
    return Company(
        name=row["name"],
        stock_ticker=row["stock_ticker"] or "",
        stock_price=row["stock_price"],
        stock_price_updated_at=row["stock_price_updated_at"] or "",
        funding_total_usd=row["funding_total_usd"],
        last_funding_round=row["last_funding_round"] or "",
        last_funding_date=row["last_funding_date"] or "",
        ipo_date=row["ipo_date"] or "",
        stock_exchange=row["stock_exchange"] or "",
        is_public=bool(row["is_public"]),
        source_url=row["source_url"] or "",
        notes=row["notes"] or "",
        auto_extracted=bool(row["auto_extracted"]),
    )


def _migrate_legacy_yaml(db, legacy_yaml_path: str | Path | None) -> None:
    if not legacy_yaml_path:
        return
    p = Path(legacy_yaml_path)
    if not p.exists() or db.companies():
        return
    import yaml

    with open(p) as f:
        raw = yaml.safe_load(f) or []
    for c in raw:
        db.add_company(
            name=c.get("name", ""),
            stock_ticker=c.get("stock_ticker", ""),
            stock_price=c.get("stock_price"),
            stock_price_updated_at=c.get("stock_price_updated_at", ""),
            funding_total_usd=c.get("funding_total_usd"),
            last_funding_round=c.get("last_funding_round", ""),
            last_funding_date=c.get("last_funding_date", ""),
            ipo_date=c.get("ipo_date", ""),
            stock_exchange=c.get("stock_exchange", ""),
            is_public=bool(c.get("is_public", False)),
            source_url=c.get("source_url", ""),
            notes=c.get("notes", "") or f"Imported from {p.name}.",
            auto_extracted=bool(c.get("auto_extracted", False)),
        )
    print(f"[trends] Imported {len(raw)} company(s) from {p} into the database — you can delete that file now.")


def load_companies(db_path: str | Path, legacy_yaml_path: str | Path | None = None) -> list[Company]:
    from ..db import DB

    db = DB(Path(db_path))
    try:
        _migrate_legacy_yaml(db, legacy_yaml_path)
        return [_row_to_company(r) for r in db.companies()]
    finally:
        db.close()


def auto_upsert_company(
    path: str | Path,
    name: str,
    funding_total_usd: float | None,
    last_funding_round: str,
    last_funding_date: str,
    source_url: str,
) -> bool:
    """Adds a new company row if `name` isn't already present
    (case/punctuation-insensitive); never overwrites an existing one.
    Returns True if it added."""
    if not name or not (funding_total_usd or last_funding_round):
        return False
    from ..db import DB

    db = DB(Path(path))
    try:
        return db.add_company(
            name=name,
            stock_ticker="",
            stock_price=None,
            stock_price_updated_at="",
            funding_total_usd=funding_total_usd,
            last_funding_round=last_funding_round or "",
            last_funding_date=last_funding_date or "",
            ipo_date="",
            stock_exchange="",
            is_public=0,
            source_url=source_url,
            notes="Auto-extracted by besseleth from a scraped item — verify before trusting.",
            auto_extracted=1,
        )
    finally:
        db.close()


def refresh_stock_prices(path: str | Path) -> list[str]:
    """Fetches current stock prices (free, no API key) for every company
    with a `stock_ticker` set, via Stooq's public CSV quote endpoint.
    Returns a list of log lines describing what happened."""
    from datetime import datetime, timezone

    import requests

    from ..db import DB

    db = DB(Path(path))
    log = []
    try:
        for c in db.companies():
            ticker = c["stock_ticker"]
            if not ticker:
                continue
            try:
                resp = requests.get(
                    "https://stooq.com/q/l/",
                    params={"s": ticker, "f": "sd2t2ohlcv", "h": "", "e": "csv"},
                    timeout=15,
                )
                resp.raise_for_status()
                lines = resp.text.strip().splitlines()
                if len(lines) < 2:
                    log.append(f"{ticker}: no data returned")
                    continue
                row = lines[1].split(",")
                # Columns: Symbol,Date,Time,Open,High,Low,Close,Volume
                close = row[6] if len(row) > 6 else None
                if not close or close == "N/D":
                    log.append(f"{ticker}: ticker not found or market data unavailable")
                    continue
                price = float(close)
                updated_at = datetime.now(timezone.utc).isoformat()
                db.update_company(c["name"], stock_price=price, stock_price_updated_at=updated_at)
                log.append(f"{ticker}: ${price}")
            except Exception as e:
                log.append(f"{ticker}: failed ({e})")
    finally:
        db.close()
    return log
