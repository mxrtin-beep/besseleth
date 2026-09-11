"""Orchestrates: scrape -> dedup/store -> personalize -> summarize -> report."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from .config import Config
from .db import DB, Item
from .personalize import flag_interests, personalize_items
from .scrapers import (
    arxiv_scraper,
    blog_scraper,
    clinicaltrials_scraper,
    conference_scraper,
    events_scraper,
    grants_scraper,
    jobs_scraper,
    linkedin_scraper,
    news_scraper,
    openalex_scraper,
    social_scraper,
)
from . import report as report_mod
from .dedupe import merge_near_duplicates
from .enrich import enrich_items
from .feeds_store import load_feeds

SOURCES = ["papers", "news", "blog", "conference", "conference_news", "event", "social", "linkedin", "clip"]


def _days_back(configured: int, since: date | None) -> int:
    """A backfill (`since`) overrides the configured lookback window,
    never shrinks it — you asked for history, not less than usual."""
    if since is None:
        return configured
    return max(configured, (date.today() - since).days)


def fetch_all(config: Config, db: DB, since: date | None = None, progress_cb=None) -> dict[str, list[Item]]:
    """Runs every enabled scraper, dedupes against the DB, and returns the
    newly-seen items grouped by source (existing items are not
    re-included). Pass `since` to backfill further back than each
    source's configured `days_back` — e.g. to seed history right after
    setup, or after being away for a while.

    progress_cb(label, current, total), if given, is called once per
    fetch phase below (a fixed 9-step sequence — disabled sources still
    advance the counter, just near-instantly, since there's no fetch to
    wait on) — a very basic "step N of 9: <label>" indicator, not a
    precise item-level progress bar."""
    results: dict[str, list[Item]] = {s: [] for s in SOURCES}
    _TOTAL_FETCH_STEPS = 9  # arXiv, Papers, News, Blogs, Conferences, Events, Social, LinkedIn, Enrichment+Jobs
    _step = [0]  # mutable cell, closed over below — a plain int can't be reassigned from the closure

    def _tick(label: str):
        _step[0] += 1
        if progress_cb:
            progress_cb(f"Fetching: {label}", _step[0], _TOTAL_FETCH_STEPS)

    # arXiv (preprints, same-day freshness) and OpenAlex (published
    # papers, has citation counts, but indexes with a real lag — days to
    # weeks behind actual publication) both feed the single "papers"
    # bucket: to you these are the same thing (research papers), just
    # from two complementary feeds with different tradeoffs, not two
    # separate report sections to compare.
    arxiv_cfg = config.source("arxiv")
    if arxiv_cfg.get("enabled"):
        print("[pipeline] Fetching arXiv...")
        items = arxiv_scraper.fetch(
            config,
            days_back=_days_back(arxiv_cfg.get("days_back", 8), since),
            max_results_per_keyword=arxiv_cfg.get("max_results_per_keyword", 15),
        )
        results["papers"] += _dedupe_and_store(items, db)
    _tick("arXiv")

    papers_cfg = config.source("papers")
    if papers_cfg.get("enabled"):
        print("[pipeline] Fetching published papers (OpenAlex)...")
        items = openalex_scraper.fetch(
            config,
            days_back=_days_back(papers_cfg.get("days_back", 8), since),
            max_results_per_keyword=papers_cfg.get("max_results_per_keyword", 15),
            mailto=papers_cfg.get("mailto"),
        )
        results["papers"] += _dedupe_and_store(items, db)

        # Labs.yaml-listed PIs' own papers, fetched by AUTHOR identity
        # instead of your keyword list — catches genuinely on-topic work
        # from a lab you already know about whose title/abstract just
        # never happens to contain one of your exact keyword phrases
        # (see fetch_known_lab_papers's docstring). Inert if labs.yaml
        # is empty/missing, same as every other labs.yaml-driven feature.
        if config.labs:
            lab_items = openalex_scraper.fetch_known_lab_papers(
                config, db,
                days_back=_days_back(papers_cfg.get("days_back", 8), since),
                mailto=papers_cfg.get("mailto"),
            )
            results["papers"] += _dedupe_and_store(lab_items, db)
    _tick("Papers (OpenAlex)")

    # User-submitted feeds (the dashboard's Feeds tab) are additional
    # sources_.news/blogs feed URLs, merged in here rather than written
    # into config.yaml itself — see feeds_store.py's docstring for why.
    submitted = load_feeds(config.feeds_path)

    news_cfg = config.source("news")
    if news_cfg.get("enabled"):
        print("[pipeline] Fetching news...")
        news_cfg = {**news_cfg, "feeds": [*news_cfg.get("feeds", []), *(f["url"] for f in submitted["news"])]}
        items = news_scraper.fetch(config, news_cfg, days_back=_days_back(news_cfg.get("days_back", 8), since))
        results["news"] = _dedupe_and_store(items, db)
    _tick("News")

    blog_cfg = config.source("blogs")
    if blog_cfg.get("enabled"):
        print("[pipeline] Fetching blogs...")
        blog_cfg = {**blog_cfg, "feeds": [*blog_cfg.get("feeds", []), *(f["url"] for f in submitted["blog"])]}
        items = blog_scraper.fetch(config, blog_cfg, days_back=_days_back(blog_cfg.get("days_back", 8), since))
        results["blog"] = _dedupe_and_store(items, db)
    _tick("Blogs")

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
    _tick("Conferences")

    events_cfg = config.source("events")
    if events_cfg.get("enabled"):
        print("[pipeline] Fetching events...")
        items = events_scraper.fetch(config, events_cfg)
        results["event"] = _dedupe_and_store(items, db)
    _tick("Events")

    social_cfg = config.source("social")
    if social_cfg.get("enabled"):
        print("[pipeline] Fetching social (Bluesky/X)...")
        items = social_scraper.fetch(config, social_cfg, days_back=_days_back(social_cfg.get("days_back", 8), since))
        results["social"] = _dedupe_and_store(items, db)
    _tick("Social")

    linkedin_cfg = config.source("linkedin")
    if linkedin_cfg.get("enabled"):
        print("[pipeline] Fetching LinkedIn source...")
        items = linkedin_scraper.fetch(config, linkedin_cfg)
        results["linkedin"] = _dedupe_and_store(items, db)
    _tick("LinkedIn")

    print("[pipeline] Enriching papers/news/blog items (org, modality, therapeutic target, novelty)...")
    if progress_cb:
        progress_cb("Enriching newly-fetched items...", None, None)
    enrich_items(config, db)

    # Runs after enrichment, not before: it needs the orgs enrichment
    # just extracted (db.orgs()) to know who to look up job boards for.
    print("[pipeline] Syncing job postings for known orgs...")
    jobs_result = jobs_scraper.fetch(config, db)
    _tick("Enrichment & jobs")
    print(
        f"[pipeline] Jobs: {jobs_result['orgs_with_board']}/{jobs_result['orgs_checked']} orgs have a known "
        f"board, {jobs_result['active_postings']} posting(s) currently active."
    )

    # Same "runs after enrichment, needs db.orgs()" shape as jobs sync
    # above — both free/keyless APIs, both self-limit to orgs not
    # rechecked recently rather than re-querying every org on every fetch.
    known_orgs = [row["org"] for row in db.orgs()]
    trials_cfg = config.raw.get("trends", {}).get("clinical_trials", {})
    if trials_cfg.get("enabled", True) and known_orgs:
        print("[pipeline] Syncing clinical trial stats for known orgs...")
        trials_result = clinicaltrials_scraper.sync_all(db, known_orgs, recheck_days=trials_cfg.get("recheck_days", 7))
        print(f"[pipeline] Clinical trials: checked {trials_result['orgs_checked']}, skipped {trials_result['orgs_skipped']} (recently checked).")

    grants_cfg = config.raw.get("trends", {}).get("nih_grants", {})
    if grants_cfg.get("enabled", True) and known_orgs:
        print("[pipeline] Syncing NIH grant stats for known orgs...")
        grants_result = grants_scraper.sync_all(db, known_orgs, recheck_days=grants_cfg.get("recheck_days", 7))
        print(f"[pipeline] NIH grants: checked {grants_result['orgs_checked']}, skipped {grants_result['orgs_skipped']} (recently checked).")

    # Persisted here (not just by the scheduler's own wrapper) so `cli
    # fetch`/`cli run` update this too — previously only a scheduled run
    # or the dashboard's "Run now" ever touched it, so a CLI-only
    # workflow left both the "next fetch due" math (_initial_fetch_delay,
    # below) and the dashboard's status bar permanently reading "never"
    # even though real fetches were happening.
    db.set_meta("last_fetch_at", datetime.now(timezone.utc).isoformat())

    return results


def _dedupe_and_store(items: list[Item], db: DB) -> list[Item]:
    new_items = []
    for item in items:
        if db.upsert_item(item):
            new_items.append(item)
    return new_items


def generate_weekly_report(config: Config, db: DB, progress_cb=None) -> str:
    """Builds the report from every item in the last `days_back` days
    (config: `news.days_back`, default 8) — a fresh snapshot of "what's in
    this window right now," recomputed from scratch every time. It does
    NOT track or check what a previous report already included: re-running
    (while developing, after pasting something new, whatever the reason)
    always looks exactly as if no report had ever run before, and never
    skips an item just because an earlier run already showed it. Each run
    still gets its own timestamped file, so re-running never overwrites an
    earlier report. Returns the saved file path."""
    if progress_cb:
        progress_cb("Building report...", None, None)
    days_back = config.source("news").get("days_back", 8)
    window_rows = db.items_in_window(days_back)
    items_by_source: dict[str, list[Item]] = {s: [] for s in SOURCES}
    for row in window_rows:
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
                org=row["org"],  # already-enriched org, if any — used only by the report's "Big picture" section
                authors=row["authors"],
                citation_count=row["citation_count"],
                novelty_score=row["novelty_score"],  # already-enriched, if any — powers the "Top findings" section
                novelty_rationale=row["novelty_rationale"],
                # Deliberately NOT carried over from the row: matched_contact/
                # matched_company/matched_reason get recomputed fresh below,
                # every run, against the *current* contacts/interests config.
                # Loading the old stored values here would make a stale match
                # from a past run (a contact since edited or removed, an old
                # matching bug) stick forever — personalize_items/flag_interests
                # only ever set these fields on a new match, they never clear a
                # leftover one, so starting from None each time is what makes
                # that actually self-correcting instead of permanent.
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
    flag_interests(all_items, config.interests)
    personalized = [i for i in all_items if i.matched_contact or i.matched_reason == "interest"]

    # Persist personalization matches (and merged summaries) back to the DB.
    for i in all_items:
        db.conn.execute(
            "UPDATE items SET matched_contact = ?, matched_company = ?, matched_reason = ?, summary = ? WHERE id = ?",
            (i.matched_contact, i.matched_company, i.matched_reason, i.summary, i.id),
        )
    db.conn.commit()

    report_cfg = config.report
    max_n = report_cfg.get("max_items_per_section", 12)

    # Ranked by citation_count (highest first), not recency — a paper
    # with real citations is more worth surfacing than an uncited one.
    # Ties (including all-None, since an arXiv preprint never has a
    # citation count — OpenAlex only indexes published papers) keep
    # published_at order via the stable sort, so within "0/unknown
    # citations" the newest still comes first. Caveat worth knowing: a
    # brand-new arXiv preprint always starts at 0/unknown citations, so
    # a week with lots of well-cited published papers can crowd fresh
    # preprints out of the top max_n entirely — say if you want a
    # blended ranking (guaranteed room for the newest few regardless of
    # citations) instead of pure citation ranking.
    papers_items = sorted(items_by_source["papers"], key=lambda i: i.citation_count or 0, reverse=True)[:max_n]

    report_id, markdown = report_mod.build_report(
        industry_name=config.industry_name,
        days_back=days_back,
        papers_items=papers_items,
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
        history=db.accumulated_knowledge_stats(),
        top_findings_min_novelty=report_cfg.get("top_findings_min_novelty", 3),
        top_findings_max_count=report_cfg.get("top_findings_max_count", 5),
    )

    path = report_mod.save_report(markdown, report_id, report_cfg.get("output_dir", "reports"))
    report_mod.email_report(markdown, report_id, config.industry_name, report_cfg.get("email", {}))
    _prune_old_reports(config)

    # Purely informational (the dashboard shows which report an item last
    # appeared in) — selection above is windowed by date, not gated on
    # this, so it doesn't affect what future reports include.
    all_ids = [i.id for i in all_items] + list(dropped_ids)
    db.mark_reported(all_ids, report_id)

    # Persisted here so `cli report`/`cli run` update it too, not just a
    # scheduled run or the dashboard's "Run now" — see the matching
    # comment on fetch_all()'s last_fetch_at write.
    db.set_meta("last_report_at", datetime.now(timezone.utc).isoformat())

    print(f"[pipeline] Report written to {path}")
    return str(path)


def _prune_old_reports(config: Config):
    keep_last = config.raw.get("reports", {}).get("keep_last", 0)
    if not keep_last:
        return
    reports_dir = Path(config.report.get("output_dir", "reports"))
    reports = sorted(reports_dir.glob("report-*.md"), reverse=True)
    for stale in reports[keep_last:]:
        stale.unlink(missing_ok=True)
        print(f"[pipeline] Pruned old report {stale.name} (reports.keep_last={keep_last})")
