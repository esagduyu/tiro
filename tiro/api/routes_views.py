"""Saved views API routes.

Named filter+sort presets, backed by the `saved_views` table (migration 007,
Phase 1 M1.2). `filter_json` is stored as the raw JSON *string* the client
sent (not re-serialized) — the only server-side requirement is that it
`json.loads()` to a dict, so arbitrary filter shapes stay forward-compatible
without a server-side schema. `position` is a plain integer the client
reorders via two PATCH calls (swap semantics live in the UI, Task 8) —
this router just persists whatever position it's told.
"""

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from tiro.database import get_connection
from tiro.migrations import new_ulid

router = APIRouter(prefix="/api/views", tags=["views"])

MAX_SAVED_VIEWS = 20

_COLUMNS = "id, uid, name, filter_json, sort_mode, position"


@router.get("")
async def list_views(request: Request):
    """List saved views ordered by position ASC, id ASC."""
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM saved_views ORDER BY position ASC, id ASC"
        ).fetchall()
        return {"success": True, "data": [dict(r) for r in rows]}
    finally:
        conn.close()


class ViewCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    filter_json: str
    sort_mode: str = "unread"


@router.post("")
async def create_view(body: ViewCreate, request: Request):
    """Create a saved view. 400 if filter_json isn't a JSON object, or if
    the library is already at MAX_SAVED_VIEWS."""
    try:
        parsed = json.loads(body.filter_json)
    except (json.JSONDecodeError, TypeError) as err:
        raise HTTPException(
            status_code=400, detail="filter_json must be valid JSON"
        ) from err
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="filter_json must be a JSON object")

    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM saved_views").fetchone()["n"]
        if count >= MAX_SAVED_VIEWS:
            raise HTTPException(
                status_code=400,
                detail=f"Maximum of {MAX_SAVED_VIEWS} saved views reached",
            )

        max_pos = conn.execute(
            "SELECT MAX(position) AS m FROM saved_views"
        ).fetchone()["m"]
        position = (max_pos if max_pos is not None else -1) + 1
        uid = new_ulid()

        conn.execute(
            "INSERT INTO saved_views (uid, name, filter_json, sort_mode, position)"
            " VALUES (?, ?, ?, ?, ?)",
            (uid, body.name, body.filter_json, body.sort_mode, position),
        )
        conn.commit()

        row = conn.execute(
            f"SELECT {_COLUMNS} FROM saved_views WHERE uid = ?", (uid,)
        ).fetchone()
        return {"success": True, "data": dict(row)}
    finally:
        conn.close()


class ViewUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    position: int | None = None


@router.patch("/{view_id}")
async def update_view(view_id: int, body: ViewUpdate, request: Request):
    """Partially update a saved view's name/position. Server sets whatever
    it's told — reorder logic (e.g. swapping two views) lives client-side."""
    config = request.app.state.config
    updates = body.model_dump(exclude_unset=True)

    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT id FROM saved_views WHERE id = ?", (view_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="View not found")

        if updates:
            set_clause = ", ".join(f"{field} = ?" for field in updates)
            conn.execute(
                f"UPDATE saved_views SET {set_clause} WHERE id = ?",
                (*updates.values(), view_id),
            )
            conn.commit()

        row = conn.execute(
            f"SELECT {_COLUMNS} FROM saved_views WHERE id = ?", (view_id,)
        ).fetchone()
        return {"success": True, "data": dict(row)}
    finally:
        conn.close()


@router.delete("/{view_id}")
async def delete_view(view_id: int, request: Request):
    """Delete a saved view. No auto-backup — views are cheap to recreate."""
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT id FROM saved_views WHERE id = ?", (view_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="View not found")

        conn.execute("DELETE FROM saved_views WHERE id = ?", (view_id,))
        conn.commit()

        return {"success": True, "data": {"id": view_id}}
    finally:
        conn.close()
