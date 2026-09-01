"""Orchestrates: scrape -> dedup/store -> personalize -> summarize -> report."""
from __future__ import annotations

from datetime import date, datetime, timezone

from .config import Config
from .db import DB, Item
from .personalize import personalize_items
from .scrapers import (
    arxiv_scraper,
    blog_scraper,
    conference_scraper,
    events_scraper,
    linkedin_scraper,
    news_scraper,
    social_scraper,
)
from . import report as report_mod
from .dedupe import merge_near_duplicates
from .enrich import enrich_items
from .trends import company_store
from .trends import store as trend_store
from .trends import plot as trend_plot

SOURCES = ["arxiv", "news", "blog", "conference", "conference_news", "event", "social", "linkedin", "clip"]


def _days_back(configured: int, since: date | None) -> int:
    """A backfill (`since`) overrides the configured lookback window,
    never shrinks it — you asked for history, not less than usual."""
    if since is None:
        return configured
    return max(configured, (date.today() - since).days)


def fetch_all(config: Config, db: DB, since: date | None = None) -> dict[str, list[Item]]:
    """Runs every enabled scraper, dedupes against the DB, and returns the
    newly-seen items grouped by source (existing items are not
    re-included). Pass `since` to backfill further back than each
    source's configured `days_back` — e.g. to seed history right after
    setup, or after being away for a while."""
    results: dict[str, list[Item]] = {s: [] for s in SOURCES}

    arxiv_cfg = config.source("arxiv")
    if arxiv_cfg.get("enabled"):
        print("[pipeline] Fetching arXiv...")
        items = arxiv_scraper.fetch(
            config,
            days_back=_days_back(arxiv_cfg.get("days_back", 8), since),
            max_results_per_keyword=arxiv_cfg.get("max_results_per_keyword", 15),
        )
        results["arxiv"] = _dedupe_and_store(items, db)

    news_cfg = config.source("news")
    if news_cfg.get("enabled"):
        print("[pipeline] Fetching news...")
        items = news_scraper.fetch(config, news_cfg, days_back=_days_back(news_cfg.get("days_back", 8), since))
        results["news"] = _dedupe_and_store(items, db)

    blog_cfg = config.source("blogs")
    if blog_cfg.get("enabled"):
        print("[pipeline] Fetching blogs...")
        items = blog_scraper.fetch(config, blog_cfg, days_back=_days_back(blog_cfg.get("days_back", 8), since))
        results["blog"] = _dedupe_and_store(items, db)

    conf_cfg = config.source("conferences")
    if conf_cfg.get("enabled"):
        print("[pipeline] Fetching conference watchlist...")
        items = conference_scraper.fetch(config, conf_cfg)
        results["conference"] = _dedupe_and_store(items, db)
        print("[pipeline] Fetching conference news feeds...")
        news_items = conference_scraper.fetch_conference_news(
            config, conf_cfg, days_back=_days_back(conf_cfg.get("days_back", 8), since)
        )
        results["conference_news"] = _dedupe_and_store(news_items, db)

    events_cfg = config.source("events")
    if events_cfg.get("enabled"):
        print("[pipeline] Fetching events...")
        items = events_scraper.fetch(config, events_cfg)
        results["event"] = _dedupe_and_store(items, db)

    social_cfg = config.source("social")
    if social_cfg.get("enabled"):
        print("[pipeline] Fetching social (Bluesky/X)...")
        items = social_scraper.fetch(config, social_cfg, days_back=_days_back(social_cfg.get("days_back", 8), since))
        results["social"] = _dedupe_and_store(items, db)

    linkedin_cfg = config.source("linkedin")
    if linkedin_cfg.get("enabled"):
        print("[pipeline] Fetching LinkedIn source...")
        items = linkedin_scraper.fetch(config, linkedin_cfg)
        results["linkedin"] = _dedupe_and_store(items, db)

    print("[pipeline] Enriching papers/news/blog items (org, modality, therapeutic target, novelty)...")
    enrich_items(config, db)

    return results


def _dedupe_and_store(items: list[Item], db: DB) -> list[Item]:
    new_items = []
    for item in items:
        if db.upsert_item(item):
            new_items.append(item)
    return new_items


def generate_weekly_report(config: Config, db: DB) -> str:
    """Pulls all unreported items from the DB, personalizes, summarizes,
    renders, saves (and optionally emails) the report. Returns the file path."""
    all_unreported = db.unreported_items()
    items_by_source: dict[str, list[Item]] = {s: [] for s in SOURCES}
    for row in all_unreported:
        if row["source"] not in items_by_source:
            continue
        items_by_source[row["source"]].append(
            Item(
                id=row["id"],
                source=row["source"],
                title=row["title"],
                url=row["url"] or "",
                summary=row["summary"] or "",
                published_at=row["published_at"] or "",
                matched_keywords=(row["matched_keywords"] or "").split(",") if row["matched_keywords"] else [],
                matched_contact=row["matched_contact"],
                matched_company=row["matched_company"],
            )
        )

    all_items_raw = [i for src in SOURCES for i in items_by_source[src]]

    # Collapse near-duplicates across sources (e.g. a pasted LinkedIn post
    # about the same story as a scraped news article) into one item, so
    # the report doesn't repeat the same fact twice. The kept item's
    # summary absorbs any distinct detail from the ones it swallows.
    all_items, dropped_by_kept = merge_near_duplicates(all_items_raw)
    dropped_ids = {did for ids in dropped_by_kept.values() for did in ids}
    if dropped_ids:
        print(f"[pipeline] Merged {len(dropped_ids)} near-duplicate item(s) into {len(dropped_by_kept)} kept item(s).")
    for src in items_by_source:
        items_by_source[src] = [i for i in items_by_source[src] if i.id not in dropped_ids]

    personalize_items(all_items, config.contacts)
    personalized = [i for i in all_items if i.matched_contact]

    # Persist personalization matches (and merged summaries) back to the DB.
    for i in all_items:
        db.conn.execute(
            "UPDATE items SET matched_contact = ?, matched_company = ?, summary = ? WHERE id = ?",
            (i.matched_contact, i.matched_company, i.summary, i.id),
        )
    db.conn.commit()

    report_cfg = config.report
    max_n = report_cfg.get("max_items_per_section", 12)
    days_back = config.source("news").get("days_back", 8)

    trend_devices = trend_store.load_devices(config.devices_path)
    trend_companies = company_store.load_companies(config.companies_path)
    trend_charts: list = []
    if trend_devices:
        charts_dir = config.raw.get("trends", {}).get("charts_dir", "reports/trends")
        trend_charts = trend_plot.generate_trend_charts(trend_devices, config.trend_metrics, charts_dir)

    report_id, markdown = report_mod.build_report(
        industry_name=config.industry_name,
        days_back=days_back,
        arxiv_items=items_by_source["arxiv"][:max_n],
        news_items=items_by_source["news"][:max_n],
        blog_items=items_by_source["blog"][:max_n],
        conference_items=items_by_source["conference"],
        conference_news_items=items_by_source["conference_news"][:max_n],
        event_items=items_by_source["event"][:max_n],
        social_items=items_by_source["social"][:max_n],
        linkedin_items=items_by_source["linkedin"][:max_n],
        clip_items=items_by_source["clip"][:max_n],
        personalized_items=personalized,
        summarizer_cfg=config.summarizer,
        trend_devices=trend_devices,
        trend_metrics=config.trend_metrics,
        trend_chart_paths=trend_charts,
        trend_companies=trend_companies,
    )

    path = report_mod.save_report(markdown, report_id, report_cfg.get("output_dir", "reports"))
    report_mod.email_report(markdown, report_id, config.industry_name, report_cfg.get("email", {}))
    _prune_old_reports(config)

    # Mark kept items AND the duplicates merged into them as reported —
    # a dropped duplicate's content is now folded into its winner, so it
    # shouldn't linger "unreported" and get re-considered every future run.
    all_ids = [i.id for i in all_items] + list(dropped_ids)
    db.mark_reported(all_ids, report_id)

    print(f"[pipeline] Report written to {path}")
    return str(path)


def _prune_old_reports(config: Config):
    keep_last = config.raw.get("reports", {}).get("keep_last", 0)
    if not keep_last:
        return
    from pathlib import Path

    reports_dir = Path(config.report.get("output_dir", "reports"))
    reports = sorted(reports_dir.glob("report-*.md"), reverse=True)
    for stale in reports[keep_last:]:
        stale.unlink(missing_ok=True)
        print(f"[pipeline] Pruned old report {stale.name} (reports.keep_last={keep_last})")
