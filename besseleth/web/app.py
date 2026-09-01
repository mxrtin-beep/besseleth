"""A small local Flask app: browse weekly reports, explore the
industry-trends dataset with an interactive, adjustable-axis chart
(Plotly, client-side), paste anything (LinkedIn/social/events/whatever —
auto-classified), and — by default — keeps itself updated on a schedule
(see besseleth/scheduler.py) so this is a standing service, not a
one-shot command.

Run with:
    .venv/bin/python -m besseleth.web.app [--config config.yaml] [--port 5050]

Everything here reads the same config.yaml / devices.yaml / companies.yaml
/ reports/ / data/besseleth.db that the CLI writes — this is a viewer
(plus the scheduler and the paste box), not a second copy of the
pipeline. It's meant for local/personal use (no auth); don't expose it on
the open internet as-is.
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import markdown as md
from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from ..config import Config, load_config
from ..db import DB
from ..pipeline import fetch_all
from ..scheduler import SchedulerStatus, run_now, start_scheduler
from ..scrapers.manual_drop import add_smart_item
from ..trends.company_store import load_companies
from ..trends.store import load_devices


def create_app(config: Config, status: SchedulerStatus | None = None) -> Flask:
    app = Flask(__name__)
    app.config["BESSELETH_CONFIG"] = config
    app.config["BESSELETH_STATUS"] = status or SchedulerStatus(enabled=False)

    reports_dir = Path(config.report.get("output_dir", "reports"))

    @app.get("/favicon.ico")
    def favicon():
        # Safari fetches /favicon.ico directly for the tab icon and
        # ignores the <link rel="icon"> tag in dashboard.html if this
        # 404s — serve the same PNG from here too (browsers sniff content,
        # not the extension) so the tab icon shows up there as well.
        return send_from_directory(Path(app.static_folder), "favicon.png", mimetype="image/png")

    @app.get("/")
    def index():
        reports = sorted(reports_dir.glob("report-*.md"), reverse=True)
        report_ids = [p.stem.removeprefix("report-") for p in reports]
        return render_template(
            "dashboard.html",
            industry=config.industry_name,
            report_ids=report_ids,
            trend_metrics=config.trend_metrics,
        )

    @app.get("/api/report/<report_id>")
    def api_report(report_id):
        path = reports_dir / f"report-{report_id}.md"
        if not path.exists():
            abort(404)
        html = md.markdown(path.read_text(), extensions=["tables"])
        return jsonify({"report_id": report_id, "html": html})

    @app.delete("/api/report/<report_id>")
    def api_delete_report(report_id):
        path = reports_dir / f"report-{report_id}.md"
        if not path.exists():
            abort(404)
        path.unlink()
        return jsonify({"ok": True, "deleted": report_id})

    @app.get("/api/devices")
    def api_devices():
        devices = load_devices(config.devices_path)
        return jsonify(
            [
                {
                    "name": d.name,
                    "org": d.org,
                    "org_type": d.org_type,
                    "fda_status": d.fda_status or "unknown",
                    "metrics": d.metrics,
                    "source_url": d.source_url,
                    "date_reported": d.date_reported,
                    "notes": d.notes,
                    "auto_extracted": d.auto_extracted,
                }
                for d in devices
            ]
        )

    @app.get("/api/companies")
    def api_companies():
        companies = load_companies(config.companies_path)
        return jsonify(
            [
                {
                    "name": c.name,
                    "stock_ticker": c.stock_ticker,
                    "stock_price": c.stock_price,
                    "stock_price_updated_at": c.stock_price_updated_at,
                    "funding_total_usd": c.funding_total_usd,
                    "last_funding_round": c.last_funding_round,
                    "last_funding_date": c.last_funding_date,
                    "source_url": c.source_url,
                    "notes": c.notes,
                    "auto_extracted": c.auto_extracted,
                }
                for c in companies
            ]
        )

    @app.get("/api/papers")
    def api_papers():
        # Renamed "Sources" in the UI — this used to default to just
        # arxiv/news/blog, which silently hid manually-pasted LinkedIn/
        # social/event clips from the table entirely. Now it shows every
        # source by default; the source filter dropdown narrows it down
        # to just arXiv (or whatever) if that's all you want.
        db = DB(config.db_path)
        try:
            rows = db.papers(
                config.raw.get("enrichment", {}).get("sources", ["arxiv", "news", "blog", "linkedin", "social", "event", "clip"])
            )
        finally:
            db.close()
        return jsonify(
            [
                {
                    "id": r["id"],
                    "source": r["source"],
                    "title": r["title"],
                    "url": r["url"],
                    "published_at": r["published_at"],
                    "matched_keywords": (r["matched_keywords"] or "").split(",") if r["matched_keywords"] else [],
                    "org": r["org"],
                    "org_type": r["org_type"],
                    "modality": r["modality"],
                    "therapeutic_target": r["therapeutic_target"],
                    "novelty_score": r["novelty_score"],
                    "novelty_rationale": r["novelty_rationale"],
                    "enriched": r["enriched_at"] is not None,
                }
                for r in rows
            ]
        )

    @app.get("/api/orgs")
    def api_orgs():
        # Every org besseleth has ever extracted, not just the ones that
        # geocoded (that subset is /api/locations, for the map). Enriched
        # with funding/stock data from companies.yaml where the names
        # match, so this one table covers labs, academic/gov orgs, and
        # funded companies alike.
        db = DB(config.db_path)
        try:
            rows = db.orgs()
        finally:
            db.close()
        companies_by_name = {c.name.lower(): c for c in load_companies(config.companies_path)}
        result = []
        for r in rows:
            company = companies_by_name.get((r["org"] or "").lower())
            result.append(
                {
                    "org": r["org"],
                    "org_type": r["org_type"],
                    "location_text": r["location_text"],
                    "lat": r["lat"],
                    "lon": r["lon"],
                    "item_count": r["n"],
                    "sources": (r["sources"] or "").split(","),
                    "stock_ticker": company.stock_ticker if company else None,
                    "funding_total_usd": company.funding_total_usd if company else None,
                    "last_funding_round": company.last_funding_round if company else None,
                }
            )
        return jsonify(result)

    @app.get("/api/locations")
    def api_locations():
        db = DB(config.db_path)
        try:
            rows = db.locations()
        finally:
            db.close()
        # Aggregate the per-(org, source) rows from db.locations() into
        # one marker per org — the Map tab shows one point per
        # company/lab, not one per source.
        by_org: dict[tuple, dict] = {}
        for r in rows:
            key = (r["org"], r["lat"], r["lon"])
            entry = by_org.setdefault(
                key,
                {
                    "org": r["org"],
                    "location_text": r["location_text"],
                    "lat": r["lat"],
                    "lon": r["lon"],
                    "org_type": r["org_type"],
                    "total": 0,
                    "by_source": {},
                },
            )
            entry["total"] += r["n"]
            entry["by_source"][r["source"]] = entry["by_source"].get(r["source"], 0) + r["n"]
        return jsonify(list(by_org.values()))

    @app.get("/api/metrics")
    def api_metrics():
        # Axis options for the trend explorer: config-defined numeric
        # metrics plus the built-in "date_reported" time axis. Includes
        # both the device and company metric sets — the dashboard picks
        # whichever matches the selected dataset.
        numeric = [m for m in config.trend_metrics if m.get("type", "numeric") == "numeric"]
        categorical = [m for m in config.trend_metrics if m.get("type") == "categorical"]
        company_numeric = [m for m in config.company_metrics if m.get("type", "numeric") == "numeric"]
        return jsonify(
            {
                "numeric": numeric,
                "categorical": categorical,
                "company_numeric": company_numeric,
                "time_axis": {"key": "date_reported", "label": "Date reported", "unit": ""},
            }
        )

    @app.get("/reports/<path:filename>")
    def report_assets(filename):
        # Serves matplotlib PNGs etc. referenced by older report renders,
        # and anything else saved under the report output dir.
        return send_from_directory(reports_dir, filename)

    @app.get("/api/status")
    def api_status():
        return jsonify(app.config["BESSELETH_STATUS"].as_dict())

    @app.post("/api/run-now")
    def api_run_now():
        status = app.config["BESSELETH_STATUS"]
        if status.running_now:
            return jsonify({"ok": False, "message": "Already running."}), 409
        # Runs synchronously in the request thread — fetching+summarizing
        # can take a while (LLM calls, network), so this blocks until
        # done rather than pretending it's instant. The dashboard shows
        # a spinner for this; poll /api/status if you'd rather not wait.
        run_now(config, status)
        return jsonify(status.as_dict())

    @app.post("/api/enrich")
    def api_enrich():
        from ..enrich import enrich_items_detailed

        db = DB(config.db_path)
        try:
            result = enrich_items_detailed(config, db)
        finally:
            db.close()
        return jsonify({"ok": True, "enriched": result["processed"], "message": result["message"], "backend": result["backend"]})

    @app.post("/api/backfill")
    def api_backfill():
        status = app.config["BESSELETH_STATUS"]
        if status.running_now:
            return jsonify({"ok": False, "message": "Already running."}), 409
        payload = request.get_json(silent=True) or {}
        since_str = payload.get("since", "")
        try:
            since = date.fromisoformat(since_str)
        except ValueError:
            return jsonify({"ok": False, "message": f"Invalid date {since_str!r}, expected YYYY-MM-DD."}), 400

        with status._lock:
            status.running_now = True
        try:
            db = DB(config.db_path)
            try:
                results = fetch_all(config, db, since=since)
            finally:
                db.close()
            counts = {k: len(v) for k, v in results.items()}
            return jsonify({"ok": True, "since": since_str, "counts": counts})
        finally:
            with status._lock:
                status.running_now = False

    @app.post("/api/paste")
    def api_paste():
        payload = request.get_json(silent=True) or {}
        text = (payload.get("text") or "").strip()
        url = payload.get("url", "")
        if not text:
            return jsonify({"ok": False, "message": "Nothing pasted."}), 400
        db = DB(config.db_path)
        try:
            item, detected_label = add_smart_item(config, db, text, url=url)
        finally:
            db.close()
        return jsonify({"ok": True, "title": item.title, "id": item.id, "detected_as": detected_label})

    @app.get("/api/pasted")
    def api_pasted():
        db = DB(config.db_path)
        try:
            rows = db.manual_items(["linkedin", "event", "social", "clip"])
        finally:
            db.close()
        return jsonify(
            [
                {
                    "id": r["id"],
                    "source": r["source"],
                    "title": r["title"],
                    "url": r["url"],
                    "summary": r["summary"],
                    "fetched_at": r["fetched_at"],
                    "included_in_report": r["included_in_report"],
                }
                for r in rows
            ]
        )

    @app.delete("/api/item/<item_id>")
    def api_delete_item(item_id):
        db = DB(config.db_path)
        try:
            deleted = db.delete_item(item_id)
        finally:
            db.close()
        if not deleted:
            abort(404)
        return jsonify({"ok": True, "deleted": item_id})

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(prog="besseleth.web")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--no-schedule", action="store_true", help="Don't start the background fetch/report schedule; serve-only."
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.no_schedule:
        config.raw.setdefault("schedule", {})["enabled"] = False
    _scheduler, status = start_scheduler(config)

    app = create_app(config, status)
    print(f"[web] Serving {config.industry_name} dashboard at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
