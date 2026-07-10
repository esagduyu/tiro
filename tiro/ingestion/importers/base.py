"""Importer core (Phase 4 M4.2, spec D7): dataclasses + `run_import`.

`run_import(config, items, *, kind, progress_cb=None)` walks a stream of
`ImportItem`s (produced by a format adapter's `parse_export`) and, per item:

1. **Dedup vs the existing library** (Phase 1 match order, adapted — imports
   carry no Tiro uid, so `url` first (raw OR canonical), then `title` +
   source-name). A match skips the article (counted) but still runs the
   highlight hook against the existing article.
2. **Content resolution, per-importer fallback (ON-7 Q8):** export-carried
   markdown is used directly (Omnivore); export-carried HTML is
   `sanitize_html`→markdownified (extraction-site sanitize invariant, D3.5);
   otherwise re-fetch via `fetch_and_extract_sync(url)`; on re-fetch failure
   (paywall / dead link) a **stub article** is created — a short fixed
   markdown template linking the original URL, tagged `import-stub`.
3. **Ingest** through `process_article(..., ingestion_method="import")`, with
   `published_at` = export published date falling back to export saved-at
   date (imported libraries sort by their history, not by import day).
4. **Highlights** — Task 4 ships `_import_highlights` as a STUB returning
   `(0, len(highlights))`: incoming highlights are honestly reported as "not
   yet imported" (counted in `highlights_skipped`). Task 5 replaces the stub
   with `reconcile_anchor`-based sidecar-first anchoring (spec D7.4).

One audit line per run (`log_api_call(config, "import", endpoint=kind, ...)`,
D4). Errors are isolated per item — a single bad item is logged and counted
in `failed`, never aborting the run.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import frontmatter
from markdownify import markdownify as _md

from tiro.anchors import content_hash, make_anchor, reconcile_anchor
from tiro.annotations import append_highlight, read_annotations, sidecar_stem
from tiro.audit import log_api_call
from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.ingestion.processor import process_article
from tiro.ingestion.rss import canonical_url
from tiro.ingestion.web import fetch_and_extract_sync
from tiro.sanitize import sanitize_html, sanitize_markdown
from tiro.tags import attach_tags

logger = logging.getLogger(__name__)

STUB_TAG = "import-stub"

# A stub article's body: the original link + an honest note, plus any
# export-carried excerpt. The URL MUST appear so the article stays
# re-fetchable (the user re-saving it later hits the 409 duplicate check).
_STUB_TEMPLATE = "Saved from {kind} — content could not be fetched.\n\n[{url}]({url})\n"


@dataclass
class ImportHighlight:
    """One imported highlight: a quote, an optional note, an optional
    creation timestamp. Anchoring against the article body happens in Task 5;
    Task 4 only carries and counts these."""

    quote: str
    note: str | None = None
    created_at: datetime | None = None


@dataclass
class ImportItem:
    """One article to import, normalized across all three export formats.

    `content_md` (markdown carried by the export, e.g. Omnivore) and
    `content_html` (HTML carried by the export) are tried before a re-fetch;
    both absent means re-fetch, then stub. `published_at`/`saved_at` feed the
    timestamp fallback in `run_import`."""

    url: str | None
    title: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    saved_at: datetime | None = None
    tags: list[str] = field(default_factory=list)
    content_md: str | None = None
    content_html: str | None = None
    highlights: list[ImportHighlight] = field(default_factory=list)
    notes: str | None = None


def _new_summary(kind: str) -> dict:
    return {
        "kind": kind,
        "total": 0,
        "processed": 0,
        "imported": 0,
        "skipped": 0,
        "failed": 0,
        "stub_articles": 0,
        "highlights_imported": 0,
        "highlights_skipped": 0,
    }


def _expected_source_name(item: ImportItem) -> str | None:
    """The source name `process_article` would derive for this item's URL
    (domain minus `www.`), for the title+source dedup fallback."""
    if not item.url:
        return None
    domain = urlparse(item.url).netloc
    return domain.removeprefix("www.") if domain else None


def _find_existing_article(conn, item: ImportItem):
    """Phase 1 match order (adapted for uid-less imports): `url` (raw OR
    canonical, matching the RSS cross-method dedup) first, then `title` +
    source-name. Returns the existing article row or None."""
    if item.url:
        canon = canonical_url(item.url)
        row = conn.execute(
            "SELECT * FROM articles WHERE url = ? OR url = ? LIMIT 1", (item.url, canon)
        ).fetchone()
        if row is not None:
            return row
    source_name = _expected_source_name(item)
    if item.title and source_name:
        row = conn.execute(
            "SELECT a.* FROM articles a JOIN sources s ON a.source_id = s.id"
            " WHERE a.title = ? AND s.name = ? LIMIT 1",
            (item.title, source_name),
        ).fetchone()
        if row is not None:
            return row
    return None


def _stub_body(item: ImportItem, kind: str) -> str:
    body = _STUB_TEMPLATE.format(kind=kind, url=item.url or "")
    excerpt = (item.content_md or item.content_html or "").strip()
    if excerpt:
        # A short, plain excerpt only — never the (failed) full HTML.
        body += "\n" + sanitize_markdown(excerpt[:500])
    return body


def _resolve_content(item: ImportItem, kind: str) -> tuple[str, bool]:
    """Return `(content_md, is_stub)`. Fallback chain: export markdown →
    export HTML (sanitize→markdownify) → re-fetch → stub."""
    if item.content_md and item.content_md.strip():
        # Export-carried markdown (Omnivore): used directly. sanitize_markdown
        # only strips dangerous raw-HTML islands / javascript: links without
        # touching markdown syntax (defense-in-depth; the reader also renders
        # through DOMPurify).
        return sanitize_markdown(item.content_md), False

    if item.content_html and item.content_html.strip():
        html = sanitize_html(item.content_html)
        text = _md(html, heading_style="ATX", bullets="-", wrap=False).strip()
        if text:
            return text, False

    if item.url:
        try:
            extracted = fetch_and_extract_sync(item.url)
            content = extracted.get("content_md")
            if content and content.strip():
                return content, False
        except Exception as e:
            logger.info("Re-fetch failed for %s (%s) — creating stub", item.url, e)

    return _stub_body(item, kind), True


def _read_article_body(config: TiroConfig, article_row) -> str:
    """The article BODY as `GET /api/articles/{id}` serves it (post-frontmatter
    markdown from `config.articles_dir / markdown_path`) -- the SAME text the
    reader displays and `reconcile_anchor` re-locates against, so imported
    offsets never disagree with what the reader will paint. Missing file -> ""
    (mirrors `routes_annotations._read_article_body`'s tolerance)."""
    md_path = Path(article_row["markdown_path"])
    if not md_path.is_absolute():
        md_path = config.articles_dir / md_path
    if not md_path.exists():
        logger.warning("Markdown file not found for highlight import: %s", md_path)
        return ""
    return frontmatter.load(str(md_path)).content


def _import_highlights(config: TiroConfig, article_row, highlights) -> tuple[int, int]:
    """Anchor each imported highlight against the article's CURRENT markdown
    body via `tiro/anchors.py` search semantics (spec D7.4 — the trust-critical
    ON-7 Q7 law), writing anchored ones sidecar-first through the shared
    `annotations.append_highlight` helper the CRUD API uses.

    Per highlight: search the body for the quote with
    `reconcile_anchor(body, {quote, prefix:"", suffix:"", position_start:None,
    content_hash:None})`. A `shifted`/`exact` result yields real offsets, from
    which `make_anchor(body, start, end)` builds the full stored anchor (real
    prefix/suffix) plus the current body's `content_hash`. `hash_mismatch`/
    `missing` (quote not found -- common when a re-fetched extraction differs
    from the source app's) -> **skipped, counted, NEVER hand-placed**.

    Re-run idempotency (spec D7.4): dedupe by exact `quote` against the
    article's current sidecar lines AND against quotes anchored earlier in this
    same call -- an already-present quote is a silent no-op (counted in
    NEITHER bucket), so re-importing catches only genuinely new highlights.

    Returns `(imported, skipped)`."""
    if not highlights:
        return (0, 0)
    body = _read_article_body(config, article_row)
    if not body:
        # No body to anchor against -- every highlight is honestly unlocatable.
        return (0, len(highlights))
    body_hash = content_hash(body)
    existing_quotes = {ln.get("quote") for ln in read_annotations(config, sidecar_stem(article_row))}

    imported = 0
    skipped = 0
    conn = get_connection(config.db_path)
    try:
        for hl in highlights:
            quote = (hl.quote or "").strip()
            if not quote:
                skipped += 1
                continue
            if quote in existing_quotes:
                continue  # dedupe: already anchored -> silent no-op, uncounted
            result = reconcile_anchor(
                body,
                {"quote": quote, "prefix": "", "suffix": "", "position_start": None,
                 "content_hash": None},
            )
            if result["status"] not in ("exact", "shifted"):
                skipped += 1
                continue
            _append_anchored_highlight(
                config, conn, article_row, body, body_hash, result,
                note=hl.note, color="yellow",
            )
            existing_quotes.add(quote)
            imported += 1
        conn.commit()
    finally:
        conn.close()
    return (imported, skipped)


def _append_anchored_highlight(config, conn, article_row, body, body_hash, result, *, note, color):
    """Given a resolved `reconcile_anchor` result (exact/shifted), build the full
    stored anchor via `make_anchor` and create ONE highlight sidecar-first through
    the shared `append_highlight` helper. Caller owns the transaction (no commit)
    and supplies the already-read body + its content_hash."""
    anchor = make_anchor(body, result["position_start"], result["position_end"])
    append_highlight(
        config,
        conn,
        article_row,
        quote=anchor["quote"],
        prefix=anchor["prefix"],
        suffix=anchor["suffix"],
        position_start=anchor["position_start"],
        position_end=anchor["position_end"],
        content_hash=body_hash,
        color=color,
        note_markdown=note,
    )


def create_highlight_from_quote(config: TiroConfig, article_id: int, quote: str, *, note=None, color="yellow") -> bool:
    """Ingest-time convenience (spec D10 — the Chrome extension's "Save with
    selection as highlight" flow): fetch the freshly-ingested article, read its
    CURRENT markdown body, and anchor `quote` against it via the SAME D7.4
    machinery the importer uses (`reconcile_anchor` search -> `make_anchor` ->
    sidecar-first `append_highlight`). Returns `highlight_created`.

    Soft-fail law (identical to import): a blank quote, a missing/empty body, or
    an unlocatable quote (`hash_mismatch`/`missing`) -> return False, create
    nothing, **never hand-place**. Opens and owns its own short transaction."""
    quote = (quote or "").strip()
    if not quote:
        return False
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT id, uid, markdown_path FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if row is None:
            return False
        body = _read_article_body(config, row)
        if not body:
            return False
        result = reconcile_anchor(
            body,
            {"quote": quote, "prefix": "", "suffix": "", "position_start": None, "content_hash": None},
        )
        if result["status"] not in ("exact", "shifted"):
            return False
        _append_anchored_highlight(config, conn, row, body, content_hash(body), result, note=note, color=color)
        conn.commit()
        return True
    finally:
        conn.close()


def _import_one(config: TiroConfig, item: ImportItem, kind: str, summary: dict) -> None:
    conn = get_connection(config.db_path)
    try:
        existing = _find_existing_article(conn, item)
    finally:
        conn.close()

    if existing is not None:
        summary["skipped"] += 1
        imported, skipped = _import_highlights(config, existing, item.highlights)
        summary["highlights_imported"] += imported
        summary["highlights_skipped"] += skipped
        return

    content_md, is_stub = _resolve_content(item, kind)
    tags = list(item.tags)
    if is_stub:
        tags.append(STUB_TAG)

    published = item.published_at or item.saved_at
    result = process_article(
        title=item.title or item.url or "Untitled",
        author=item.author,
        content_md=content_md,
        url=item.url or "",
        config=config,
        published_at=published,
        ingestion_method="import",
    )

    if tags:
        conn = get_connection(config.db_path)
        try:
            attach_tags(conn, result["id"], tags)
            conn.commit()
        finally:
            conn.close()

    # Count the article as imported only AFTER tag-attach: if `attach_tags`
    # raised, `run_import`'s per-item handler already counts this item in
    # `failed` -- incrementing `imported` above too would double-count the
    # same item across two buckets.
    summary["imported"] += 1
    if is_stub:
        summary["stub_articles"] += 1

    conn = get_connection(config.db_path)
    try:
        row = conn.execute("SELECT * FROM articles WHERE id = ?", (result["id"],)).fetchone()
    finally:
        conn.close()
    imported, skipped = _import_highlights(config, row, item.highlights)
    summary["highlights_imported"] += imported
    summary["highlights_skipped"] += skipped


def run_import(config: TiroConfig, items, *, kind: str, progress_cb=None) -> dict:
    """Import a stream of `ImportItem`s (spec D7.1–7.4). Returns a summary
    dict; writes exactly one audit line for the run (D4). Per-item errors are
    isolated (logged + counted in `failed`), never aborting the run. If
    `progress_cb` is given it is called after each item with the live summary
    (its own exceptions are swallowed so a bad callback can't break an
    import)."""
    summary = _new_summary(kind)
    started = time.monotonic()
    success = True
    error = None
    try:
        for item in items:
            summary["total"] += 1
            try:
                _import_one(config, item, kind, summary)
            except Exception as e:
                logger.error(
                    "Import item failed (%s): %s", getattr(item, "url", "?"), e
                )
                summary["failed"] += 1
            summary["processed"] += 1
            if progress_cb is not None:
                try:
                    progress_cb(summary)
                except Exception as e:
                    logger.warning("Import progress callback raised (ignored): %s", e)
    except Exception as e:
        # An adapter that raised mid-stream (e.g. a corrupt archive central
        # directory) — record it on the audit line, then re-raise.
        success = False
        error = str(e)
        raise
    finally:
        log_api_call(
            config,
            "import",
            endpoint=kind,
            count=summary["imported"],
            duration_ms=int((time.monotonic() - started) * 1000),
            success=success,
            error=error,
        )
    return summary
