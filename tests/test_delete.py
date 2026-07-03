"""Article deletion coordinator + atomic ingestion rollback."""

from tiro.database import get_connection, init_db, migrate_db


def test_articles_have_vector_status_column(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    conn = get_connection(db)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()]
    finally:
        conn.close()
    assert "vector_status" in cols


def test_migrate_adds_vector_status_to_old_db(tmp_path):
    db = tmp_path / "old.db"
    conn = get_connection(db)
    try:
        # Minimal pre-migration articles table WITHOUT vector_status
        conn.execute(
            "CREATE TABLE articles (id INTEGER PRIMARY KEY, slug TEXT, markdown_path TEXT)"
        )
        conn.commit()
    finally:
        conn.close()
    migrate_db(db)
    conn = get_connection(db)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()]
    finally:
        conn.close()
    assert "vector_status" in cols
