"""A small local Flask app: browse weekly reports, and explore the
industry-trends dataset with an interactive, adjustable-axis chart
(Plotly, client-side) instead of the static matplotlib PNGs.

Run with:
    .venv/bin/python -m besseleth.web.app [--config config.yaml] [--port 5050]

Everything here reads the same config.yaml / devices.yaml / reports/ /
data/besseleth.db that the CLI writes — this is a viewer, not a second
copy of the pipeline. It's meant for local/personal use (no auth); don't
expose it on the open internet as-is.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import markdown as md
from flask import Flask, abort, jsonify, render_template, send_from_directory

from ..config import Config, load_config
from ..trends.store import load_devices


def create_app(config: Config) -> Flask:
    app = Flask(__name__)
    app.config["BESSELETH_CONFIG"] = config

    reports_dir = Path(config.report.get("output_dir", "reports"))

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
                }
                for d in devices
            ]
        )

    @app.get("/api/metrics")
    def api_metrics():
        # Axis options for the trend explorer: config-defined numeric
        # metrics plus the built-in "date_reported" time axis.
        numeric = [m for m in config.trend_metrics if m.get("type", "numeric") == "numeric"]
        return jsonify(
            {
                "numeric": numeric,
                "time_axis": {"key": "date_reported", "label": "Date reported", "unit": ""},
            }
        )

    @app.get("/reports/<path:filename>")
    def report_assets(filename):
        # Serves matplotlib PNGs etc. referenced by older report renders,
        # and anything else saved under the report output dir.
        return send_from_directory(reports_dir, filename)

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(prog="besseleth.web")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    app = create_app(config)
    print(f"[web] Serving {config.industry_name} dashboard at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
