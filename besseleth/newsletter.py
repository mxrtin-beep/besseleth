"""Builds and sends the fortnightly/periodic newsletter — a non-
personalized, event-deduplicated digest organized into fixed categories
(Funding, Regulatory, Clinical, Commercial, Hiring) instead of by source.
Deliberately reuses the SAME deduped item pool the weekly report already
gathered (see pipeline.generate_weekly_report, which calls build_newsletter
right after build_report with the exact same `all_items` list) rather than
re-fetching or re-deduping anything — the only newsletter-specific LLM work
is categorizing/writing a one-line bullet per item and one executive-
summary call, not re-doing the fetch/enrich/merge-duplicates pipeline."""
from __future__ import annotations

import json
import re
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from .config import env
from .db import Item
from . import summarizer

# Order matters — this is the section order in the rendered newsletter.
CATEGORY_LABELS = {
    "Funding": "Funding",
    "Regulatory": "Regulatory",
    "Clinical": "Clinical / Research",
    "Commercial": "Commercial / Collaborations / Launches",
    "Hiring": "Hiring / Leadership",
}
CATEGORIES = list(CATEGORY_LABELS)

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_NEWSLETTER_FILENAME_RE = re.compile(r"^newsletter-(\d{4}-\d{2}-\d{2})(?:-(\d+))?\.html$")


def newsletter_sort_key(path: Path) -> tuple[str, int]:
    """Same scheme as report.report_sort_key — see its docstring."""
    match = _NEWSLETTER_FILENAME_RE.match(path.name)
    if not match:
        return ("", 0)
    return (match.group(1), int(match.group(2) or 0))


def _categorize_and_bullet(
    items: list[Item], industry_name: str, summarizer_cfg: dict, batch_size: int = 15,
) -> list[dict]:
    """Returns [{"item": Item, "category": str, "bullet": str}, ...] for
    whichever items the LLM judged newsletter-worthy (skips generic/non-
    newsworthy items entirely rather than forcing every item into a
    category). Numbered-index-in/numbered-index-out, same anti-dropped-
    link/anti-hallucination pattern as summarizer.summarize_items_numbered
    — the LLM never has to repeat a title or URL, just point at a number,
    so there's nothing for it to garble. Batched so one big week doesn't
    blow the prompt's context window in one call."""
    results = []
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        listing = "\n".join(
            f"{i}. {b.title} — {(b.summary or '')[:300]}" for i, b in enumerate(batch)
        )
        prompt = (
            f"You are drafting a periodic industry newsletter for {industry_name}. Below is a numbered list of "
            f"recent items. For each item that is genuinely newsletter-worthy (a real funding round or M&A, a "
            f"regulatory action/clearance/approval, a clinical or research result, a commercial launch/"
            f"partnership/collaboration, or a hiring/leadership change — skip generic news, opinion pieces, and "
            f"anything that isn't a concrete, reportable event), respond with one JSON object per item in a JSON "
            f"array:\n"
            '{"i": <item number>, "category": "Funding"|"Regulatory"|"Clinical"|"Commercial"|"Hiring", '
            '"bullet": "<one concise newsletter-style sentence, third person, active voice, no citation or link '
            'in the text itself>"}\n'
            f"Omit any item that doesn't belong in the newsletter at all — don't force-fit filler. Items:\n\n"
            f"{listing}\n\nRespond with ONLY the JSON array, nothing else."
        )
        raw = summarizer._llm_generate(prompt, summarizer_cfg, timeout=90)
        if not raw:
            continue
        match = _JSON_ARRAY_RE.search(raw)
        if not match:
            continue
        try:
            parsed = json.loads(match.group(0))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, list):
            continue
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            idx = entry.get("i")
            category = entry.get("category")
            bullet = (entry.get("bullet") or "").strip()
            if not isinstance(idx, int) or not (0 <= idx < len(batch)):
                continue
            if category not in CATEGORY_LABELS or not bullet:
                continue
            results.append({"item": batch[idx], "category": category, "bullet": bullet})
    return results


def _executive_summary(
    categorized: list[dict], counts: dict[str, int], industry_name: str, summarizer_cfg: dict,
) -> str:
    """One short LLM call synthesizing the whole issue into a few-sentence
    executive summary — the counts themselves are computed in code (never
    trusted to the LLM to add up), only the narrative thread is generated."""
    total = sum(counts.values())
    if total == 0:
        return f"No newsletter-worthy items this period for {industry_name}."
    counts_line = ", ".join(
        f"{counts[c]} {CATEGORY_LABELS[c]}" for c in CATEGORIES if counts.get(c)
    )
    bullets_listing = "\n".join(f"- [{CATEGORY_LABELS[e['category']]}] {e['bullet']}" for e in categorized)
    prompt = (
        f"Write a short executive summary (2-3 short paragraphs, plain prose, no headers or bullet points) for "
        f"a {industry_name} industry newsletter covering {total} stories this period ({counts_line}). Identify "
        f"the clearest overall theme(s) connecting several of the stories below, and call out anything "
        f"particularly notable. Do not just restate the category counts as a list — that's already shown "
        f"separately. Stories:\n\n{bullets_listing}\n\nRespond with ONLY the summary prose."
    )
    result = summarizer._llm_generate(prompt, summarizer_cfg, timeout=90)
    return (result or "").strip() or f"{total} stories this period: {counts_line}."


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _bullet_html(entry: dict) -> str:
    """The bullet's first word links to the item's source URL — if the
    item has no URL, the bullet just renders as plain text (never a link
    to nowhere)."""
    item: Item = entry["item"]
    bullet = entry["bullet"].strip()
    if not item.url or not bullet:
        return f"<li>{_esc(bullet)}</li>"
    parts = bullet.split(" ", 1)
    first_word, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    linked = f'<a href="{_esc(item.url)}">{_esc(first_word)}</a>'
    return f"<li>{linked}{(' ' + _esc(rest)) if rest else ''}</li>"


def _bullet_markdown(entry: dict) -> str:
    item: Item = entry["item"]
    bullet = entry["bullet"].strip()
    if not item.url or not bullet:
        return f"- {bullet}"
    parts = bullet.split(" ", 1)
    first_word, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    return f"- [{first_word}]({item.url}){(' ' + rest) if rest else ''}"


def build_newsletter(
    industry_name: str, all_items: list[Item], summarizer_cfg: dict, issue_number: int | None = None,
) -> tuple[str, str, str]:
    """Returns (newsletter_id, html, plaintext). `all_items` should already
    be near-duplicate-merged (event-level dedup) — pass the exact same
    list generate_weekly_report already built for the report, not a fresh
    fetch. issue_number is just cosmetic ("Newsletter #N" in the title);
    pass None to omit it."""
    now = datetime.now(timezone.utc)
    newsletter_id = now.strftime("%Y-%m-%d")

    categorized = _categorize_and_bullet(all_items, industry_name, summarizer_cfg)
    by_category: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    for entry in categorized:
        by_category[entry["category"]].append(entry)
    counts = {c: len(by_category[c]) for c in CATEGORIES}
    total = sum(counts.values())

    exec_summary = _executive_summary(categorized, counts, industry_name, summarizer_cfg)

    title = f"{industry_name} Newsletter" + (f" #{issue_number}" if issue_number else "")
    date_str = now.strftime("%B %d, %Y")

    html_sections = []
    md_sections = []
    for cat in CATEGORIES:
        entries = by_category[cat]
        if not entries:
            continue
        html_sections.append(
            f"<h2>{_esc(CATEGORY_LABELS[cat])}</h2><ul>" + "".join(_bullet_html(e) for e in entries) + "</ul>"
        )
        md_sections.append(f"## {CATEGORY_LABELS[cat]}\n\n" + "\n\n".join(_bullet_markdown(e) for e in entries))

    html = (
        f"<html><body style=\"font-family: sans-serif; max-width: 680px; margin: 0 auto;\">"
        f"<h1>{_esc(title)}</h1><p><em>{_esc(date_str)}</em></p>"
        f"<h2>Executive Summary</h2><p>{_esc(exec_summary).replace(chr(10) + chr(10), '</p><p>')}</p>"
        + "".join(html_sections)
        + f"<hr><p><em>Generated by besseleth on {now.strftime('%Y-%m-%d %H:%M UTC')}. "
        f"{total} stories this period.</em></p></body></html>"
    )
    plaintext = (
        f"{title}\n{date_str}\n\nExecutive Summary\n\n{exec_summary}\n\n"
        + "\n\n".join(md_sections)
        + f"\n\n---\nGenerated by besseleth on {now.strftime('%Y-%m-%d %H:%M UTC')}. {total} stories this period."
    )
    return newsletter_id, html, plaintext


def save_newsletter(html: str, newsletter_id: str, output_dir) -> tuple[Path, str]:
    """Same same-day-suffix scheme as report.save_report — see its
    docstring."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    final_id = newsletter_id
    suffix = 0
    while (out / f"newsletter-{final_id}.html").exists():
        suffix += 1
        final_id = f"{newsletter_id}-{suffix}"
    path = out / f"newsletter-{final_id}.html"
    path.write_text(html, encoding="utf-8")
    return path, final_id


def email_newsletter(
    html: str, plaintext: str, newsletter_id: str, industry_name: str,
    newsletter_cfg: dict, report_email_cfg: dict,
):
    """Sent as a proper HTML email (so the linked bullet words actually
    render as clickable links) with a plaintext fallback part. SMTP
    host/port/credentials are read from report.email — same mail
    account, just a different recipient list (newsletter_cfg["to"],
    the mailing list — never report.email["to"], which is your personal
    address) — so there's no separate SMTP config to duplicate/keep in
    sync for what's almost always the same sending account."""
    if not newsletter_cfg.get("enabled"):
        return
    user = env(report_email_cfg.get("smtp_user_env", ""))
    password = env(report_email_cfg.get("smtp_pass_env", ""))
    to = newsletter_cfg.get("to", [])
    if not (user and password and to):
        print("[newsletter] Enabled but SMTP creds or recipients missing; skipping send.")
        return

    msg = MIMEMultipart("alternative")
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg["Subject"] = f"{industry_name} Newsletter — {newsletter_id}"
    msg.attach(MIMEText(plaintext, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(report_email_cfg["smtp_host"], report_email_cfg["smtp_port"]) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(user, to, msg.as_string())
        print(f"[newsletter] Emailed to {len(to)} recipient(s).")
    except Exception as e:
        print(f"[newsletter] Failed to send email: {e}")
