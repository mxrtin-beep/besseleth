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
pipeline. Meant for local/personal use; no auth by default (fine on your
own Mac, or over Tailscale to your phone), but set BESSELETH_AUTH_USER/
BESSELETH_AUTH_PASSWORD (env vars, never config.yaml — that's tracked in
git) before putting this behind a public tunnel (ngrok, Cloudflare
Tunnel, etc.) — see _require_auth() below.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import threading
from datetime import date
from pathlib import Path

import markdown as md
from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory

from ..config import Config, load_config
from ..contacts_store import Contact, add_contact, import_linkedin_csv, load_contacts, remove_contact, update_contact
from ..db import DB
from ..feeds_store import add_feed, load_feeds, remove_feed
from ..interests_store import load_interests, save_interests
from ..pipeline import fetch_all
from ..scheduler import SchedulerStatus, run_now, start_scheduler
from ..scrapers.manual_drop import add_smart_item
from ..trends.company_store import find_possible_duplicate_companies, load_companies, merge_company_pair
from ..trends.fda_stages import FDA_STAGES, stage_for
from ..trends.store import load_devices


def _require_auth() -> "Response | None":
    """HTTP Basic Auth, gated entirely on two env vars — unset either one
    (the default) and this is a no-op, so a plain local/Tailscale setup
    is unaffected. Set BOTH before exposing the dashboard any other way
    (a public tunnel, a machine other people can reach):

        export BESSELETH_AUTH_USER="you"
        export BESSELETH_AUTH_PASSWORD="something-only-you-know"

    Deliberately not a config.yaml setting — that file is tracked in
    git now (see an earlier commit), and a password has no business in
    version control. secrets.compare_digest avoids leaking the correct
    password one character at a time via response-time differences.
    Returning a Response (rather than calling abort()) from a
    before_request hook is what tells Flask to use it as the reply and
    skip the view entirely — returning None proceeds as normal."""
    user = os.environ.get("BESSELETH_AUTH_USER")
    password = os.environ.get("BESSELETH_AUTH_PASSWORD")
    if not user or not password:
        return None
    auth = request.authorization
    valid = (
        auth is not None
        and secrets.compare_digest(auth.username, user)
        and secrets.compare_digest(auth.password, password)
    )
    if valid:
        return None
    return Response("Authentication required.", 401, {"WWW-Authenticate": 'Basic realm="besseleth"'})


def create_app(config: Config, status: SchedulerStatus | None = None) -> Flask:
    app = Flask(__name__)
    app.config["BESSELETH_CONFIG"] = config
    app.config["BESSELETH_STATUS"] = status or SchedulerStatus(enabled=False)
    app.before_request(_require_auth)

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
        devices = load_devices(config.devices_path, config.legacy_devices_yaml_path)
        result = []
        for d in devices:
            stage_rank, stage_label = stage_for(d.fda_status)
            result.append(
                {
                    "name": d.name,
                    "org": d.org,
                    "org_type": d.org_type,
                    "fda_status": d.fda_status or "unknown",
                    "fda_stage_rank": stage_rank,
                    "fda_stage_label": stage_label,
                    "metrics": d.metrics,
                    "source_url": d.source_url,
                    "date_reported": d.date_reported,
                    "notes": d.notes,
                    "auto_extracted": d.auto_extracted,
                }
            )
        return jsonify(result)

    @app.get("/api/trends/fda-stages")
    def api_fda_stages():
        # The canonical stage ladder itself (rank + label), for the
        # Trends tab's FDA timeline to build a shared, ordered Y axis
        # from — see trends/fda_stages.py for what each rung means.
        return jsonify([{"rank": rank, "label": label} for rank, label in FDA_STAGES])

    @app.get("/api/companies")
    def api_companies():
        companies = load_companies(config.companies_path, config.legacy_companies_yaml_path)
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
                    "ipo_date": c.ipo_date,
                    "stock_exchange": c.stock_exchange,
                    "is_public": c.is_public,
                    "source_url": c.source_url,
                    "notes": c.notes,
                    "auto_extracted": c.auto_extracted,
                }
                for c in companies
            ]
        )

    @app.get("/api/companies/possible-duplicates")
    def api_possible_duplicate_companies():
        # Flagged, never auto-merged — see company_store's module
        # docstring for why a name-similarity score alone can't be
        # trusted to decide two companies are the same one.
        pairs = find_possible_duplicate_companies(config.companies_path)
        return jsonify([{"a": a, "b": b, "ratio": round(ratio, 2)} for a, b, ratio in pairs])

    @app.post("/api/companies/merge")
    def api_merge_companies():
        payload = request.get_json(force=True) or {}
        keep, drop = payload.get("keep"), payload.get("drop")
        if not keep or not drop:
            return jsonify({"ok": False, "message": "Both 'keep' and 'drop' are required."}), 400
        merge_company_pair(config.companies_path, keep_name=keep, drop_name=drop)
        return jsonify({"ok": True})

    @app.get("/api/jobs")
    def api_jobs():
        db = DB(config.db_path)
        try:
            rows = db.job_postings()
        finally:
            db.close()
        return jsonify(
            [
                {
                    "id": r["id"],
                    "org": r["org"],
                    "platform": r["platform"],
                    "title": r["title"],
                    "url": r["url"],
                    "location": r["location"],
                    "first_seen_at": r["first_seen_at"],
                    "last_seen_at": r["last_seen_at"],
                    "removed_at": r["removed_at"],
                }
                for r in rows
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
                config.raw.get("enrichment", {}).get(
                    "sources", ["papers", "news", "blog", "linkedin", "social", "event", "clip"]
                )
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
                    "authors": r["authors"],
                    "citation_count": r["citation_count"],
                    "enriched": r["enriched_at"] is not None,
                }
                for r in rows
            ]
        )

    @app.get("/api/orgs")
    def api_orgs():
        # Every org besseleth has ever extracted, not just the ones that
        # geocoded (that subset is /api/locations, for the map). Enriched
        # with funding/stock data from the companies table where the
        # names match, so this one table covers labs, academic/gov orgs,
        # and funded companies alike.
        db = DB(config.db_path)
        try:
            rows = db.orgs()
        finally:
            db.close()
        companies_by_name = {c.name.lower(): c for c in load_companies(config.companies_path, config.legacy_companies_yaml_path)}
        result = []
        for r in rows:
            company = companies_by_name.get((r["org"] or "").lower())
            result.append(
                {
                    "org": r["org"],
                    "org_description": r["org_description"],
                    "source_url": r["source_url"],
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

    @app.get("/api/contacts/locations")
    def api_contacts_locations():
        # Where your contacts work, for the Map tab's "friends" layer —
        # reuses org_location_cache (see enrich.py's
        # _backfill_contact_locations), so this is a cache read, not a
        # live lookup: fast, and never blocks a page load on a network
        # call. A contact whose employer hasn't been resolved yet (new
        # contact, or the next enrich run hasn't reached it) is just
        # skipped rather than shown with no location.
        db = DB(config.db_path)
        try:
            points = []
            for contact in config.contacts:
                workplaces = contact.get("workplaces") or []
                if not workplaces or not workplaces[0].get("company"):
                    continue
                company = workplaces[0]["company"]
                cached = db.get_org_location_cache(company)
                if not cached or not cached["found"]:
                    continue
                points.append(
                    {
                        "name": contact.get("name"),
                        "company": company,
                        "role": workplaces[0].get("role", ""),
                        "location_text": cached["location_text"],
                        "lat": cached["lat"],
                        "lon": cached["lon"],
                    }
                )
        finally:
            db.close()
        return jsonify(points)

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
        # last_fetch_at/last_report_at are in-memory on SchedulerStatus,
        # so they reset to "never" on every restart and never reflect
        # activity from a CLI-only workflow (cli fetch/report never touch
        # this in-process object). Fall back to the persisted DB meta —
        # written by pipeline.py's fetch_all()/generate_weekly_report()
        # regardless of caller — whenever the in-memory value is unset,
        # so the status bar reflects real history, not just this
        # process's own uptime.
        data = app.config["BESSELETH_STATUS"].as_dict()
        if not data.get("last_fetch_at") or not data.get("last_report_at"):
            db = DB(config.db_path)
            try:
                data.setdefault("last_fetch_at", None)
                data.setdefault("last_report_at", None)
                if not data["last_fetch_at"]:
                    data["last_fetch_at"] = db.get_meta("last_fetch_at")
                if not data["last_report_at"]:
                    data["last_report_at"] = db.get_meta("last_report_at")
            finally:
                db.close()
        return jsonify(data)

    @app.post("/api/run-now")
    def api_run_now():
        status = app.config["BESSELETH_STATUS"]
        if status.running_now:
            return jsonify({"ok": False, "message": "Already running."}), 409
        # Runs in a background thread — fetching+summarizing can take a
        # while (LLM calls, network) — so this returns immediately and the
        # dashboard polls /api/status for progress_label/current/total and
        # running_now instead of blocking on the request.
        with status._lock:
            status.running_now = True
        status.set_progress("Starting run...")

        def _work():
            try:
                run_now(config, status)
            finally:
                status.set_progress(None)
                with status._lock:
                    status.running_now = False

        threading.Thread(target=_work, daemon=True).start()
        return jsonify({"ok": True, "message": "Started."})

    @app.post("/api/enrich")
    def api_enrich():
        from ..enrich import enrich_items_detailed

        status = app.config["BESSELETH_STATUS"]
        if status.running_now:
            return jsonify({"ok": False, "message": "Already running."}), 409
        payload = request.get_json(silent=True) or {}
        force = bool(payload.get("force"))
        run_until_done = bool(payload.get("run_until_done"))
        # Runs in a background thread, same as /api/run-now — with
        # run_until_done this can take a long while (the whole backlog,
        # not one capped batch), so the dashboard polls /api/status rather
        # than the request blocking until it's actually done.
        with status._lock:
            status.running_now = True
        status.set_progress("Starting enrichment...")

        def _work():
            try:
                db = DB(config.db_path)
                try:
                    result = enrich_items_detailed(
                        config, db, force=force, run_until_done=run_until_done,
                        progress_cb=status.set_progress,
                    )
                finally:
                    db.close()
                with status._lock:
                    status.last_error = None
                status.last_enrich_result = {
                    "ok": True, "enriched": result["processed"], "message": result["message"],
                    "backend": result["backend"], "stats": result.get("stats"),
                }
            except Exception as e:
                with status._lock:
                    status.last_error = f"enrich: {e}"
            finally:
                status.set_progress(None)
                with status._lock:
                    status.running_now = False

        threading.Thread(target=_work, daemon=True).start()
        return jsonify({"ok": True, "message": "Started."})

    @app.get("/api/enrich/stats")
    def api_enrich_stats():
        # Read-only — for the dashboard to show "enriched N items so far,
        # avg Xs/item" on page load, without triggering a run.
        db = DB(config.db_path)
        try:
            stats = db.get_enrich_stats()
        finally:
            db.close()
        return jsonify(stats)

    @app.get("/api/enrich/log")
    def api_enrich_log():
        # Troubleshooting view: the most recently enriched items with
        # exactly what got extracted, plus the live config/backend status
        # — so "why is everything null/unknown" is answerable by looking
        # at this tab instead of reading server logs or config.yaml by
        # hand. Read-only, no run triggered.
        from ..enrich import ollama_status

        summarizer_cfg = config.summarizer
        backend = summarizer_cfg.get("backend", "none")
        if backend == "ollama":
            ok, ollama_message = ollama_status(summarizer_cfg)
        else:
            ok, ollama_message = False, ""

        db = DB(config.db_path)
        try:
            rows = db.recently_enriched(limit=50)
            stats = db.get_enrich_stats()
        finally:
            db.close()

        return jsonify({
            "backend": backend,
            "model": summarizer_cfg.get("model", "llama3.1"),
            "ollama_url": summarizer_cfg.get("ollama_url", "http://localhost:11434"),
            "ollama_ok": ok,
            "ollama_message": ollama_message,
            "stats": stats,
            "items": [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "source": r["source"],
                    "url": r["url"],
                    "enriched_at": r["enriched_at"],
                    "org": r["org"],
                    "org_type": r["org_type"],
                    "modality": r["modality"],
                    "therapeutic_target": r["therapeutic_target"],
                    "novelty_score": r["novelty_score"],
                    "novelty_rationale": r["novelty_rationale"],
                    "location_text": r["location_text"],
                }
                for r in rows
            ],
        })

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
        status.set_progress("Starting backfill...")

        def _work():
            try:
                db = DB(config.db_path)
                try:
                    results = fetch_all(config, db, since=since, progress_cb=status.set_progress)
                finally:
                    db.close()
                with status._lock:
                    status.last_error = None
                status.last_fetch_counts = {k: len(v) for k, v in results.items()}
            except Exception as e:
                with status._lock:
                    status.last_error = f"backfill: {e}"
            finally:
                status.set_progress(None)
                with status._lock:
                    status.running_now = False

        threading.Thread(target=_work, daemon=True).start()
        return jsonify({"ok": True, "since": since_str, "message": "Started."})

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

    @app.get("/api/feeds")
    def api_feeds():
        return jsonify(load_feeds(config.feeds_path))

    @app.post("/api/feeds")
    def api_add_feed():
        payload = request.get_json(silent=True) or {}
        category = payload.get("category", "")
        url = (payload.get("url") or "").strip()
        label = (payload.get("label") or "").strip()
        if category not in ("news", "blog"):
            return jsonify({"ok": False, "message": "category must be 'news' or 'blog'."}), 400
        if not url.startswith(("http://", "https://")):
            return jsonify({"ok": False, "message": "URL must start with http:// or https://."}), 400

        added = add_feed(config.feeds_path, category, url, label)
        if not added:
            return jsonify({"ok": False, "message": "That feed URL is already in the list."}), 409

        # Best-effort validation, after adding: a feed that fails to parse
        # right now might just be a transient fetch error, not a bad URL
        # (some sites block server-side/non-browser requests intermittently),
        # so this warns rather than rejecting the submission outright — it
        # stays in the list either way and gets retried on the next fetch.
        warning = None
        try:
            import feedparser

            parsed = feedparser.parse(url)
            if not parsed.entries:
                warning = "Added, but couldn't find any entries in it just now — double-check the URL, or it may just be temporarily empty/unreachable."
        except Exception:
            warning = "Added, but couldn't validate it just now — it'll still be tried on the next fetch."

        return jsonify({"ok": True, "warning": warning})

    @app.delete("/api/feeds")
    def api_remove_feed():
        payload = request.get_json(silent=True) or {}
        category = payload.get("category", "")
        url = (payload.get("url") or "").strip()
        if category not in ("news", "blog"):
            return jsonify({"ok": False, "message": "category must be 'news' or 'blog'."}), 400
        removed = remove_feed(config.feeds_path, category, url)
        if not removed:
            abort(404)
        return jsonify({"ok": True})

    @app.get("/api/interests")
    def api_interests():
        return jsonify(load_interests(config.interests_path))

    @app.post("/api/interests")
    def api_save_interests():
        payload = request.get_json(force=True) or {}
        interests = payload.get("interests") or []
        if not isinstance(interests, list):
            return jsonify({"ok": False, "message": "'interests' must be a list of strings."}), 400
        save_interests(interests, config.interests_path)
        return jsonify({"ok": True, "interests": load_interests(config.interests_path)})

    @app.get("/api/contacts")
    def api_contacts():
        contacts = load_contacts(config.contacts_path)
        return jsonify(
            [
                {
                    "index": i, "name": c.name, "emails": c.emails, "linkedin_url": c.linkedin_url,
                    "workplaces": c.workplaces, "schools": c.schools,
                    "relationship": c.relationship, "notes": c.notes,
                }
                for i, c in enumerate(contacts)
            ]
        )

    def _parse_lines(text: str, key_a: str, key_b: str) -> list[dict]:
        """One entry per non-blank line, "Value — Detail" (em dash, or a
        plain hyphen) splitting into {key_a: Value, key_b: Detail} — the
        detail half is optional (a line with no separator just gets an
        empty key_b). Used for the Workplaces ("Company — Role") and
        Schools ("School — Level") textareas."""
        entries = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = re.split(r"\s*[—-]\s*", line, maxsplit=1)
            value = parts[0].strip()
            detail = parts[1].strip() if len(parts) > 1 else ""
            if value:
                entries.append({key_a: value, key_b: detail})
        return entries

    def _contact_from_payload(payload: dict) -> Contact | None:
        name = (payload.get("name") or "").strip()
        if not name:
            return None
        emails = [e.strip() for e in (payload.get("emails") or "").split(",") if e.strip()]
        return Contact(
            name=name,
            emails=emails,
            linkedin_url=(payload.get("linkedin_url") or "").strip(),
            workplaces=_parse_lines(payload.get("workplaces", ""), "company", "role"),
            schools=_parse_lines(payload.get("schools", ""), "name", "level"),
            relationship=(payload.get("relationship") or "").strip(),
            notes=(payload.get("notes") or "").strip(),
        )

    @app.post("/api/contacts")
    def api_add_contact():
        payload = request.get_json(silent=True) or {}
        contact = _contact_from_payload(payload)
        if not contact:
            return jsonify({"ok": False, "message": "Name is required."}), 400
        add_contact(config.contacts_path, contact)
        return jsonify({"ok": True})

    @app.put("/api/contacts/<int:index>")
    def api_update_contact(index):
        payload = request.get_json(silent=True) or {}
        contact = _contact_from_payload(payload)
        if not contact:
            return jsonify({"ok": False, "message": "Name is required."}), 400
        if not update_contact(config.contacts_path, index, contact):
            abort(404)
        return jsonify({"ok": True})

    @app.delete("/api/contacts/<int:index>")
    def api_remove_contact(index):
        if not remove_contact(config.contacts_path, index):
            abort(404)
        return jsonify({"ok": True})

    @app.post("/api/contacts/import-linkedin")
    def api_import_linkedin():
        # LinkedIn has no API for pulling your connections into a third-
        # party app — this reads the CSV LinkedIn itself lets you export
        # (Settings & Privacy -> Data privacy -> Get a copy of your data
        # -> Connections). Not live sync, but a real bulk import.
        uploaded = request.files.get("file")
        if not uploaded:
            return jsonify({"ok": False, "message": "No file uploaded."}), 400
        try:
            text = uploaded.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return jsonify({"ok": False, "message": "Couldn't read that file as text — is it the CSV LinkedIn emailed you?"}), 400
        # Your LinkedIn export is your whole network, not just neurotech —
        # only import connections whose company/title look on-topic. Uses
        # both the industry keyword list (for a title like "EEG Research
        # Scientist" at an otherwise generic-sounding employer) AND every
        # org name besseleth has already seen in your actual feeds (so a
        # company like "Neuralink" gets recognized even though its name
        # doesn't literally contain a keyword phrase like "neurotechnology").
        from ..trends.company_store import load_companies

        db = DB(config.db_path)
        try:
            known_orgs = db.distinct_orgs()
        finally:
            db.close()
        # Also fold in the Trends tab's curated company list (companies
        # you're tracking funding/stock for) — a flagship name like
        # "Neuralink" or "Blackrock Neurotech" is very likely already
        # there even if no scraped item happens to have mentioned it yet.
        known_orgs += [c.name for c in load_companies(config.db_path, config.companies_path)]
        added = import_linkedin_csv(config.contacts_path, text, keywords=config.keywords, known_orgs=known_orgs)
        if added == 0:
            return jsonify({
                "ok": True, "added": 0,
                "message": (
                    "Found 0 new contacts — either everyone relevant is already added, this doesn't look "
                    f"like a LinkedIn Connections.csv export, or nobody in it looks like {config.industry_name}."
                ),
            })
        return jsonify({"ok": True, "added": added, "message": f"Added {added} new contact(s) working in {config.industry_name}."})

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
