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

Deliberately NOT fuzzy-matched at insert time: two company names that
are a couple of edits apart are just as often two real, distinct
companies (e.g. "Precision Neuroscience" vs. "Precision Neurotech",
"Neurable" vs. "Neurala") as they are one company typo'd two ways — a
similarity score alone can't tell those apart, so guessing wrong would
silently fold one company's data into another's with no record it
happened. find_possible_duplicate_companies() below surfaces likely
typos (e.g. "Axfot" for "Axoft") for a human to actually look at and
merge — via merge_company_pair() — instead of merging on its own.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

# How close two names need to be to flag them as a *possible* typo of one
# another for a human to review — never used to auto-merge (see module
# docstring). Deliberately conservative: a real false negative here (a
# typo that doesn't get flagged) just means you spot it yourself later;
# a false positive would suggest merging two unrelated companies.
_FUZZY_MATCH_RATIO = 0.78
_FUZZY_MATCH_MAX_LEN_DIFF = 2


def _is_fuzzy_match(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > _FUZZY_MATCH_MAX_LEN_DIFF:
        return False
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() >= _FUZZY_MATCH_RATIO


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


def auto_mark_ipo(path: str | Path, name: str, ipo_date: str, stock_exchange: str) -> None:
    """Records an IPO extracted from a scraped item — adds the company if
    it's new, or fills in ipo_date/stock_exchange on an existing row only
    if not already set (never overwrites a correction). Powers the
    Trends tab's IPO timeline."""
    if not name or not ipo_date:
        return
    from ..db import DB

    db = DB(Path(path))
    try:
        db.set_company_ipo(name, ipo_date, stock_exchange or "")
    finally:
        db.close()


def find_possible_duplicate_companies(path: str | Path) -> list[tuple[str, str, float]]:
    """Flags pairs of stored companies whose names are close enough to be
    a typo of each other (e.g. "Axfot"/"Axoft") — for a human to look at
    and decide, via merge_company_pair() below, NOT an automatic merge:
    see the module docstring for why a similarity score alone can't
    reliably tell a typo apart from two real, similarly-named companies.
    Returns (name_a, name_b, ratio) tuples, highest ratio first."""
    from ..db import DB

    db = DB(Path(path))
    try:
        names = [r["name"] for r in db.companies()]
        candidates = []
        for i, name in enumerate(names):
            for other in names[i + 1 :]:
                if _is_fuzzy_match(name, other):
                    candidates.append((name, other, difflib.SequenceMatcher(None, name.lower(), other.lower()).ratio()))
        candidates.sort(key=lambda c: c[2], reverse=True)
        return candidates
    finally:
        db.close()


def merge_company_pair(path: str | Path, keep_name: str, drop_name: str) -> None:
    """Explicit, human-requested merge — the dashboard calls this when
    you confirm two flagged companies (see find_possible_duplicate_companies)
    are actually the same one. Never called automatically."""
    from ..db import DB

    db = DB(Path(path))
    try:
        db.merge_company(keep_name=keep_name, drop_name=drop_name)
    finally:
        db.close()


def backfill_missing_funding_dates(path: str | Path) -> int:
    """Retroactive one-time fix for a company row that has funding data
    but no last_funding_date/ipo_date (an older enrich.py bug — see
    auto_upsert_company's caller in enrich.py) — without a date on
    *some* field, such a row has table data but nothing to plot on the
    Trends tab's date axis. Backfills from the earliest scraped item
    naming that org, as a best-effort "reported around this date".
    Returns how many rows were filled in."""
    from ..db import DB

    db = DB(Path(path))
    filled = 0
    try:
        for row in db.companies():
            if row["last_funding_date"] or row["ipo_date"] or not row["funding_total_usd"]:
                continue
            earliest = db.earliest_item_date_for_org(row["name"])
            if earliest:
                db.update_company(row["name"], last_funding_date=earliest)
                filled += 1
        return filled
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
