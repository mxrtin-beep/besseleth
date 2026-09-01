"""CLI: fetch new items, generate/send the weekly report, or do both.

Usage:
    python -m besseleth.cli fetch  [--config config.yaml]
    python -m besseleth.cli report [--config config.yaml]
    python -m besseleth.cli run    [--config config.yaml]   # fetch + report

Schedule `run` weekly with cron, e.g.:
    0 8 * * MON  cd /path/to/besseleth && .venv/bin/python -m besseleth.cli run
"""
from __future__ import annotations

import argparse
import sys

from .config import load_config
from .db import DB
from .pipeline import fetch_all, generate_weekly_report


def main(argv=None):
    parser = argparse.ArgumentParser(prog="besseleth")
    parser.add_argument("command", choices=["fetch", "report", "run"])
    parser.add_argument("--config", default=None, help="Path to config.yaml")
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
    finally:
        db.close()


if __name__ == "__main__":
    main()
