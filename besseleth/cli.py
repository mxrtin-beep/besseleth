"""CLI: fetch new items, generate/send the weekly report, or do both.

Usage:
    python -m besseleth.cli fetch          [--config config.yaml]
    python -m besseleth.cli report         [--config config.yaml]
    python -m besseleth.cli run            [--config config.yaml]   # fetch + report
    python -m besseleth.cli linkedin-add   [--url URL] [--text "..."]  # paste LinkedIn content in
    python -m besseleth.cli event-add      [--url URL] [--text "..."]  # paste a Luma/Eventbrite/Meetup page in
    python -m besseleth.cli social-add     [--url URL] [--text "..."]  # paste a tweet/post in
    python -m besseleth.cli device-suggest --item-id <id>  # draft a devices.yaml entry from a scraped item

Schedule `run` weekly with cron, e.g.:
    0 8 * * MON  cd /path/to/besseleth && .venv/bin/python -m besseleth.cli run
"""
from __future__ import annotations

import argparse
import sys

from .config import load_config
from .db import DB
from .pipeline import fetch_all, generate_weekly_report
from .scrapers import events_scraper, linkedin_scraper, social_scraper


def _read_text(args) -> str:
    text = args.text
    if text is None:
        print("Paste content below, then press Ctrl-D:")
        text = sys.stdin.read()
    if not text.strip():
        print("[cli] Nothing pasted; aborting.", file=sys.stderr)
        sys.exit(1)
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(prog="besseleth")
    parser.add_argument(
        "command",
        choices=["fetch", "report", "run", "linkedin-add", "event-add", "social-add", "device-suggest"],
    )
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--url", default="", help="Source URL for a paste-in command (auto-detected if omitted)")
    parser.add_argument(
        "--text",
        default=None,
        help="Text to ingest for a paste-in command. If omitted, reads from stdin — "
        "so you can pipe or paste content and press Ctrl-D.",
    )
    parser.add_argument("--item-id", default=None, help="Item id to draft a devices.yaml entry from (device-suggest)")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    db = DB(config.db_path)
    try:
        if args.command in ("fetch", "run"):
            results = fetch_all(config, db)
            total = sum(len(v) for v in results.values())
            print(f"[cli] Fetched {total} new items: " + ", ".join(f"{k}={len(v)}" for k, v in results.items()))
        if args.command in ("report", "run"):
            generate_weekly_report(config, db)

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
