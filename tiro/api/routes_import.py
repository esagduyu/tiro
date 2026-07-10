"""Background library-import API (Phase 4 M4.2, spec D6).

`POST /api/import/{kind}` (kind ∈ readwise/instapaper/omnivore) accepts a
multipart file upload and starts a **single-slot** background job: a second
POST while one is active 409s (`import_running`). The job runs as a scheduler
one-shot (`scheduler.start("import", ...)`) with its live progress dict on
`app.state.import_job`; `GET /api/import/status` reads it. Job state is
in-memory only — a restart forgets a finished report (the CLI verbs are the
durable path). Blocking work (`run_import`, which does DB/file writes and the
adapter's parse) runs inside `asyncio.to_thread` so the event loop is never
blocked; the progress callback mutates the shared dict between items.
"""

import asyncio
import logging
import os
import tempfile
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response, UploadFile

from tiro.ingestion.importers import instapaper, omnivore, readwise
from tiro.ingestion.importers.base import run_import

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["import"])

# kind -> adapter module (each exposes `parse_export(path)`).
_ADAPTERS = {
    "readwise": readwise,
    "instapaper": instapaper,
    "omnivore": omnivore,
}

# Progress/summary fields mirrored from `run_import`'s summary into the job.
_SUMMARY_KEYS = (
    "total",
    "processed",
    "imported",
    "skipped",
    "failed",
    "stub_articles",
    "highlights_imported",
    "highlights_skipped",
)

# Upload ceiling — generous for a whole-library export, bounded so a hostile
# upload can't exhaust memory before the adapter's own per-member guards.
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_job(kind: str) -> dict:
    return {
        "kind": kind,
        "running": True,
        "total": 0,
        "processed": 0,
        "imported": 0,
        "skipped": 0,
        "failed": 0,
        "stub_articles": 0,
        "highlights_imported": 0,
        "highlights_skipped": 0,
        "error": None,
        "started_at": _now_iso(),
        "finished_at": None,
    }


async def _run_job(app_state, config, kind: str, tmp_path: str) -> None:
    """Scheduler one-shot body: drive `run_import` in a worker thread, keeping
    `app_state.import_job` live. Never raises (errors are captured into the job
    dict) so the background task can't crash the server."""
    job = app_state.import_job

    def progress_cb(summary: dict) -> None:
        for key in _SUMMARY_KEYS:
            job[key] = summary[key]

    def _blocking() -> dict:
        adapter = _ADAPTERS[kind]
        return run_import(config, adapter.parse_export(tmp_path), kind=kind, progress_cb=progress_cb)

    try:
        summary = await asyncio.to_thread(_blocking)
        for key in _SUMMARY_KEYS:
            job[key] = summary[key]
    except Exception as e:  # noqa: BLE001 — a bad export must not crash the server
        logger.error("Import job (%s) failed: %s", kind, e)
        job["error"] = str(e)
    finally:
        job["running"] = False
        job["finished_at"] = _now_iso()
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@router.post("/import/{kind}")
async def start_import(kind: str, file: UploadFile, request: Request, response: Response):
    """Start a background import from an uploaded export file. 400 on an
    unknown kind or oversize upload; 409 `import_running` if a job is already
    active. Returns a 202-style ack with the initial job state."""
    if kind not in _ADAPTERS:
        raise HTTPException(status_code=400, detail=f"Unknown import kind {kind!r}")

    config = request.app.state.config
    existing = getattr(request.app.state, "import_job", None)
    if existing is not None and existing.get("running"):
        raise HTTPException(status_code=409, detail={"error": "import_running"})

    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="Upload exceeds 100 MB")
    if not raw:
        raise HTTPException(status_code=400, detail="Empty upload")

    fd, tmp_path = tempfile.mkstemp(prefix="tiro-import-", suffix=f"-{kind}")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not stage upload: {e}") from e

    request.app.state.import_job = _new_job(kind)
    request.app.state.scheduler.start(
        "import", _run_job(request.app.state, config, kind, tmp_path)
    )

    response.status_code = 202
    return {"success": True, "data": request.app.state.import_job}


@router.get("/import/status")
async def import_status(request: Request):
    """Return the current/last import job's progress dict, or `{running: false}`
    when none has run since startup."""
    job = getattr(request.app.state, "import_job", None)
    if job is None:
        return {"success": True, "data": {"running": False}}
    return {"success": True, "data": job}
