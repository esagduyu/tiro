"""The shared filter builder must express every facet the API and MCP use."""

from tiro.queries import ARTICLE_COLUMNS, SORT_SQL, build_article_filters


def test_empty_filters():
    where, params = build_article_filters()
    assert where == "" and params == []


def test_article_columns_includes_snoozed_until():
    # M3.2 Task 1: snoozed_until must be surfaced in every ARTICLE_COLUMNS
    # consumer (GET /api/articles list) so the inbox toggle/chip can read it.
    assert "a.snoozed_until" in ARTICLE_COLUMNS


def test_include_snoozed_defaults_permissive():
    # Default (no exclusion) so callers that never mention snooze — the
    # digest gather, decay, classifier, MCP search — see snoozed articles
    # by default; only the inbox route explicitly opts out.
    where, params = build_article_filters()
    assert "snoozed_until" not in where
    assert params == []


def test_include_snoozed_false_excludes_future_snoozed():
    where, params = build_article_filters(include_snoozed=False)
    assert "a.snoozed_until IS NULL OR a.snoozed_until <= datetime('now')" in where
    assert params == []


def test_decay_and_read_filters():
    where, params = build_article_filters(
        include_decayed=False, decay_threshold=0.1, is_read=False
    )
    assert "a.relevance_weight >= ?" in where
    assert "a.is_read = ?" in where
    assert params == [0.1, 0]


def test_tier_unclassified_maps_to_null():
    where, params = build_article_filters(ai_tier="must-read,unclassified")
    assert "a.ai_tier IS NULL" in where
    assert "must-read" in params


def test_rating_names_map():
    where, params = build_article_filters(rating="loved,unrated")
    assert "a.rating IS NULL" in where
    assert 2 in params


def test_sort_modes_use_display_date():
    for mode, sql in SORT_SQL.items():
        assert "COALESCE" not in sql, mode
        assert "display_date" in sql, mode


def test_unclassified_tier_is_null_semantics():
    # Pure "unclassified" (no other tiers mixed in) must compile to a plain
    # IS NULL check, not an IN(...) clause with zero matching values — the
    # MCP search_articles path (tiro/mcp/server.py::_build_filter_sql) relies
    # on this so ai_tier="unclassified" actually returns untagged articles
    # instead of silently matching nothing.
    where, params = build_article_filters(ai_tier="unclassified")
    assert "a.ai_tier IS NULL" in where and "IN (" not in where
    assert params == []


def test_null_ingestion_method_matches_manual():
    # NULL ingestion_method rows (pre-M7 articles, or any row where the
    # column was explicitly cleared) must match ingestion_method="manual" —
    # the MCP search_articles path depends on this COALESCE mapping.
    where, params = build_article_filters(ingestion_method="manual")
    assert "COALESCE(a.ingestion_method, 'manual')" in where
    assert params[-1] == "manual"


def test_mcp_filter_sql_finds_null_tier_and_null_method_article(initialized_library):
    """End-to-end through the actual MCP integration point.

    tiro.mcp.server imports cleanly with no side effects (config/vectorstore
    are only touched lazily inside _get_config(), not at import time), so we
    can drive its real _build_filter_sql() wrapper — the thing
    search_articles() actually calls — against a seeded temp DB, instead of
    only testing build_article_filters() in isolation.
    """
    from tiro.database import get_connection
    from tiro.mcp.server import _build_filter_sql

    conn = get_connection(initialized_library.db_path)
    conn.execute(
        """INSERT INTO articles
               (uid, title, slug, markdown_path, ai_tier, ingestion_method)
           VALUES (?, ?, ?, ?, NULL, NULL)""",
        ("01AAAAAAAAAAAAAAAAAAAAAAAA", "Untagged Article", "untagged-article", "untagged-article.md"),
    )
    conn.commit()

    where, params = _build_filter_sql(ai_tier="unclassified", ingestion_method="manual")
    rows = conn.execute(
        f"SELECT a.id, a.title FROM articles a LEFT JOIN sources s ON a.source_id = s.id WHERE {where}",
        params,
    ).fetchall()
    conn.close()

    assert [dict(r)["title"] for r in rows] == ["Untagged Article"]
