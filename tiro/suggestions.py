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


# --- Task 6: accept appliers ------------------------------------------------


class SuggestionApplyError(ValueError):
    """Accept-path validation failure. The suggestion stays PENDING."""


VALID_TIERS = ("must-read", "summary-enough", "discard")


def _apply_note(config: TiroConfig, suggestion: dict) -> dict:
    from tiro.annotations import read_note, sidecar_stem, upsert_article_note

    payload = suggestion["payload"]
    article_id = int(payload["article_id"])
    markdown = str(payload.get("markdown", "")).strip()
    if not markdown:
        raise SuggestionApplyError("suggestion has an empty note body")
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT id, markdown_path FROM articles WHERE id = ?",
            (article_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SuggestionApplyError(f"article {article_id} no longer exists")
    attribution = (f'*Suggested by persona "{suggestion["persona"]}":*'
                   f"\n\n{markdown}")
    existing = read_note(config, sidecar_stem(row))
    new_body = (f"{existing}\n\n---\n\n{attribution}" if existing
                else attribution)
    return upsert_article_note(config, article_id, new_body)


def _apply_contradiction(config: TiroConfig, suggestion: dict) -> dict:
    """Accept = append the composed contradiction markdown to the NEW
    article's note — the same validated write path as the note kind
    (K4 decision 6). Dismiss needs no applier."""
    from tiro.annotations import read_note, sidecar_stem, upsert_article_note

    payload = suggestion["payload"]
    article_id = int(payload["article_id"])
    markdown = str(payload.get("markdown", "")).strip()
    if not markdown:
        raise SuggestionApplyError("contradiction suggestion has no body")
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT id, markdown_path FROM articles WHERE id = ?",
            (article_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SuggestionApplyError(f"article {article_id} no longer exists")
    attribution = f"*Flagged by the contradiction detector:*\n\n{markdown}"
    existing = read_note(config, sidecar_stem(row))
    new_body = (f"{existing}\n\n---\n\n{attribution}" if existing
                else attribution)
    return upsert_article_note(config, article_id, new_body)


def _apply_tier(config: TiroConfig, suggestion: dict) -> dict:
    payload = suggestion["payload"]
    tier = payload.get("tier")
    if tier not in VALID_TIERS:
        raise SuggestionApplyError(f"invalid tier {tier!r}")
    conn = get_connection(config.db_path)
    try:
        # The same statement the classifier's writeback uses
        # (tiro/agents/context.py's RunContext.set_tier -- K2 moved the
        # writeback off preferences.py onto the agent runtime).
        cur = conn.execute("UPDATE articles SET ai_tier = ? WHERE id = ?",
                           (tier, int(payload["article_id"])))
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount == 0:
        raise SuggestionApplyError(
            f"article {payload['article_id']} no longer exists")
    return {"article_id": payload["article_id"], "ai_tier": tier}


def _apply_digest_section(config: TiroConfig, suggestion: dict) -> dict:
    from datetime import date

    payload = suggestion["payload"]
    markdown = str(payload.get("markdown", "")).strip()
    if not markdown:
        raise SuggestionApplyError("suggestion has an empty section body")
    title = str(payload.get("title") or "Persona section").strip()
    today = date.today().isoformat()
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT id, content FROM digests WHERE date = ? AND "
            "digest_type = 'ranked'", (today,)).fetchone()
        if row is None:
            raise SuggestionApplyError(
                "no cached digest for today — generate one first")
        conn.execute("UPDATE digests SET content = ? WHERE id = ?",
                     (f"{row['content']}\n\n## {title}\n\n{markdown}",
                      row["id"]))
        conn.commit()
    finally:
        conn.close()
    return {"date": today, "digest_type": "ranked"}


def _apply_wiki_page(config: TiroConfig, suggestion: dict) -> dict:
    from tiro.wiki import read_page, write_page

    payload = suggestion["payload"]
    slug = str(payload.get("slug", ""))
    markdown = str(payload.get("markdown", "")).strip()
    if not markdown:
        raise SuggestionApplyError("suggestion has an empty page body")
    prior = read_page(config, slug)
    if prior is None:
        raise SuggestionApplyError(
            f"wiki page {slug!r} does not exist — personas may only "
            "update existing pages")
    return write_page(
        config, slug=slug, kind=prior["kind"], title=prior["title"],
        entity_type=prior.get("entity_type"),
        article_uids=suggestion.get("citations") or [],
        body=markdown, generated_by=f"persona:{suggestion['persona']}",
        user_pinned_note=prior.get("user_pinned_note") or "",
        uid=prior["uid"])


_APPLIERS = {
    "note": _apply_note,
    "tier_suggestion": _apply_tier,
    "digest_section": _apply_digest_section,
    "wiki_page": _apply_wiki_page,
    "contradiction": _apply_contradiction,
}


def apply_suggestion(config: TiroConfig, suggestion: dict) -> dict:
    """Run the kind's validated write. Raises SuggestionApplyError on any
    validation failure -- callers flip status to accepted ONLY after this
    returns (apply first, resolve second: a failed apply stays pending)."""
    applier = _APPLIERS.get(suggestion["kind"])
    if applier is None:
        raise SuggestionApplyError(
            f"no applier for kind {suggestion['kind']!r}")
    return applier(config, suggestion)
