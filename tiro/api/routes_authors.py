"""Author management API routes.

Mirrors `routes_sources.py`'s list/VIP-toggle/merge shapes exactly, layered
on the `authors`/`article_authors` tables (migration 007) and the
`tiro.authors` helper module. See that module's docstring for why
`articles.author` (free text) and the `authors` table (deduped identity)
are kept separate.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tiro.authors import merge_authors
from tiro.database import get_connection

router = APIRouter(prefix="/api/authors", tags=["authors"])


@router.get("")
async def list_authors(request: Request):
    """List all authors with article counts."""
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute("""
            SELECT au.id, au.uid, au.name, au.is_vip,
                   COUNT(aa.article_id) AS article_count
            FROM authors au
            LEFT JOIN article_authors aa ON au.id = aa.author_id
            GROUP BY au.id
            ORDER BY au.is_vip DESC, au.name ASC
        """).fetchall()

        return {"success": True, "data": [dict(r) for r in rows]}
    finally:
        conn.close()


@router.patch("/{author_id}/vip")
async def toggle_vip(author_id: int, request: Request):
    """Toggle VIP status for an author."""
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT is_vip FROM authors WHERE id = ?", (author_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Author not found")

        new_vip = not bool(row["is_vip"])
        conn.execute(
            "UPDATE authors SET is_vip = ? WHERE id = ?", (new_vip, author_id)
        )
        conn.commit()

        return {"success": True, "data": {"id": author_id, "is_vip": new_vip}}
    finally:
        conn.close()


class MergeRequest(BaseModel):
    keep_id: int
    merge_id: int


@router.post("/merge")
async def merge_authors_route(body: MergeRequest, request: Request):
    """Merge `merge_id` into `keep_id` via `tiro.authors.merge_authors`."""
    config = request.app.state.config

    if body.keep_id == body.merge_id:
        raise HTTPException(
            status_code=400, detail="keep_id and merge_id must be different"
        )

    conn = get_connection(config.db_path)
    try:
        keep_row = conn.execute(
            "SELECT id FROM authors WHERE id = ?", (body.keep_id,)
        ).fetchone()
        merge_row = conn.execute(
            "SELECT id FROM authors WHERE id = ?", (body.merge_id,)
        ).fetchone()
        if not keep_row or not merge_row:
            raise HTTPException(status_code=404, detail="Unknown author id")

        merge_authors(conn, body.keep_id, body.merge_id)
        conn.commit()

        return {"success": True, "data": {"keep_id": body.keep_id}}
    finally:
        conn.close()
