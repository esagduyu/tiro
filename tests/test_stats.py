"""M5: reading-stats increments are state-transition-gated."""

from datetime import date
from pathlib import Path

import pytest

from tiro.database import get_connection


def _ingest_one(client):
    eml = (Path(__file__).parent / "fixtures" / "newsletter.eml").read_bytes()
    r = client.post("/api/ingest/email",
                    files={"file": ("newsletter.eml", eml, "message/rfc822")})
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"]


def _today_stats(config):
    conn = get_connection(config.db_path)
    try:
        return conn.execute(
            "SELECT * FROM reading_stats WHERE date = ?",
            (date.today().isoformat(),),
        ).fetchone()
    finally:
        conn.close()


def test_mark_read_counts_once_but_opened_count_every_time(authenticated_client, configured_library):
    aid = _ingest_one(authenticated_client)
    assert authenticated_client.patch(f"/api/articles/{aid}/read").status_code == 200
    assert authenticated_client.patch(f"/api/articles/{aid}/read").status_code == 200
    stats = _today_stats(configured_library)
    assert stats["articles_read"] == 1

    conn = get_connection(configured_library.db_path)
    try:
        row = conn.execute(
            "SELECT opened_count, is_read FROM articles WHERE id = ?", (aid,)
        ).fetchone()
    finally:
        conn.close()
    assert row["opened_count"] == 2
    assert row["is_read"] == 1


def test_reading_time_counted_once(authenticated_client, configured_library):
    aid = _ingest_one(authenticated_client)
    authenticated_client.patch(f"/api/articles/{aid}/read")
    first = _today_stats(configured_library)["total_reading_time_min"]
    authenticated_client.patch(f"/api/articles/{aid}/read")
    assert _today_stats(configured_library)["total_reading_time_min"] == first


def test_rate_counts_only_first_rating(authenticated_client, configured_library):
    aid = _ingest_one(authenticated_client)
    assert authenticated_client.patch(f"/api/articles/{aid}/rate", json={"rating": 1}).status_code == 200
    assert authenticated_client.patch(f"/api/articles/{aid}/rate", json={"rating": 2}).status_code == 200
    stats = _today_stats(configured_library)
    assert stats["articles_rated"] == 1

    conn = get_connection(configured_library.db_path)
    try:
        row = conn.execute("SELECT rating FROM articles WHERE id = ?", (aid,)).fetchone()
    finally:
        conn.close()
    assert row["rating"] == 2  # re-rating still updates the value


def test_update_stat_rejects_unknown_field(initialized_library):
    from tiro.stats import update_stat

    with pytest.raises(ValueError):
        update_stat(initialized_library, "articles_read; DROP TABLE articles")
