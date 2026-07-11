"""Daily digest generation — compat wrapper over the DigestWriter agent.

The gather SQL now lives on the agent context
(tiro/agents/context.py: gather_digest_articles / get_highlights); the
orchestration lives in tiro/agents/builtin/digest_writer.py. This module
keeps the historical `generate_digest` signature + exception surface for
routes_digest.py / email_digest.py / app.py's scheduler, plus the cache
readers/writer and split logic those (and the agent) still depend on.
"""

import json
import logging
import re
from datetime import UTC, datetime, timedelta

from tiro.config import TiroConfig
from tiro.database import get_connection

logger = logging.getLogger(__name__)

RATING_LABELS = {-1: "Dislike", 1: "Like", 2: "Love"}
DIGEST_TYPES = ("ranked", "by_topic", "by_entity")

# Highlights-this-week recap (Phase 2 M2.3, Task 4): a short additional
# section appended to the "ranked" digest variant only -- see
# `generate_digest`'s docstring for why not all three.
HIGHLIGHT_RECAP_WINDOW_DAYS = 7
MAX_HIGHLIGHTS_FOR_RECAP = 50  # cap by recency, same rationale as MAX_ARTICLES_FOR_DIGEST

# Section header patterns to split Opus's response into three digest types
SECTION_PATTERNS = [
    (r"###?\s*1[\.\):]?\s*Ranked\s+by\s+Importance", "ranked"),
    (r"###?\s*2[\.\):]?\s*Grouped\s+by\s+Topic", "by_topic"),
    (r"###?\s*3[\.\):]?\s*Grouped\s+by\s+Entity", "by_entity"),
]


MAX_ARTICLES_FOR_DIGEST = 50  # cap to avoid enormous prompts


def _gather_articles(
    config: TiroConfig, unread_only: bool = False
) -> tuple[list[dict], list[str], list[str], list[dict]]:
    """Gather recent articles, VIP source names, VIP author names, and recent
    ratings from the database.

    An article counts as VIP if its source is VIP OR any linked author is VIP.

    Returns (articles, vip_sources, vip_authors, recent_ratings).

    Kept here (a byte-identical duplicate of RunContext.gather_digest_
    articles' SQL, minus the `uid` column/citation capture) as a back-compat
    direct-import seam — tests/test_snooze_api.py imports this name
    directly and predates the agent runtime; generate_digest itself no
    longer calls it (the agent's gather goes through the context tool
    instead).
    """
    conn = get_connection(config.db_path)
    try:
        # Get recent articles with source info (capped to avoid huge prompts)
        where_clause = "WHERE a.is_read = 0" if unread_only else ""
        rows = conn.execute(f"""
            SELECT
                a.id, a.title, a.summary, a.published_at, a.ingested_at,
                a.is_read, a.rating, a.relevance_weight,
                s.name AS source_name, s.is_vip
            FROM articles a
            LEFT JOIN sources s ON a.source_id = s.id
            {where_clause}
            ORDER BY a.ingested_at DESC
            LIMIT ?
        """, (MAX_ARTICLES_FOR_DIGEST,)).fetchall()

        if not rows:
            return [], [], [], []

        article_ids = [row["id"] for row in rows]

        # Batch-fetch all tags for these articles
        placeholders = ",".join("?" * len(article_ids))
        tag_rows = conn.execute(f"""
            SELECT at.article_id, t.name
            FROM article_tags at JOIN tags t ON t.id = at.tag_id
            WHERE at.article_id IN ({placeholders})
        """, article_ids).fetchall()
        tags_by_article: dict[int, list[str]] = {}
        for row in tag_rows:
            tags_by_article.setdefault(row["article_id"], []).append(row["name"])

        # Batch-fetch all entities for these articles
        entity_rows = conn.execute(f"""
            SELECT ae.article_id, e.name
            FROM article_entities ae JOIN entities e ON e.id = ae.entity_id
            WHERE ae.article_id IN ({placeholders})
        """, article_ids).fetchall()
        entities_by_article: dict[int, list[str]] = {}
        for row in entity_rows:
            entities_by_article.setdefault(row["article_id"], []).append(row["name"])

        # Batch-fetch which of these articles have a VIP author linked.
        author_vip_rows = conn.execute(f"""
            SELECT DISTINCT aa.article_id
            FROM article_authors aa JOIN authors au ON au.id = aa.author_id
            WHERE aa.article_id IN ({placeholders}) AND au.is_vip = 1
        """, article_ids).fetchall()
        vip_author_article_ids = {row["article_id"] for row in author_vip_rows}

        articles = []
        recent_ratings = []

        for row in rows:
            aid = row["id"]
            is_vip = bool(row["is_vip"]) or aid in vip_author_article_ids

            articles.append({
                "id": aid,
                "title": row["title"],
                "source": row["source_name"] or "Unknown",
                "is_vip": is_vip,
                "tags": tags_by_article.get(aid, []),
                "entities": entities_by_article.get(aid, []),
                "summary": row["summary"] or "",
                "published_date": row["published_at"] or row["ingested_at"],
                "relevance_weight": row["relevance_weight"] or 1.0,
            })

            # Collect rated articles for context
            if row["rating"] is not None:
                recent_ratings.append({
                    "title": row["title"],
                    "source": row["source_name"] or "Unknown",
                    "rating_label": RATING_LABELS.get(row["rating"], "Unknown"),
                    "summary": row["summary"] or "",
                })

        # Get VIP sources
        vip_rows = conn.execute(
            "SELECT name FROM sources WHERE is_vip = 1"
        ).fetchall()
        vip_sources = [r["name"] for r in vip_rows]

        # Get VIP authors
        vip_author_rows = conn.execute(
            "SELECT name FROM authors WHERE is_vip = 1"
        ).fetchall()
        vip_authors = [r["name"] for r in vip_author_rows]

        return articles, vip_sources, vip_authors, recent_ratings
    finally:
        conn.close()


def _gather_highlights(config: TiroConfig) -> list[dict]:
    """Gather highlights from the last `HIGHLIGHT_RECAP_WINDOW_DAYS` days for
    the digest's "Highlights this week" recap section.

    Reads the derived `highlights`/`notes` SQLite index only (same posture
    as the rest of digest.py, which never touches sidecar files directly --
    `reconcile_annotations()` keeps the index honest). Scope is deliberately
    HIGHLIGHTS, not "everything annotation-shaped": a highlight's own
    anchored note (`notes.highlight_id = highlights.id`) is included when
    present, since it's the user's own gloss on that exact quote, but
    whole-article notes (`notes.highlight_id IS NULL`) are out of scope --
    an article can have a standalone note with zero highlights, and pulling
    those in would blur "highlights this week" into "everything I annotated
    this week."

    Returns a list of dicts (article_id, article_title, quote, note),
    newest-highlight-first, capped to `MAX_HIGHLIGHTS_FOR_RECAP`. Windowing
    and the cap both key on `highlights.created_at` (ISO 8601 UTC strings,
    so lexicographic and chronological order agree).

    Kept here (a byte-identical duplicate of RunContext.get_highlights'
    SQL, minus the extra highlight_uid/color/article_uid columns) as a
    back-compat direct-import seam — tests/test_highlight_recap.py imports
    this name directly and predates the agent runtime; the DigestWriter
    agent's own gather goes through ctx.get_highlights(days=7, limit=50)
    instead.
    """
    conn = get_connection(config.db_path)
    try:
        cutoff = (datetime.now(UTC) - timedelta(days=HIGHLIGHT_RECAP_WINDOW_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rows = conn.execute(
            """
            SELECT h.article_id, h.quote_text, a.title AS article_title,
                   n.body_markdown AS note_markdown
            FROM highlights h
            JOIN articles a ON a.id = h.article_id
            LEFT JOIN notes n ON n.highlight_id = h.id
            WHERE h.created_at >= ?
            ORDER BY h.created_at DESC
            LIMIT ?
            """,
            (cutoff, MAX_HIGHLIGHTS_FOR_RECAP),
        ).fetchall()
        return [
            {
                "article_id": row["article_id"],
                "article_title": row["article_title"],
                "quote": row["quote_text"],
                "note": row["note_markdown"],
            }
            for row in rows
        ]
    finally:
        conn.close()


def _split_digest(content: str) -> dict[str, str]:
    """Split Opus's combined response into three digest sections.

    Returns dict mapping digest_type -> markdown content.
    """
    # Find positions of each section header
    positions = []
    for pattern, dtype in SECTION_PATTERNS:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            positions.append((match.start(), match.end(), dtype))

    # Sort by position
    positions.sort(key=lambda x: x[0])

    result = {}
    for i, (_start, header_end, dtype) in enumerate(positions):
        # Content runs from after the header to the start of the next section (or end)
        if i + 1 < len(positions):
            section_content = content[header_end : positions[i + 1][0]]
        else:
            section_content = content[header_end:]

        result[dtype] = section_content.strip()

    # Fallback: if splitting failed, put everything in ranked
    if not result:
        logger.warning("Could not split digest into sections, using full content as ranked")
        result["ranked"] = content.strip()

    return result


def _cache_digest(
    config: TiroConfig,
    today: str,
    sections: dict[str, str],
    article_ids: list[int],
) -> None:
    """Cache digest sections in the SQLite digests table."""
    conn = get_connection(config.db_path)
    try:
        ids_json = json.dumps(article_ids)
        for dtype, content in sections.items():
            conn.execute(
                """INSERT OR REPLACE INTO digests (date, digest_type, content, article_ids)
                   VALUES (?, ?, ?, ?)""",
                (today, dtype, content, ids_json),
            )
        conn.commit()
        logger.info("Cached %d digest sections for %s", len(sections), today)
    finally:
        conn.close()


def get_cached_digest(config: TiroConfig, today: str, digest_type: str | None = None) -> dict | None:
    """Retrieve cached digest from SQLite.

    Looks for today's digest first, then falls back to the most recent cached
    digest (so a digest generated last night isn't lost at midnight).

    Returns dict mapping digest_type -> content, or None if not cached.
    If digest_type is specified, returns only that type.
    """
    conn = get_connection(config.db_path)
    try:
        if digest_type:
            # Try today first, then most recent
            row = conn.execute(
                """SELECT content, article_ids, created_at FROM digests
                   WHERE digest_type = ?
                   ORDER BY CASE WHEN date = ? THEN 0 ELSE 1 END, date DESC
                   LIMIT 1""",
                (digest_type, today),
            ).fetchone()
            if row:
                return {
                    digest_type: {
                        "content": row["content"],
                        "article_ids": json.loads(row["article_ids"]),
                        "created_at": row["created_at"],
                    }
                }
            return None
        else:
            # Try today first
            rows = conn.execute(
                "SELECT digest_type, content, article_ids, created_at FROM digests WHERE date = ?",
                (today,),
            ).fetchall()
            # Fall back to most recent date
            if not rows:
                rows = conn.execute(
                    """SELECT digest_type, content, article_ids, created_at FROM digests
                       WHERE date = (SELECT MAX(date) FROM digests)""",
                ).fetchall()
            if not rows:
                return None
            return {
                row["digest_type"]: {
                    "content": row["content"],
                    "article_ids": json.loads(row["article_ids"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            }
    finally:
        conn.close()


def generate_digest(config: TiroConfig, unread_only: bool = False) -> dict:
    """Generate today's digest via the digest_writer agent run.

    Return shape unchanged: {digest_type: {content, article_ids, created_at}}
    with created_at as a full datetime string (the staleness banner parses
    it). ValueError/RuntimeError surface preserved via cause re-raise.
    """
    from tiro.agents.base import AgentRunError
    from tiro.agents.runtime import run_agent

    try:
        res = run_agent(config, "digest_writer", {"unread_only": unread_only})
    except AgentRunError as e:
        if e.__cause__ is not None:
            # Deliberate bare re-raise of the ORIGINAL exception: `from ...`
            # would rewrite its own cause chain; historical callers expect
            # the plain ValueError/RuntimeError surface.
            raise e.__cause__  # noqa: B904
        raise
    out = res.outputs
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    return {
        dtype: {"content": content, "article_ids": out.article_ids,
                "created_at": now}
        for dtype, content in out.sections.items()
    }


def get_digest_dates(config: TiroConfig) -> list[dict]:
    """Get list of dates that have cached digests (most recent first, max 30)."""
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute("""
            SELECT date, GROUP_CONCAT(digest_type) AS types, MAX(created_at) AS created_at
            FROM digests
            GROUP BY date
            ORDER BY date DESC
            LIMIT 30
        """).fetchall()
        return [
            {"date": row["date"], "types": row["types"].split(","), "created_at": row["created_at"]}
            for row in rows
        ]
    finally:
        conn.close()


def get_digest_by_date(config: TiroConfig, target_date: str) -> dict | None:
    """Get cached digest for a specific date (exact match, no fallback).

    Returns dict mapping digest_type -> {content, article_ids, created_at}, or None.
    """
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute(
            "SELECT digest_type, content, article_ids, created_at FROM digests WHERE date = ?",
            (target_date,),
        ).fetchall()
        if not rows:
            return None
        return {
            row["digest_type"]: {
                "content": row["content"],
                "article_ids": json.loads(row["article_ids"]),
                "created_at": row["created_at"],
            }
            for row in rows
        }
    finally:
        conn.close()
