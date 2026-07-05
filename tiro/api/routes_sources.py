"""Source management API routes."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tiro.database import get_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sources", tags=["sources"])


@router.get("")
async def list_sources(request: Request):
    """List all sources with article counts."""
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute("""
            SELECT s.id, s.name, s.domain, s.email_sender, s.source_type,
                   s.is_vip, COUNT(a.id) AS article_count
            FROM sources s
            LEFT JOIN articles a ON s.id = a.source_id
            GROUP BY s.id
            ORDER BY s.is_vip DESC, s.name ASC
        """).fetchall()

        return {"success": True, "data": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.patch("/{source_id}/vip")
async def toggle_vip(source_id: int, request: Request):
    """Toggle VIP status for a source."""
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT is_vip FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Source not found")

        new_vip = not bool(row["is_vip"])
        conn.execute(
            "UPDATE sources SET is_vip = ? WHERE id = ?", (new_vip, source_id)
        )
        conn.commit()

        return {"success": True, "data": {"id": source_id, "is_vip": new_vip}}
    finally:
        conn.close()


def _delete_source_and_articles(config, source_id: int) -> int:
    """Sync helper run inside a single `asyncio.to_thread` call.

    Collects the source's article ids (closing the read connection before
    doing any writes), deletes each article through the lifecycle
    coordinator (`delete_article` — it manages its own SQLite connection per
    call and is idempotent/best-effort per store), then deletes the source
    row itself. One `to_thread` wrapping this whole helper is used rather
    than one `to_thread` per `delete_article` call: the work is inherently
    sequential (each article deletion opens/closes its own connection
    already), so hopping back to the event loop between every article would
    add N thread-pool round-trips for no benefit — a single worker thread
    running the loop start-to-finish is simpler and just as safe.
    """
    from tiro.lifecycle import delete_article

    conn = get_connection(config.db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM articles WHERE source_id = ?", (source_id,)
        ).fetchall()
        article_ids = [r["id"] for r in rows]
    finally:
        conn.close()

    deleted_count = 0
    for article_id in article_ids:
        if delete_article(config, article_id):
            deleted_count += 1

    conn = get_connection(config.db_path)
    try:
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        conn.commit()
    finally:
        conn.close()

    return deleted_count


@router.delete("/{source_id}")
async def delete_source(source_id: int, request: Request):
    """Delete a source and all of its articles (all four stores) at once."""
    from tiro.backup import auto_backup

    config = request.app.state.config

    conn = get_connection(config.db_path)
    try:
        exists = conn.execute(
            "SELECT id FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
    finally:
        conn.close()
    if not exists:
        raise HTTPException(status_code=404, detail="Source not found")

    # Snapshot before the destructive cascade. auto_backup never raises.
    await asyncio.to_thread(auto_backup, config, "source-delete")

    deleted_articles = await asyncio.to_thread(
        _delete_source_and_articles, config, source_id
    )

    return {"success": True, "data": {"deleted_articles": deleted_articles}}


class MergeRequest(BaseModel):
    from_id: int
    into_id: int
    force: bool = False


@router.post("/merge")
async def merge_sources(body: MergeRequest, request: Request):
    """Merge one source into another: repoint articles, OR the VIP flag,
    then delete the losing source. Single connection/transaction."""
    config = request.app.state.config

    if body.from_id == body.into_id:
        raise HTTPException(
            status_code=400, detail="from_id and into_id must be different"
        )

    conn = get_connection(config.db_path)
    try:
        from_row = conn.execute(
            "SELECT id, source_type, is_vip FROM sources WHERE id = ?",
            (body.from_id,),
        ).fetchone()
        into_row = conn.execute(
            "SELECT id, source_type, is_vip FROM sources WHERE id = ?",
            (body.into_id,),
        ).fetchone()
        if not from_row or not into_row:
            raise HTTPException(status_code=400, detail="Unknown source id")

        if from_row["source_type"] != into_row["source_type"] and not body.force:
            return JSONResponse(
                status_code=409,
                content={"success": False, "error": "type_mismatch"},
            )

        cursor = conn.execute(
            "UPDATE articles SET source_id = ? WHERE source_id = ?",
            (body.into_id, body.from_id),
        )
        moved = cursor.rowcount

        if from_row["is_vip"]:
            conn.execute(
                "UPDATE sources SET is_vip = 1 WHERE id = ?", (body.into_id,)
            )

        conn.execute("DELETE FROM sources WHERE id = ?", (body.from_id,))
        conn.commit()

        return {"success": True, "data": {"moved_articles": moved}}
    finally:
        conn.close()


class SourceUpdate(BaseModel):
    name: str | None = None
    domain: str | None = None
    email_sender: str | None = None


@router.patch("/{source_id}")
async def update_source(source_id: int, body: SourceUpdate, request: Request):
    """Partially update a source's name/domain/email_sender."""
    config = request.app.state.config
    updates = body.model_dump(exclude_unset=True)

    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT id FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Source not found")

        if updates:
            set_clause = ", ".join(f"{field} = ?" for field in updates)
            conn.execute(
                f"UPDATE sources SET {set_clause} WHERE id = ?",
                (*updates.values(), source_id),
            )
            conn.commit()

        row = conn.execute(
            "SELECT id, name, domain, email_sender, source_type, is_vip "
            "FROM sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        return {"success": True, "data": dict(row)}
    finally:
        conn.close()
