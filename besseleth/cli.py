"""CLI: fetch new items, generate/send the weekly report, or do both.

Usage:
    python -m besseleth.cli fetch          [--config config.yaml] [--since YYYY-MM-DD]
    python -m besseleth.cli report         [--config config.yaml]
    python -m besseleth.cli run            [--config config.yaml] [--since YYYY-MM-DD]
    python -m besseleth.cli paste          [--url URL] [--text "..."]  # one paste box — source auto-detected
    python -m besseleth.cli linkedin-add   [--url URL] [--text "..."]  # force-tag as linkedin instead of auto-detecting
    python -m besseleth.cli event-add      [--url URL] [--text "..."]  # force-tag as event
    python -m besseleth.cli social-add     [--url URL] [--text "..."]  # force-tag as social
    python -m besseleth.cli device-suggest --item-id <id>  # draft a devices.yaml entry from a scraped item
    python -m besseleth.cli company-refresh-stock          # update stock_price for companies.yaml's tickers (free)
    python -m besseleth.cli report-delete <report-id>       # e.g. report-delete 2026-09-01
    python -m besseleth.cli item-delete --item-id <id>       # remove a pasted (or any) item outright
    python -m besseleth.cli enrich [--all]                   # tag arXiv/news/blog items for the Papers table
    python -m besseleth.cli serve          [--config config.yaml]   # run continuously per `schedule` in config.yaml

`serve` is the "regularly updating" mode — start it once (e.g. as a
launchd/systemd service) and it keeps fetching/reporting on the schedule
in config.yaml's `schedule` section forever, no cron needed. Prefer the
browser dashboard (`python -m besseleth.web.app`) if you also want to
*view* things — it runs the same schedule in the background plus serves
the UI, including a paste box and a backfill control. Use `serve` for a
headless box with no browser.

`--since` backfills further back than the configured `days_back` for
each source — useful right after setup, or after being away a while, to
seed history beyond just "the last week".

Alternatively, schedule `run` with system cron, e.g.:
    0 8 * * MON  cd /path/to/besseleth && .venv/bin/python -m besseleth.cli run
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date

from .config import load_config
from .db import DB
from .pipeline import fetch_all, generate_weekly_report
from .scrapers import events_scraper, linkedin_scraper, manual_drop, social_scraper


def _read_text(args) -> str:
    text = args.text
    if text is None:
        print("Paste content below, then press Ctrl-D:")
        text = sys.stdin.read()
    if not text.strip():
        print("[cli] Nothing pasted; aborting.", file=sys.stderr)
        sys.exit(1)
    return text


COMMANDS = [
    "fetch",
    "report",
    "run",
    "paste",
    "linkedin-add",
    "event-add",
    "social-add",
    "device-suggest",
    "company-refresh-stock",
    "report-delete",
    "item-delete",
    "enrich",
    "serve",
]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="besseleth")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("report_id", nargs="?", default=None, help="Report id for report-delete, e.g. 2026-09-01")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--url", default="", help="Source URL for a paste-in command (auto-detected if omitted)")
    parser.add_argument(
        "--text",
        default=None,
        help="Text to ingest for a paste-in command. If omitted, reads from stdin — "
        "so you can pipe or paste content and press Ctrl-D.",
    )
    parser.add_argument("--item-id", default=None, help="Item id to draft a devices.yaml entry from (device-suggest)")
    parser.add_argument(
        "--since", default=None, help="Backfill from this date (YYYY-MM-DD) instead of each source's usual days_back."
    )
    parser.add_argument(
        "--all", action="store_true", help="For `enrich`: process the whole backlog, ignoring max_items_per_run."
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    since = None
    if args.since:
        try:
            since = date.fromisoformat(args.since)
        except ValueError:
            print(f"[cli] --since must be YYYY-MM-DD, got {args.since!r}.", file=sys.stderr)
            sys.exit(1)

    if args.command == "serve":
        from .scheduler import start_scheduler

        scheduler, status = start_scheduler(config)
        if scheduler is None:
            print("[cli] schedule.enabled is false in config.yaml; nothing to serve. Exiting.", file=sys.stderr)
            sys.exit(1)
        print("[cli] Serving. Ctrl-C to stop.")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            scheduler.shutdown()
            print("\n[cli] Stopped.")
        return

    if args.command == "company-refresh-stock":
        from .trends.company_store import refresh_stock_prices

        log = refresh_stock_prices(config.companies_path)
        if not log:
            print("[cli] No companies with a stock_ticker set — add one via the dashboard's Trends tab.")
        for line in log:
            print(f"[cli] {line}")
        return

    if args.command == "enrich":
        from .enrich import enrich_items_detailed

        db = DB(config.db_path)
        try:
            if args.all:
                config.raw.setdefault("enrichment", {})["max_items_per_run"] = 1_000_000
            result = enrich_items_detailed(config, db)
            print(f"[cli] {result['message']}")
        finally:
            db.close()
        return

    if args.command == "item-delete":
        if not args.item_id:
            print("[cli] Usage: besseleth.cli item-delete --item-id <id>", file=sys.stderr)
            sys.exit(1)
        db = DB(config.db_path)
        try:
            if db.delete_item(args.item_id):
                print(f"[cli] Deleted item {args.item_id}.")
            else:
                print(f"[cli] No item found with id {args.item_id!r}.", file=sys.stderr)
                sys.exit(1)
        finally:
            db.close()
        return

    if args.command == "report-delete":
        from pathlib import Path

        if not args.report_id:
            print("[cli] Usage: besseleth.cli report-delete <report-id>", file=sys.stderr)
            sys.exit(1)
        path = Path(config.report.get("output_dir", "reports")) / f"report-{args.report_id}.md"
        if not path.exists():
            print(f"[cli] No report found at {path}.", file=sys.stderr)
            sys.exit(1)
        path.unlink()
        print(f"[cli] Deleted {path}.")
        return

    db = DB(config.db_path)
    try:
        if args.command in ("fetch", "run"):
            results = fetch_all(config, db, since=since)
            total = sum(len(v) for v in results.values())
            print(f"[cli] Fetched {total} new items: " + ", ".join(f"{k}={len(v)}" for k, v in results.items()))
        if args.command in ("report", "run"):
            generate_weekly_report(config, db)

        if args.command == "paste":
            item, detected = manual_drop.add_smart_item(config, db, _read_text(args), url=args.url)
            print(f"[cli] Added as {detected}: {item.title!r} (id={item.id})")
        if args.command == "linkedin-add":
            item = linkedin_scraper.add_manual_item(config, db, _read_text(args), url=args.url)
            print(f"[cli] Added: {item.title!r} (id={item.id})")
        if args.command == "event-add":
            item = events_scraper.add_manual_item(config, db, _read_text(args), url=args.url)
            print(f"[cli] Added: {item.title!r} (id={item.id})")
        if args.command == "social-add":
            item = social_scraper.add_manual_item(config, db, _read_text(args), url=args.url)
            print(f"[cli] Added: {item.title!r} (id={item.id})")

        if args.command == "device-suggest":
            from .trends.store import suggest_from_item
            from .db import Item

            if not args.item_id:
                print("[cli] --item-id is required for device-suggest (see item ids in the DB or report).", file=sys.stderr)
                sys.exit(1)
            db.conn.row_factory = None
            row = db.conn.execute(
                "SELECT id, source, title, url, summary, published_at FROM items WHERE id = ?", (args.item_id,)
            ).fetchone()
            if not row:
                print(f"[cli] No item found with id {args.item_id!r}.", file=sys.stderr)
                sys.exit(1)
            item = Item(id=row[0], source=row[1], title=row[2], url=row[3] or "", summary=row[4] or "", published_at=row[5] or "")
            draft = suggest_from_item(item, config.trend_metrics, config.summarizer)
            if draft:
                print(f"\n# Draft for devices.yaml (review before adding!):\n{draft}\n")
            else:
                print("[cli] No metrics/FDA status found in this item, or Ollama unavailable.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
