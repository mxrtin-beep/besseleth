"""Orchestrates: scrape -> dedup/store -> personalize -> summarize -> report."""
from __future__ import annotations

from .config import Config
from .db import DB, Item
from .personalize import personalize_items
from .scrapers import arxiv_scraper, news_scraper, conference_scraper, linkedin_scraper
from . import report as report_mod


def fetch_all(config: Config, db: DB) -> dict[str, list[Item]]:
    """Runs every enabled scraper, dedupes against the DB, and returns the
    newly-seen items grouped by source (existing items are not re-included)."""
    results: dict[str, list[Item]] = {"arxiv": [], "news": [], "conference": [], "linkedin": []}

    arxiv_cfg = config.source("arxiv")
    if arxiv_cfg.get("enabled"):
        print("[pipeline] Fetching arXiv...")
        items = arxiv_scraper.fetch(
            config,
            days_back=arxiv_cfg.get("days_back", 8),
            max_results_per_keyword=arxiv_cfg.get("max_results_per_keyword", 15),
        )
        results["arxiv"] = _dedupe_and_store(items, db)

    news_cfg = config.source("news")
    if news_cfg.get("enabled"):
        print("[pipeline] Fetching news...")
        items = news_scraper.fetch(config, news_cfg, days_back=news_cfg.get("days_back", 8))
        results["news"] = _dedupe_and_store(items, db)

    conf_cfg = config.source("conferences")
    if conf_cfg.get("enabled"):
        print("[pipeline] Fetching conference watchlist...")
        items = conference_scraper.fetch(config, conf_cfg)
        results["conference"] = _dedupe_and_store(items, db)

    linkedin_cfg = config.source("linkedin")
    if linkedin_cfg.get("enabled"):
        print("[pipeline] Fetching LinkedIn source...")
        items = linkedin_scraper.fetch(config, linkedin_cfg)
        results["linkedin"] = _dedupe_and_store(items, db)

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
    items_by_source: dict[str, list[Item]] = {"arxiv": [], "news": [], "conference": []}
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

    all_items = items_by_source["arxiv"] + items_by_source["news"] + items_by_source["conference"]
    personalize_items(all_items, config.contacts)
    personalized = [i for i in all_items if i.matched_contact]

    # Persist personalization matches back to the DB.
    for i in personalized:
        db.conn.execute(
            "UPDATE items SET matched_contact = ?, matched_company = ? WHERE id = ?",
            (i.matched_contact, i.matched_company, i.id),
        )
    db.conn.commit()

    report_cfg = config.report
    days_back = config.source("news").get("days_back", 8)
    report_id, markdown = report_mod.build_report(
        industry_name=config.industry_name,
        days_back=days_back,
        arxiv_items=items_by_source["arxiv"][: report_cfg.get("max_items_per_section", 12)],
        news_items=items_by_source["news"][: report_cfg.get("max_items_per_section", 12)],
        conference_items=items_by_source["conference"],
        personalized_items=personalized,
        summarizer_cfg=config.summarizer,
    )

    path = report_mod.save_report(markdown, report_id, report_cfg.get("output_dir", "reports"))
    report_mod.email_report(markdown, report_id, config.industry_name, report_cfg.get("email", {}))

    all_ids = [i.id for i in all_items]
    db.mark_reported(all_ids, report_id)

    print(f"[pipeline] Report written to {path}")
    return str(path)
