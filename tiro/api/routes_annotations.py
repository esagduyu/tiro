"""Highlights + notes CRUD API (Phase 2 M2.1, Task 3).

Sidecar-first writes: every mutation writes the sidecar FILE first (truth,
`tiro/annotations.py`'s `read_annotations`/`write_annotations`/`read_note`/
`write_note`/`delete_note` primitives), THEN updates the derived `highlights`/
`notes` SQLite rows. A crash between the two leaves the file ahead of the
index -- `reconcile_annotations()` (run on every boot and by `tiro doctor`)
heals the drift on its own, files-win. See each handler below for the exact
order and what a partial failure leaves behind.

Anchoring text: the article BODY as `GET /api/articles/{id}` serves it --
`frontmatter.load(path).content` (post-frontmatter markdown), read from
`config.articles_dir / markdown_path`. This is deliberately the same text
M2.2's reader displays and the same text `reconcile_anchor()` will be asked
to re-locate anchors against later, so offsets never disagree between what
the user highlighted, what's stored, and what's redrawn.

The flat `GET /api/highlights` list's WHERE-builder lives HERE, not in
`tiro/queries.py` -- that module is documented as the single owner of the
ARTICLE list shape only (`ARTICLE_COLUMNS`/`ARTICLE_FROM`/`SORT_SQL`/
`build_article_filters`); highlights are a different row shape with a much
smaller filter set, so a second tiny builder living next to its one caller
is simpler than widening queries.py's contract.

No-await invariant: these sidecar read-modify-write handlers (read file,
mutate, write file, then update the row) are lost-update-safe ONLY because
every handler below contains ZERO `await` points between its read and its
write -- Python's single-threaded event loop runs each `async def` handler
to completion, uninterrupted, once it starts. Inserting an `await` inside a
mutation handler (e.g. an accidental async I/O call between the read and
the write) would open a window where a second request could interleave and
clobber the first's write -- a lost update. Keep these handlers await-free
across their read-modify-write span.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path

import frontmatter
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tiro.anchors import content_hash, make_anchor, reconcile_anchor
from tiro.annotations import (
    annotations_dir,
    append_highlight,
    delete_note,
    read_annotations,
    sidecar_stem,
    upsert_article_note,
    write_annotations,
)
from tiro.database import get_connection
from tiro.migrations import new_ulid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["annotations"])

COLORS = {"yellow", "green", "blue", "pink"}
DEFAULT_COLOR = "yellow"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_color(color: str | None) -> str:
    if color is None:
        return DEFAULT_COLOR
    if color not in COLORS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid color {color!r}; must be one of {sorted(COLORS)}",
        )
    return color


def _get_article_row(conn, article_id: int):
    row = conn.execute(
        "SELECT id, uid, markdown_path FROM articles WHERE id = ?", (article_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return row


def _read_article_body(config, article_row) -> str:
    """Same resolution + read as `GET /api/articles/{id}` (routes_articles.py):
    `config.articles_dir / markdown_path`, python-frontmatter, `.content`
    (post-frontmatter body). Missing file logs a warning and yields ""
    rather than raising -- mirrors the existing article-read tolerance."""
    md_path = Path(article_row["markdown_path"])
    if not md_path.is_absolute():
        md_path = config.articles_dir / md_path
    if not md_path.exists():
        logger.warning("Markdown file not found for annotations: %s", md_path)
        return ""
    return frontmatter.load(str(md_path)).content


def _highlight_row_to_dict(row, note_markdown: str | None) -> dict:
    return {
        "uid": row["uid"],
        "color": row["color"],
        "quote_text": row["quote_text"],
        "prefix_context": row["prefix_context"],
        "suffix_context": row["suffix_context"],
        "text_position_start": row["text_position_start"],
        "text_position_end": row["text_position_end"],
        "content_hash": row["content_hash"],
        "note_markdown": note_markdown,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _note_for_highlight(conn, highlight_id: int) -> str | None:
    row = conn.execute(
        "SELECT body_markdown FROM notes WHERE highlight_id = ?", (highlight_id,)
    ).fetchone()
    return row["body_markdown"] if row else None


def _line_from_highlight_row(article_uid: str, row, note_markdown: str | None) -> dict:
    """Reconstruct a JSONL line dict from a `highlights` row -- used only as
    a self-heal fallback (see PATCH/DELETE) when a highlight's row exists but
    its line has gone missing from the sidecar file (drift from something
    other than this module)."""
    return {
        "uid": row["uid"],
        "article_uid": article_uid,
        "quote": row["quote_text"],
        "prefix": row["prefix_context"],
        "suffix": row["suffix_context"],
        "position_start": row["text_position_start"],
        "position_end": row["text_position_end"],
        "content_hash": row["content_hash"],
        "color": row["color"],
        "note_markdown": note_markdown,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get_highlight_or_404(conn, uid: str):
    """Fetch a highlight row plus its owning article row by highlight uid.
    `highlights.uid` is UNIQUE (migration 009), so this lookup unambiguously
    resolves the one article/stem a given uid belongs to -- cross-article
    isolation falls out of this rather than needing separate enforcement.

    Defense-in-depth: `delete_article()`'s cascade (Phase 2 M2.1 Task 4)
    makes a highlight outliving its article impossible going forward, but a
    missing article row must still 404 cleanly here rather than crash on
    `article["uid"]` a few lines down in the caller (graceful error handling
    everywhere, per CLAUDE.md)."""
    row = conn.execute("SELECT * FROM highlights WHERE uid = ?", (uid,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Highlight not found")
    article = conn.execute(
        "SELECT id, uid, markdown_path FROM articles WHERE id = ?", (row["article_id"],)
    ).fetchone()
    if article is None:
        raise HTTPException(status_code=404, detail="Highlight not found")
    return row, article


# --- GET /api/articles/{id}/annotations --------------------------------------


@router.get("/articles/{article_id}/annotations")
async def get_annotations(article_id: int, request: Request):
    """The one-call payload M2.2's reader loads: every highlight (+ its
    inline note + a live `anchor_status` computed via `reconcile_anchor`
    against the CURRENT article body), the article-level note if any, and
    the current body's `content_hash`."""
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        article = _get_article_row(conn, article_id)
        body = _read_article_body(config, article)
        current_hash = content_hash(body)

        rows = conn.execute(
            "SELECT * FROM highlights WHERE article_id = ? "
            "ORDER BY text_position_start IS NULL, text_position_start, created_at",
            (article_id,),
        ).fetchall()

        highlights = []
        for row in rows:
            note_markdown = _note_for_highlight(conn, row["id"])
            anchor = {
                "quote": row["quote_text"],
                "prefix": row["prefix_context"],
                "suffix": row["suffix_context"],
                "position_start": row["text_position_start"],
                "position_end": row["text_position_end"],
                "content_hash": row["content_hash"],
            }
            data = _highlight_row_to_dict(row, note_markdown)
            data["anchor_status"] = reconcile_anchor(body, anchor)
            highlights.append(data)

        note_row = conn.execute(
            "SELECT uid, body_markdown, updated_at FROM notes "
            "WHERE article_id = ? AND highlight_id IS NULL",
            (article_id,),
        ).fetchone()
        note = dict(note_row) if note_row else None

        return {
            "success": True,
            "data": {
                "highlights": highlights,
                "note": note,
                "content_hash": current_hash,
            },
        }
    finally:
        conn.close()


# --- POST /api/articles/{id}/highlights --------------------------------------


class HighlightCreateRequest(BaseModel):
    position_start: int
    position_end: int
    color: str | None = None


@router.post("/articles/{article_id}/highlights")
async def create_highlight(article_id: int, body: HighlightCreateRequest, request: Request):
    """Server derives quote/prefix/suffix from the CURRENT article body via
    `make_anchor` -- the client sends offsets only, so it can't forge the
    stored context. `content_hash` of the current body is stored alongside.
    400 on a bad color or an out-of-bounds/inverted range (`make_anchor`'s
    `ValueError`); 404 on an unknown article."""
    config = request.app.state.config
    color = _validate_color(body.color)

    conn = get_connection(config.db_path)
    try:
        article = _get_article_row(conn, article_id)
        text = _read_article_body(config, article)
        try:
            anchor = make_anchor(text, body.position_start, body.position_end)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        now = _now_iso()
        current_hash = content_hash(text)

        # Sidecar-first (file then row, M2.1 invariant) via the shared helper
        # the importer also uses -- one place owns the line shape + order. If
        # the row insert fails after the file write, the sidecar is ahead of
        # SQLite and the next `reconcile_annotations()` run heals the drift.
        uid = append_highlight(
            config,
            conn,
            article,
            quote=anchor["quote"],
            prefix=anchor["prefix"],
            suffix=anchor["suffix"],
            position_start=anchor["position_start"],
            position_end=anchor["position_end"],
            content_hash=current_hash,
            color=color,
            now=now,
        )
        conn.commit()

        row = conn.execute("SELECT * FROM highlights WHERE uid = ?", (uid,)).fetchone()
        return {"success": True, "data": _highlight_row_to_dict(row, None)}
    finally:
        conn.close()


# --- PATCH /api/highlights/{uid} ----------------------------------------------


class HighlightPatchRequest(BaseModel):
    color: str | None = None
    note_markdown: str | None = None


@router.patch("/highlights/{uid}")
async def patch_highlight(uid: str, body: HighlightPatchRequest, request: Request):
    """Update a highlight's color and/or its anchored note. `note_markdown`
    omitted (`None`) leaves the note untouched; `""` OR whitespace-only
    clears it (deletes the note, same as PUT .../note's empty-body 400
    threshold -- whitespace-only is treated as "nothing," not content); any
    other string upserts it verbatim (no sanitize/transform -- the client
    renders through DOMPurify). 404 on an unknown uid. A true no-op PATCH
    (both fields omitted, `{}`) short-circuits before any file/row write or
    `updated_at` bump -- it returns the highlight unchanged rather than
    rewriting the sidecar for nothing."""
    config = request.app.state.config
    color = None if body.color is None else _validate_color(body.color)

    conn = get_connection(config.db_path)
    try:
        row, article = _get_highlight_or_404(conn, uid)

        if body.color is None and body.note_markdown is None:
            return {
                "success": True,
                "data": _highlight_row_to_dict(row, _note_for_highlight(conn, row["id"])),
            }

        stem = sidecar_stem(article)
        now = _now_iso()
        new_color = color if color is not None else row["color"]

        current_note = _note_for_highlight(conn, row["id"])
        new_note = (
            current_note
            if body.note_markdown is None
            else (body.note_markdown if body.note_markdown.strip() else None)
        )

        # 1. FILE FIRST: locate the line by uid and rewrite it in place. If
        # the line is missing from the file (drift from outside this
        # module -- the file is truth, so this should not happen via normal
        # use), reconstruct it from the row's current values so the file
        # still ends up holding a superset of the truth rather than losing
        # the update.
        lines = read_annotations(config, stem)
        found = False
        for line in lines:
            if line.get("uid") == uid:
                line["color"] = new_color
                line["note_markdown"] = new_note
                line["updated_at"] = now
                found = True
                break
        if not found:
            lines.append(
                _line_from_highlight_row(article["uid"], row, current_note)
                | {"color": new_color, "note_markdown": new_note, "updated_at": now}
            )
        write_annotations(config, stem, lines)

        # 2. INDEX SECOND.
        conn.execute(
            "UPDATE highlights SET color = ?, updated_at = ? WHERE id = ?",
            (new_color, now, row["id"]),
        )
        existing_note = conn.execute(
            "SELECT * FROM notes WHERE highlight_id = ?", (row["id"],)
        ).fetchone()
        if new_note is None:
            if existing_note is not None:
                conn.execute("DELETE FROM notes WHERE id = ?", (existing_note["id"],))
        elif existing_note is None:
            conn.execute(
                "INSERT INTO notes (uid, article_id, highlight_id, body_markdown,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (new_ulid(), row["article_id"], row["id"], new_note, now, now),
            )
        else:
            conn.execute(
                "UPDATE notes SET body_markdown = ?, updated_at = ? WHERE id = ?",
                (new_note, now, existing_note["id"]),
            )
        conn.commit()

        updated_row = conn.execute("SELECT * FROM highlights WHERE id = ?", (row["id"],)).fetchone()
        return {
            "success": True,
            "data": _highlight_row_to_dict(updated_row, _note_for_highlight(conn, row["id"])),
        }
    finally:
        conn.close()


# --- DELETE /api/highlights/{uid} ---------------------------------------------


@router.delete("/highlights/{uid}")
async def delete_highlight(uid: str, request: Request):
    """Remove a highlight, its sidecar line, and its anchored note (if any).
    404 on an unknown uid."""
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        row, article = _get_highlight_or_404(conn, uid)
        stem = sidecar_stem(article)

        # 1. FILE FIRST: drop the line; unlink the sidecar entirely once it
        # would otherwise be empty (matches `rebuild_sidecars_for_article`'s
        # "no highlights -> no file" convention, not just a dumb empty write).
        lines = [line for line in read_annotations(config, stem) if line.get("uid") != uid]
        if lines:
            write_annotations(config, stem, lines)
        else:
            (annotations_dir(config) / f"{stem}.jsonl").unlink(missing_ok=True)

        # 2. INDEX SECOND: cascade-delete the anchored note row first (no ON
        # DELETE CASCADE on notes.highlight_id), then the highlight row.
        conn.execute("DELETE FROM notes WHERE highlight_id = ?", (row["id"],))
        conn.execute("DELETE FROM highlights WHERE id = ?", (row["id"],))
        conn.commit()

        return {"success": True}
    finally:
        conn.close()


# --- PUT/DELETE /api/articles/{id}/note --------------------------------------


class NoteRequest(BaseModel):
    body_markdown: str


@router.put("/articles/{article_id}/note")
async def upsert_note(article_id: int, body: NoteRequest, request: Request):
    """Upsert the article-level note. Raw markdown stored as-is (no
    sanitize/transform -- the client renders through DOMPurify). An empty
    (or whitespace-only) `body_markdown` is 400 -- use DELETE instead."""
    config = request.app.state.config
    if not body.body_markdown.strip():
        raise HTTPException(
            status_code=400, detail="body_markdown must not be empty; use DELETE to remove a note"
        )

    conn = get_connection(config.db_path)
    try:
        _get_article_row(conn, article_id)   # keeps the exact 404 behavior
    finally:
        conn.close()

    data = upsert_article_note(config, article_id, body.body_markdown)
    return {"success": True, "data": data}


@router.delete("/articles/{article_id}/note")
async def remove_note(article_id: int, request: Request):
    """Delete the article-level note. Idempotent: no-op (still 200) if there
    isn't one. 404 on an unknown article."""
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        article = _get_article_row(conn, article_id)
        stem = sidecar_stem(article)

        # 1. FILE FIRST.
        delete_note(config, stem)

        # 2. INDEX SECOND.
        conn.execute(
            "DELETE FROM notes WHERE article_id = ? AND highlight_id IS NULL", (article_id,)
        )
        conn.commit()

        return {"success": True}
    finally:
        conn.close()


# --- GET /api/highlights (flat review list) ----------------------------------


def _build_highlight_filters(
    *,
    article_id: int | None,
    source_id: int | None,
    color: str | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, list]:
    """Tiny WHERE-builder for the flat highlights list. Deliberately NOT in
    `tiro/queries.py` -- that module's docstring reserves it for the ARTICLE
    list shape only; this is a different, much smaller row shape with its
    one caller living right here."""
    clauses: list[str] = []
    params: list = []
    if article_id is not None:
        clauses.append("h.article_id = ?")
        params.append(article_id)
    if source_id is not None:
        clauses.append("a.source_id = ?")
        params.append(source_id)
    if color is not None:
        clauses.append("h.color = ?")
        params.append(color)
    if date_from:
        clauses.append("h.created_at >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("h.created_at <= ?")
        params.append(date_to + "T23:59:59Z")
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where_sql, params


@router.get("/highlights")
async def list_highlights(
    request: Request,
    article_id: int | None = None,
    source_id: int | None = None,
    color: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Flat highlights list joined with article title + source, for M2.2's
    review view."""
    config = request.app.state.config
    where_sql, params = _build_highlight_filters(
        article_id=article_id,
        source_id=source_id,
        color=color,
        date_from=date_from,
        date_to=date_to,
    )
    conn = get_connection(config.db_path)
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM highlights h "
            f"JOIN articles a ON h.article_id = a.id{where_sql}",
            params,
        ).fetchone()["n"]

        rows = conn.execute(
            f"""SELECT h.uid, h.color, h.quote_text, h.prefix_context, h.suffix_context,
                       h.text_position_start, h.text_position_end, h.content_hash,
                       h.created_at, h.updated_at,
                       a.id AS article_id, a.title AS article_title,
                       s.id AS source_id, s.name AS source_name
                FROM highlights h
                JOIN articles a ON h.article_id = a.id
                LEFT JOIN sources s ON a.source_id = s.id
                {where_sql}
                ORDER BY h.created_at DESC
                LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        ).fetchall()

        highlights = []
        for row in rows:
            data = dict(row)
            data["note_markdown"] = _note_for_highlight_by_uid(conn, row["uid"])
            highlights.append(data)

        return {"success": True, "data": {"highlights": highlights, "total": total}}
    finally:
        conn.close()


def _note_for_highlight_by_uid(conn, highlight_uid: str) -> str | None:
    row = conn.execute(
        "SELECT n.body_markdown FROM notes n "
        "JOIN highlights h ON n.highlight_id = h.id WHERE h.uid = ?",
        (highlight_uid,),
    ).fetchone()
    return row["body_markdown"] if row else None
