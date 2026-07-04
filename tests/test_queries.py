"""The shared filter builder must express every facet the API and MCP use."""

from tiro.queries import SORT_SQL, build_article_filters


def test_empty_filters():
    where, params = build_article_filters()
    assert where == "" and params == []


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
