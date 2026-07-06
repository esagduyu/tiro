"""Reading-session telemetry API (Phase 2 M2.3, Task 1).

Opt-in, strictly local-only signal — feeds the future wiki-importance
ranking (Decision #8). The server itself enforces the privacy posture: when
`reading_telemetry_enabled` is False, `POST /api/articles/{id}/session`
no-ops (204, no row inserted) even if a client sends a payload — refusing
data is a server-side guarantee, not just a client-side toggle.

Endpoint is POST, not PATCH (a deviation from the roadmap's original
wording, decided at the plan level): the reader-side tracker (Task 2) sends
via `navigator.sendBeacon`, which can only issue POST requests.

One row per reader visit; sessions are ephemeral telemetry, not
user-authored content, so there is no sidecar file (unlike wiki_pages/
highlights/notes) — SQLite is the only store, mirroring migration 010's
`reading_sessions` table.
"""

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ValidationError

from tiro.database import get_connection
from tiro.migrations import new_ulid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/articles", tags=["sessions"])

MAX_ACTIVE_SECONDS = 86400
MAX_DWELL_ENTRIES = 100
MAX_HEADING_CHARS = 200


class DwellEntry(BaseModel):
    heading: str = ""
    seconds: int = 0


class SessionPayload(BaseModel):
    started_at: str | None = None
    max_scroll_pct: int = 0
    active_seconds: int = 0
    dwell: list[DwellEntry] = []


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


@router.post("/{article_id}/session")
async def record_reading_session(article_id: int, request: Request):
    """Record one reading-session telemetry row for an article visit.

    Body: {started_at, max_scroll_pct, active_seconds, dwell: [{heading, seconds}]}.
    When telemetry is disabled, the flag is checked FIRST -- before body
    parsing/validation or the article lookup -- so a disabled server 204
    no-ops (no insert, no error) even for a malformed body or an unknown
    article id. This is a deliberate ordering: "disabled means disabled"
    must not leak information via 400s or 404s. Only when enabled do the
    usual checks apply: malformed JSON/shape -> 400, unknown article -> 404,
    then validated + clamped (see module docstring / task brief for the
    exact table) and inserted.
    """
    config = request.app.state.config

    if not config.reading_telemetry_enabled:
        return Response(status_code=204)

    try:
        raw_body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Malformed JSON body") from e

    try:
        payload = SessionPayload.model_validate(raw_body)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT id FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Article not found")

        max_scroll_pct = _clamp(payload.max_scroll_pct, 0, 100)
        active_seconds = _clamp(payload.active_seconds, 0, MAX_ACTIVE_SECONDS)

        dwell = [
            {
                "heading": entry.heading[:MAX_HEADING_CHARS],
                "seconds": _clamp(entry.seconds, 0, MAX_ACTIVE_SECONDS),
            }
            for entry in payload.dwell[:MAX_DWELL_ENTRIES]
        ]

        conn.execute(
            """
            INSERT INTO reading_sessions
                (uid, article_id, started_at, ended_at, max_scroll_pct, active_seconds, dwell_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_ulid(),
                article_id,
                payload.started_at,
                _now_iso(),
                max_scroll_pct,
                active_seconds,
                json.dumps(dwell),
            ),
        )
        conn.commit()

        return {"success": True, "data": {"recorded": True}}
    finally:
        conn.close()
