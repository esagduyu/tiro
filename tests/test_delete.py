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
        # Realistic pre-vector_status legacy DB: sources + articles.source_id
        # both predate ingestion_method/vector_status in the schema, so a real
        # legacy Tiro DB always has them. Missing: ingestion_method, vector_status.
        conn.execute(
            "CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, source_type TEXT)"
        )
        conn.execute(
            """
            CREATE TABLE articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER,
                title TEXT,
                slug TEXT,
                markdown_path TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO sources (name, source_type) VALUES ('Example Blog', 'web')"
        )
        conn.execute(
            """
            INSERT INTO articles (source_id, title, slug, markdown_path)
            VALUES (1, 'Old Article', 'old-article', 'old-article.md')
            """
        )
        conn.commit()
    finally:
        conn.close()
    migrate_db(db)
    conn = get_connection(db)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()]
        row = conn.execute(
            "SELECT ingestion_method FROM articles WHERE slug = 'old-article'"
        ).fetchone()
    finally:
        conn.close()
    assert "vector_status" in cols
    assert "ingestion_method" in cols
    assert row["ingestion_method"] == "manual"
