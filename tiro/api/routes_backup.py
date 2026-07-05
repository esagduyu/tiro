"""Backup snapshots listing."""

from fastapi import APIRouter, Request

from tiro.backup import list_snapshots

router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.get("/snapshots")
async def get_snapshots(request: Request):
    """List manual + auto snapshots, newest first."""
    config = request.app.state.config
    return {"success": True, "data": {"snapshots": list_snapshots(config)}}
