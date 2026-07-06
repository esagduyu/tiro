"""Shared article list/filter SQL. THE single owner of this query shape —
routes_articles, search hydration, and the MCP server all build from here.
Do not fork copies back into route files."""

ARTICLE_COLUMNS = """
    a.id, a.uid, a.title, a.author, a.url, a.slug, a.summary,
    a.word_count, a.reading_time_min, a.published_at, a.ingested_at,
    a.is_read, a.rating, a.opened_count, a.ai_tier,
    a.relevance_weight, a.ingestion_method, a.snoozed_until,
    s.name AS source_name, s.domain, s.is_vip, s.id AS source_id,
    s.source_type
"""

ARTICLE_FROM = "FROM articles a LEFT JOIN sources s ON a.source_id = s.id"

SORT_SQL = {
    "unread": "a.is_read ASC, s.is_vip DESC, a.display_date DESC",
    "newest": "a.display_date DESC, s.is_vip DESC",
    "oldest": "a.display_date ASC, s.is_vip DESC",
    "importance": (
        "CASE a.ai_tier WHEN 'must-read' THEN 0 WHEN 'summary-enough' THEN 1 "
        "WHEN 'discard' THEN 2 ELSE 3 END ASC, s.is_vip DESC, a.display_date DESC"
    ),
}


def build_article_filters(
    *,
    include_decayed: bool = True,
    decay_threshold: float | None = None,
    include_snoozed: bool = True,
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
) -> tuple[str, list]:
    """Build a ` WHERE ...` clause (or "") plus its params list from the
    documented article-list filter facets. Caller supplies decay_threshold as
    a value (this module must not import config).

    `include_snoozed` defaults to True (permissive) deliberately — snoozed
    articles are inbox-hidden, not gone, so every consumer of this builder
    (digest gather doesn't use it; MCP search_articles' `_build_filter_sql`
    does, via this function) sees them unless it explicitly opts out. Only
    the inbox route (GET /api/articles in routes_articles.py) passes
    `include_snoozed=False` by default, scoping the exclusion to that one
    call site."""
    where_clauses: list[str] = []
    params: list = []

    if not include_decayed:
        where_clauses.append("a.relevance_weight >= ?")
        params.append(decay_threshold)

    if not include_snoozed:
        # SQLite's datetime('now') returns UTC 'YYYY-MM-DD HH:MM:SS', the
        # same naive-UTC string convention snoozed_until is stored in
        # (tiro/snooze.py) — lexically comparable, no Python-side "now".
        # A NULL snoozed_until (never snoozed) always passes; a past
        # snoozed_until (expired) also passes, so it auto-reappears once
        # its time comes without any sweep/cron needed.
        where_clauses.append("(a.snoozed_until IS NULL OR a.snoozed_until <= datetime('now'))")

    if is_read is not None:
        where_clauses.append("a.is_read = ?")
        params.append(1 if is_read else 0)

    if is_vip is not None:
        where_clauses.append("s.is_vip = ?")
        params.append(1 if is_vip else 0)

    if ai_tier:
        tiers = [t.strip() for t in ai_tier.split(",") if t.strip()]
        if tiers:
            placeholders = ",".join("?" * len(tiers))
            # Handle 'unclassified' as NULL
            if "unclassified" in tiers:
                tiers_filtered = [t for t in tiers if t != "unclassified"]
                if tiers_filtered:
                    where_clauses.append(f"(a.ai_tier IN ({','.join('?' * len(tiers_filtered))}) OR a.ai_tier IS NULL)")
                    params.extend(tiers_filtered)
                else:
                    where_clauses.append("a.ai_tier IS NULL")
            else:
                where_clauses.append(f"a.ai_tier IN ({placeholders})")
                params.extend(tiers)

    if author:
        where_clauses.append("a.author = ?")
        params.append(author)

    if source_id is not None:
        where_clauses.append("a.source_id = ?")
        params.append(source_id)

    if tag:
        where_clauses.append("""a.id IN (
            SELECT at2.article_id FROM article_tags at2
            JOIN tags t2 ON t2.id = at2.tag_id
            WHERE t2.name = ?
        )""")
        params.append(tag)

    if rating:
        ratings = [r.strip() for r in rating.split(",") if r.strip()]
        if ratings:
            # Map named ratings to values
            rating_map = {"loved": "2", "liked": "1", "disliked": "-1", "unrated": "NULL"}
            rating_vals = []
            has_unrated = False
            for r in ratings:
                mapped = rating_map.get(r, r)
                if mapped == "NULL":
                    has_unrated = True
                else:
                    rating_vals.append(mapped)

            parts = []
            if rating_vals:
                parts.append(f"a.rating IN ({','.join('?' * len(rating_vals))})")
                params.extend(int(v) for v in rating_vals)
            if has_unrated:
                parts.append("a.rating IS NULL")
            if parts:
                where_clauses.append(f"({' OR '.join(parts)})")

    if ingestion_method:
        methods = [m.strip() for m in ingestion_method.split(",") if m.strip()]
        if methods:
            placeholders = ",".join("?" * len(methods))
            where_clauses.append(f"COALESCE(a.ingestion_method, 'manual') IN ({placeholders})")
            params.extend(methods)

    if min_reading_time is not None:
        where_clauses.append("a.reading_time_min >= ?")
        params.append(min_reading_time)

    if max_reading_time is not None:
        where_clauses.append("a.reading_time_min <= ?")
        params.append(max_reading_time)

    if has_audio is not None:
        if has_audio:
            where_clauses.append("a.id IN (SELECT article_id FROM audio)")
        else:
            where_clauses.append("a.id NOT IN (SELECT article_id FROM audio)")

    if date_from:
        where_clauses.append("a.display_date >= ?")
        params.append(date_from)

    if date_to:
        where_clauses.append("a.display_date <= ?")
        params.append(date_to + " 23:59:59")

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    return where_sql, params
