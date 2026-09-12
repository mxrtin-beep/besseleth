"""Upload-a-research-PDF: extract its text, store it as a normal `papers`
item (source="upload") so it flows through the same enrichment/report
pipeline as every arXiv/OpenAlex paper, and — right away, rather than
waiting on the next scheduled enrich pass — write up how it compares to
the closest-matching papers besseleth already has on file.

Requires pypdf (pip install pypdf) for text extraction. Comparison uses
the same local LLM (Ollama) as summarizer.py, with a plain "here's what's
related, no LLM available" fallback so an upload is never a dead end.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from . import summarizer as summarizer_mod
from .db import DB, Item
from .scrapers.util import stable_id, text_matches_keywords

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "for", "on", "with", "is", "are", "was", "were",
    "we", "this", "that", "by", "as", "at", "from", "be", "it", "its", "into", "using", "used", "based",
}


def _extract_pdf_text(pdf_path: str | Path) -> tuple[str, str]:
    """Returns (title_guess, full_text). Title guess = the first
    non-trivial line of page 1 (PDF metadata titles are unreliable —
    often "Microsoft Word - draft3.docx" or blank)."""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError("PDF upload requires pypdf — `pip install pypdf` (see requirements.txt).") from e

    reader = PdfReader(str(pdf_path))
    pages_text = [page.extract_text() or "" for page in reader.pages]
    full_text = "\n".join(pages_text).strip()
    title = Path(pdf_path).stem
    if pages_text:
        for line in pages_text[0].splitlines():
            line = line.strip()
            if len(line) > 8 and not line.isdigit():
                title = line
                break
    return title, full_text


def _keywords(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2}


def _find_related_papers(db: DB, text: str, limit: int = 8) -> list:
    """Cheap, dependency-free relatedness: word-overlap between the
    upload and every stored paper's title+summary. Good enough to
    surface "these are about the same topic/technique" without needing
    an embeddings model — the actual comparison write-up is the LLM's
    job, this just narrows what it reads."""
    upload_words = _keywords(text[:20000])
    if not upload_words:
        return []
    scored = []
    for row in db.papers(["papers", "arxiv"]):
        candidate_text = f"{row['title']} {row['summary'] or ''}"
        candidate_words = _keywords(candidate_text)
        if not candidate_words:
            continue
        overlap = len(upload_words & candidate_words)
        if overlap:
            scored.append((overlap, row))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [row for _, row in scored[:limit]]


def _comparison_note(config, upload_title: str, upload_text: str, related: list) -> str:
    """LLM write-up of how the upload fits in with `related` — falls
    back to a plain "here's what looks related" listing if Ollama isn't
    reachable, same fallback philosophy as summarizer.py."""
    summarizer_cfg = config.summarizer
    if not related:
        return (
            "No closely related papers found yet in besseleth's own database — this may be the first item on "
            "this specific topic it's seen, or the wording just doesn't overlap with what's stored. It's been "
            "added to Papers/Sources and will be compared again as more items come in."
        )
    listing = "\n".join(
        f"- \"{r['title']}\" ({(r['published_at'] or '')[:10]}, org: {r['org'] or 'unknown'}): "
        f"{(r['summary'] or '')[:300]}"
        for r in related
    )
    if summarizer_cfg.get("backend") != "ollama":
        return (
            "Closest related items already on file (set summarizer.backend: \"ollama\" in config.yaml and have "
            "Ollama running for an actual written comparison):\n" + listing
        )
    prompt = (
        "You are a research analyst. A user just uploaded a paper. Compare it to the related papers listed below "
        "and write a short (3-5 sentence) analysis of how it fits into current research: does it confirm, extend, "
        "contradict, or sit apart from what's already known; is it more/less advanced on any metric mentioned; "
        "what's genuinely new about it, if anything. Be specific and reference the related papers by title where "
        "relevant. If nothing below is truly related, say so plainly instead of forcing a comparison.\n\n"
        f"Uploaded paper title: {upload_title}\n"
        f"Uploaded paper text (excerpt): {upload_text[:4000]}\n\n"
        f"Related papers already on file:\n{listing}\n\n"
        "Respond with ONLY the analysis text, no preamble."
    )
    result = summarizer_mod._ollama_generate(
        prompt,
        summarizer_cfg.get("ollama_url", "http://localhost:11434"),
        summarizer_cfg.get("model", "llama3.1"),
        num_thread=summarizer_cfg.get("num_thread"),
    )
    if result:
        return result
    return "Closest related items already on file (Ollama unreachable — couldn't generate a written comparison):\n" + listing


def ingest_pdf_upload(config, pdf_path: str | Path, original_filename: str) -> dict:
    """Extracts text from the uploaded PDF, stores it as a `papers` item,
    and writes a comparison note against related papers already stored.
    Returns {"item_id", "title", "comparison_note", "related": [...]}."""
    title, full_text = _extract_pdf_text(pdf_path)
    if not full_text.strip():
        raise ValueError("Couldn't extract any text from that PDF — is it a scanned image without OCR text?")

    db = DB(config.db_path)
    try:
        related = _find_related_papers(db, full_text)
        comparison_note = _comparison_note(config, title, full_text, related)

        item = Item(
            id=stable_id("upload", f"{original_filename}:{datetime.now(timezone.utc).isoformat()}"),
            source="papers",
            title=title,
            url="",
            # Truncated: the full text isn't needed for the report/enrich
            # pipeline (which only reads the first ~1500 chars anyway —
            # see enrich.py's prompt builder), and keeping the items table
            # light matters more than keeping every page around twice
            # (it's also on disk as the uploaded file).
            summary=full_text[:8000],
            published_at=datetime.now(timezone.utc).isoformat(),
            matched_keywords=text_matches_keywords(full_text, config.keywords) or ["upload"],
        )
        db.upsert_item(item)
        db.add_paper_upload(
            item_id=item.id, filename=original_filename, comparison_note=comparison_note,
            related_item_ids=[r["id"] for r in related],
        )
        return {
            "item_id": item.id,
            "title": title,
            "comparison_note": comparison_note,
            "related": [{"id": r["id"], "title": r["title"], "url": r["url"], "published_at": r["published_at"]} for r in related],
        }
    finally:
        db.close()
