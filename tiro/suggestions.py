"""Suggestions storage (Phase 6 K3) -- the persona write surface.

Rows are pending|accepted|dismissed; ctx.suggest is the only producer
during a run; accept/dismiss (routes_personas.py) is the only consumer
that changes status. Payloads are stored as DATA (raw JSON) -- anything
that renders them must go through the standard sanitize path
(renderMarkdown -> DOMPurify client-side); anything that reads them
treats them as quoted data, never instructions (spec §5).
Appliers (accept path) are added in K3 Task 6.
"""

import json
import logging
from datetime import UTC, datetime

from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.migrations import new_ulid

logger = logging.getLogger(__name__)

SUGGESTION_KINDS = {"note", "digest_section", "wiki_page",
                    "tier_suggestion", "contradiction"}
SUGGESTION_STATUSES = {"pending", "accepted", "dismissed"}


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["payload"] = json.loads(d.pop("payload_json") or "{}")
    d["citations"] = json.loads(d.pop("citations_json") or "[]")
    return d


def create_suggestion(config: TiroConfig, *, persona: str, kind: str,
                      payload: dict, citations: list[str]) -> dict:
    if kind not in SUGGESTION_KINDS:
        raise ValueError(f"invalid suggestion kind: {kind!r}")
    uid = new_ulid()
    now = _now_iso()
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            """INSERT INTO suggestions
               (uid, persona, kind, payload_json, citations_json,
                created_at, status)
               VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
            (uid, persona, kind, json.dumps(payload, default=str),
             json.dumps(citations), now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"uid": uid, "persona": persona, "kind": kind, "payload": payload,
            "citations": citations, "created_at": now, "status": "pending"}


def list_suggestions(config: TiroConfig, *, status: str | None = None,
                     article_id: int | None = None,
                     limit: int = 100) -> list[dict]:
    where, params = [], []
    if status is not None:
        if status not in SUGGESTION_STATUSES:
            raise ValueError(f"invalid status: {status!r}")
        where.append("status = ?")
        params.append(status)
    if article_id is not None:
        # No article_id column by design (spec §5 columns are frozen);
        # note/tier payloads carry it -- SQLite JSON1 extracts it.
        where.append("json_extract(payload_json, '$.article_id') = ?")
        params.append(article_id)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute(
            f"""SELECT * FROM suggestions {where_sql}
                ORDER BY created_at DESC, id DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def get_suggestion(config: TiroConfig, uid: str) -> dict | None:
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM suggestions WHERE uid = ?", (uid,)).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row else None


def set_suggestion_status(config: TiroConfig, uid: str, status: str) -> bool:
    """Flip a PENDING suggestion to accepted/dismissed. Returns False when
    the row is missing or already resolved (single atomic UPDATE -- no
    SELECT-then-UPDATE window, same posture as login-token redemption)."""
    if status not in ("accepted", "dismissed"):
        raise ValueError(f"invalid target status: {status!r}")
    conn = get_connection(config.db_path)
    try:
        cur = conn.execute(
            "UPDATE suggestions SET status = ? "
            "WHERE uid = ? AND status = 'pending'",
            (status, uid),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()
