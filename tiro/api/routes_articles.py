"""Article API routes."""

import asyncio
import logging
from pathlib import Path

import frontmatter
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tiro.database import get_connection
from tiro.intelligence.analysis import analyze_article, get_cached_analysis
from tiro.queries import ARTICLE_COLUMNS, ARTICLE_FROM, SORT_SQL, build_article_filters
from tiro.snooze import PRESETS as SNOOZE_PRESETS
from tiro.snooze import compute_preset, validate_until
from tiro.stats import update_stat

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/articles", tags=["articles"])


@router.get("/{article_id}")
async def get_article(article_id: int, request: Request):
    """Get a single article with full markdown content."""
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        row = conn.execute("""
            SELECT
                a.id, a.title, a.author, a.url, a.slug, a.summary,
                a.word_count, a.reading_time_min, a.published_at, a.ingested_at,
                a.is_read, a.rating, a.opened_count, a.markdown_path, a.ai_tier,
                a.relevance_weight, a.snoozed_until,
                s.name AS source_name, s.domain, s.is_vip, s.id AS source_id,
                s.source_type
            FROM articles a
            LEFT JOIN sources s ON a.source_id = s.id
            WHERE a.id = ?
        """, (article_id,)).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Article not found")

        article = dict(row)

        # Fetch tags
        tags = conn.execute("""
            SELECT t.name FROM tags t
            JOIN article_tags at ON t.id = at.tag_id
            WHERE at.article_id = ?
        """, (article_id,)).fetchall()
        article["tags"] = [t["name"] for t in tags]

        # Read markdown content from file
        md_path = Path(article["markdown_path"])
        if not md_path.is_absolute():
            md_path = config.articles_dir / md_path
        if md_path.exists():
            post = frontmatter.load(str(md_path))
            article["content"] = post.content
        else:
            article["content"] = ""
            logger.warning("Markdown file not found: %s", md_path)

        return {"success": True, "data": article}
    finally:
        conn.close()


@router.get("")
async def list_articles(
    request: Request,
    page: int = 1,
    per_page: int = 0,
    sort: str = "unread",
    is_read: bool | None = None,
    is_vip: bool | None = None,
    ai_tier: str | None = None,
    author: str | None = None,
    source_id: int | None = None,
    tag: str | None = None,
    rating: str | None = None,
    ingestion_method: str | None = None,
    min_reading_time: int | None = None,
    max_reading_time: int | None = None,
    has_audio: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    include_decayed: bool = True,
    include_snoozed: bool = False,
    count_only: bool = False,
):
    """List articles with filtering, sorting, and pagination.

    per_page=0 (default) returns all results (backwards compatible).
    count_only=true returns just the count matching filters.

    include_snoozed=False by default: this is the one inbox-scoped call
    site that hides snoozed articles (tiro/queries.py's `include_snoozed`
    builder param defaults to True/permissive everywhere else — digest,
    decay, classification, MCP search, export, and stats all still see
    snoozed articles, since snoozing hides from the inbox, not the
    library). Pass include_snoozed=true to reveal them here too (mirrors
    include_decayed's "Show archived" pattern).
    """
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        where_sql, params = build_article_filters(
            include_decayed=include_decayed,
            decay_threshold=config.decay_threshold,
            include_snoozed=include_snoozed,
            is_read=is_read,
            is_vip=is_vip,
            ai_tier=ai_tier,
            author=author,
            source_id=source_id,
            tag=tag,
            rating=rating,
            ingestion_method=ingestion_method,
            min_reading_time=min_reading_time,
            max_reading_time=max_reading_time,
            has_audio=has_audio,
            date_from=date_from,
            date_to=date_to,
        )

        # Count only mode (for unread badge etc.)
        if count_only:
            count = conn.execute(
                f"SELECT COUNT(*) {ARTICLE_FROM}{where_sql}",
                params,
            ).fetchone()[0]
            return {"success": True, "data": {"count": count}}

        # Sort — VIP is always second-order priority within each sort mode
        sort_sql = SORT_SQL.get(sort, SORT_SQL["unread"])

        # Total count for pagination
        total = conn.execute(
            f"SELECT COUNT(*) {ARTICLE_FROM}{where_sql}",
            params,
        ).fetchone()[0]

        # Pagination
        limit_sql = ""
        if per_page > 0:
            offset = (max(1, page) - 1) * per_page
            limit_sql = f" LIMIT {per_page} OFFSET {offset}"

        query = f"""
            SELECT
                {ARTICLE_COLUMNS}
            {ARTICLE_FROM}
            {where_sql}
            ORDER BY {sort_sql}
            {limit_sql}
        """

        rows = conn.execute(query, params).fetchall()

        # Batch-fetch tags for all articles
        article_ids = [row["id"] for row in rows]
        tags_map: dict[int, list[str]] = {aid: [] for aid in article_ids}
        if article_ids:
            placeholders = ",".join("?" * len(article_ids))
            tag_rows = conn.execute(f"""
                SELECT at.article_id, t.name FROM tags t
                JOIN article_tags at ON t.id = at.tag_id
                WHERE at.article_id IN ({placeholders})
            """, article_ids).fetchall()
            for tr in tag_rows:
                tags_map[tr["article_id"]].append(tr["name"])

        articles = []
        for row in rows:
            article = dict(row)
            article["tags"] = tags_map.get(article["id"], [])
            articles.append(article)

        response: dict = {"success": True, "data": articles}
        if per_page > 0:
            import math
            response["pagination"] = {
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": math.ceil(total / per_page) if per_page else 1,
            }
        return response
    finally:
        conn.close()


class RateRequest(BaseModel):
    rating: int


@router.patch("/{article_id}/rate")
async def rate_article(article_id: int, body: RateRequest, request: Request):
    """Set article rating: -1 (dislike), 1 (like), 2 (love)."""
    if body.rating not in (-1, 1, 2):
        raise HTTPException(status_code=400, detail="Rating must be -1, 1, or 2")

    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT rating FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Article not found")
        first_rating = row["rating"] is None
        conn.execute(
            "UPDATE articles SET rating = ? WHERE id = ?",
            (body.rating, article_id),
        )
        conn.commit()

        if first_rating:
            try:
                update_stat(config, "articles_rated")
            except Exception as e:
                logger.error("Failed to update reading stats: %s", e)

        return {"success": True, "data": {"id": article_id, "rating": body.rating}}
    finally:
        conn.close()


@router.patch("/{article_id}/read")
async def mark_read(article_id: int, request: Request):
    """Mark article as read and increment open count."""
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT is_read, reading_time_min FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Article not found")
        was_read = bool(row["is_read"])
        conn.execute(
            "UPDATE articles SET is_read = 1, opened_count = opened_count + 1 WHERE id = ?",
            (article_id,),
        )
        conn.commit()

        # Update reading stats
        if not was_read:
            try:
                update_stat(config, "articles_read")
                if row["reading_time_min"]:
                    update_stat(config, "total_reading_time_min", row["reading_time_min"])
            except Exception as e:
                logger.error("Failed to update reading stats: %s", e)

        row = conn.execute(
            "SELECT is_read, opened_count FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
        return {
            "success": True,
            "data": {
                "id": article_id,
                "is_read": row["is_read"],
                "opened_count": row["opened_count"],
            },
        }
    finally:
        conn.close()


class SnoozeRequest(BaseModel):
    until: str | None = None
    preset: str | None = None


@router.patch("/{article_id}/snooze")
async def snooze_article(article_id: int, body: SnoozeRequest, request: Request):
    """Snooze an article out of the default inbox view until a future time
    (the backbone of M3.2's swipe-triage) — snoozing never deletes or
    archives anything, and NOTHING but GET /api/articles' default listing
    hides a snoozed article (see tiro/queries.py's `include_snoozed`
    builder param): digest generation, classification, decay recalculation,
    MCP search, export, and stats all still see it.

    Body is exactly one of:
      - {"until": "<ISO timestamp>"} — explicit target; must be strictly in
        the future. Malformed or past → 400.
      - {"preset": "tonight"|"tomorrow"|"weekend"|"next_week"} — server
        computes the target from *server-local* wall-clock time (single-
        user local app, deliberately not UTC/client time — see
        tiro/snooze.py's compute_preset() docstring for the exact table):
            tonight    -> today 19:00, or now+6h if 19:00 already passed
            tomorrow   -> tomorrow 09:00
            weekend    -> next Saturday 09:00
            next_week  -> next Monday 09:00
      - {"until": null} (preset omitted, or body omitted entirely) —
        unsnooze: clears snoozed_until.

    Providing both `until` and `preset` is a 400. Unknown article -> 404.
    Response includes the computed/stored `snoozed_until` (null if
    unsnoozed).
    """
    if body.preset is not None and body.until is not None:
        raise HTTPException(
            status_code=400, detail="Provide either 'until' or 'preset', not both"
        )

    if body.preset is not None:
        if body.preset not in SNOOZE_PRESETS:
            raise HTTPException(status_code=400, detail=f"Unknown preset: {body.preset!r}")
        snoozed_until = compute_preset(body.preset)
    elif body.until is not None:
        try:
            snoozed_until = validate_until(body.until)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    else:
        snoozed_until = None  # unsnooze

    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT id FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Article not found")

        conn.execute(
            "UPDATE articles SET snoozed_until = ? WHERE id = ?",
            (snoozed_until, article_id),
        )
        conn.commit()

        return {
            "success": True,
            "data": {"id": article_id, "snoozed_until": snoozed_until},
        }
    finally:
        conn.close()


@router.delete("/{article_id}")
async def delete_article_route(article_id: int, request: Request):
    """Permanently delete an article from all stores."""
    from tiro.lifecycle import delete_article

    config = request.app.state.config
    deleted = await asyncio.to_thread(delete_article, config, article_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Article not found")
    return {"success": True, "data": {"deleted": article_id}}


@router.get("/{article_id}/analysis")
async def get_analysis(article_id: int, request: Request):
    """Read cached analysis. Pure read — data is null when nothing is cached;
    running Opus is POST /api/articles/{id}/analysis (M4b)."""
    config = request.app.state.config
    cached = get_cached_analysis(config, article_id)
    return {"success": True, "data": cached}


@router.post("/{article_id}/analysis")
async def run_analysis(article_id: int, request: Request):
    """Run (or re-run) ingenuity/trust analysis with Opus."""
    config = request.app.state.config

    # Blocking Opus call wrapped in thread — can take up to a minute
    try:
        analysis = await asyncio.to_thread(analyze_article, config, article_id)
        return {"success": True, "data": analysis}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        logger.error("Analysis failed for article %d: %s", article_id, e)
        raise HTTPException(status_code=500, detail="Analysis failed") from e
