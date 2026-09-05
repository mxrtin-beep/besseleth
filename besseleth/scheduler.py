"""Runs the pipeline on a recurring schedule, in-process — so besseleth is
a standing thing you start once (`besseleth.web.app` or `besseleth.cli
serve`) rather than a command you have to remember to run.

Two independent schedules, since you usually want fresher raw data than
report cadence:

  - `fetch_interval_hours` — how often to scrape sources into the DB
    (default: every 6 hours).
  - `report_cron` — a standard 5-field cron expression for when to render
    the weekly report from whatever's accumulated since the last one
    (default: Monday 4am — `0 4 * * MON`). Interpreted in `schedule.timezone`
    if set, otherwise in the host's local timezone — so the default means
    4am local time, overnight, rather than 4am UTC.

Status (last run time, last error, next scheduled run) is kept in a small
in-memory object so besseleth.web.app can show it; it's not persisted, so
it resets on restart — that's fine, it's just a "what happened recently"
display, not the source of truth (the DB and reports/ are).
"""
from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import Config
from .db import DB
from .pipeline import fetch_all, generate_weekly_report


@dataclass
class SchedulerStatus:
    enabled: bool = False
    running_now: bool = False
    last_fetch_at: str | None = None
    last_fetch_counts: dict | None = None
    last_report_at: str | None = None
    last_report_path: str | None = None
    last_error: str | None = None
    next_fetch_at: str | None = None
    next_report_at: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def as_dict(self) -> dict:
        with self._lock:
            return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


def _run_fetch(config: Config, status: SchedulerStatus):
    with status._lock:
        status.running_now = True
    try:
        db = DB(config.db_path)
        try:
            results = fetch_all(config, db)
            fetch_finished_at = datetime.now(timezone.utc).isoformat()
            # Persisted (unlike SchedulerStatus, which is in-memory only)
            # so a restart can tell how recently this ran — see
            # _initial_fetch_delay() below.
            db.set_meta("last_fetch_at", fetch_finished_at)
        finally:
            db.close()
        with status._lock:
            status.last_fetch_at = fetch_finished_at
            status.last_fetch_counts = {k: len(v) for k, v in results.items()}
            status.last_error = None
    except Exception as e:
        print(f"[scheduler] fetch job failed: {e}")
        traceback.print_exc()
        with status._lock:
            status.last_error = f"fetch: {e}"
    finally:
        with status._lock:
            status.running_now = False


def _run_report(config: Config, status: SchedulerStatus):
    with status._lock:
        status.running_now = True
    try:
        db = DB(config.db_path)
        try:
            path = generate_weekly_report(config, db)
        finally:
            db.close()
        with status._lock:
            status.last_report_at = datetime.now(timezone.utc).isoformat()
            status.last_report_path = path
            status.last_error = None
    except Exception as e:
        print(f"[scheduler] report job failed: {e}")
        traceback.print_exc()
        with status._lock:
            status.last_error = f"report: {e}"
    finally:
        with status._lock:
            status.running_now = False


def run_now(config: Config, status: SchedulerStatus):
    """Fetch + report immediately, e.g. from a dashboard 'Run now' button."""
    _run_fetch(config, status)
    _run_report(config, status)


def _initial_fetch_delay(config: Config, fetch_hours: float) -> datetime:
    """When to run the very first fetch after starting up. If the last
    fetch (persisted in the DB, so it survives a restart) was recent
    enough that it isn't due yet, waits until it would be instead of
    always firing immediately — otherwise every close-and-reopen re-runs
    a full fetch (and its enrichment burst) even seconds after the last
    one, which is wasted network/CPU work, not fresher data."""
    now = datetime.now(timezone.utc)
    db = DB(config.db_path)
    try:
        last_fetch_at = db.get_meta("last_fetch_at")
    finally:
        db.close()
    if not last_fetch_at:
        return now  # never fetched — run right away
    try:
        last_dt = datetime.fromisoformat(last_fetch_at)
    except ValueError:
        return now
    due_at = last_dt + timedelta(hours=fetch_hours)
    return due_at if due_at > now else now


def _resolve_timezone(schedule_cfg: dict):
    """`schedule.timezone` names an IANA zone (e.g. "America/Los_Angeles").
    Left unset, we fall back to whatever the host's local timezone is
    (rather than UTC) so a plain time like `report_cron: "0 4 * * MON"`
    means 4am where besseleth is actually running."""
    tz_name = schedule_cfg.get("timezone")
    if tz_name:
        return ZoneInfo(tz_name)
    local_tz = datetime.now().astimezone().tzinfo
    return local_tz or timezone.utc


def start_scheduler(config: Config) -> tuple[BackgroundScheduler | None, SchedulerStatus]:
    """Starts the background jobs per `schedule` in config.yaml. Returns
    (scheduler_or_None, status) — scheduler is None if schedule.enabled is
    false, in which case besseleth only updates when you trigger it
    manually (CLI or the dashboard's 'Run now')."""
    schedule_cfg = config.raw.get("schedule", {}) or {}
    status = SchedulerStatus(enabled=bool(schedule_cfg.get("enabled", True)))

    if not status.enabled:
        print("[scheduler] schedule.enabled is false; besseleth will only run when triggered manually.")
        return None, status

    fetch_hours = schedule_cfg.get("fetch_interval_hours", 6)
    report_cron = schedule_cfg.get("report_cron", "0 4 * * MON")
    tz = _resolve_timezone(schedule_cfg)

    scheduler = BackgroundScheduler(timezone=tz)
    first_fetch_at = _initial_fetch_delay(config, fetch_hours)
    fetch_job = scheduler.add_job(
        _run_fetch, IntervalTrigger(hours=fetch_hours), args=[config, status], id="fetch", next_run_time=first_fetch_at
    )
    report_job = scheduler.add_job(
        _run_report, CronTrigger.from_crontab(report_cron, timezone=tz), args=[config, status], id="report"
    )
    scheduler.start()

    def _sync_next_runs():
        with status._lock:
            status.next_fetch_at = fetch_job.next_run_time.isoformat() if fetch_job.next_run_time else None
            status.next_report_at = report_job.next_run_time.isoformat() if report_job.next_run_time else None

    _sync_next_runs()
    scheduler.add_listener(lambda event: _sync_next_runs())

    print(
        f"[scheduler] Started: fetch every {fetch_hours}h, report on cron '{report_cron}' ({tz}) "
        f"(next fetch {status.next_fetch_at}, next report {status.next_report_at})."
    )
    return scheduler, status
