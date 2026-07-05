from tiro.authors import ensure_author, link_article_author, merge_authors
from tiro.database import get_connection, init_db


def _conn(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    return get_connection(db)


def test_ensure_author_dedupes_by_canonical_key(tmp_path):
    conn = _conn(tmp_path)
    a1 = ensure_author(conn, "Matt Levine")
    a2 = ensure_author(conn, "  matt LEVINE ")
    assert a1 == a2
    row = conn.execute("SELECT name, uid FROM authors").fetchone()
    assert row["name"] == "Matt Levine" and len(row["uid"]) == 26
    assert ensure_author(conn, "   ") is None
    assert ensure_author(conn, "") is None


def test_merge_authors_repoints_and_ors_vip(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'web')")
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
        " VALUES (?, 1, 't', 's', 'f.md')", ("01A".ljust(26, "0"),))
    keep = ensure_author(conn, "Ben Thompson")
    lose = ensure_author(conn, "B. Thompson")
    conn.execute("UPDATE authors SET is_vip = 1 WHERE id = ?", (lose,))
    link_article_author(conn, 1, "B. Thompson")
    merge_authors(conn, keep, lose)
    rows = conn.execute("SELECT id, is_vip FROM authors").fetchall()
    assert len(rows) == 1 and rows[0]["id"] == keep and rows[0]["is_vip"]
    link = conn.execute("SELECT author_id FROM article_authors").fetchone()
    assert link["author_id"] == keep


def test_ingest_links_author(initialized_library, fake_llm):
    from tiro.ingestion.processor import process_article

    fake_llm('{"tags": [], "entities": [], "summary": ""}')
    result = process_article(
        title="T", author="Jane Doe", content_md="body text here",
        url="https://x.com/a", config=initialized_library,
    )
    conn = get_connection(initialized_library.db_path)
    row = conn.execute(
        "SELECT au.name FROM authors au JOIN article_authors aa ON au.id = aa.author_id"
        " WHERE aa.article_id = ?", (result["id"],),
    ).fetchone()
    conn.close()
    assert row["name"] == "Jane Doe"


def test_delete_article_cleans_article_authors(initialized_library, fake_llm):
    from tiro.ingestion.processor import process_article
    from tiro.lifecycle import delete_article

    fake_llm('{"tags": [], "entities": [], "summary": ""}')
    result = process_article(
        title="T2", author="Jane Doe", content_md="body text here",
        url="https://x.com/b", config=initialized_library,
    )
    conn = get_connection(initialized_library.db_path)
    try:
        assert conn.execute(
            "SELECT 1 FROM article_authors WHERE article_id = ?", (result["id"],)
        ).fetchone() is not None

        assert delete_article(initialized_library, result["id"]) is True

        assert conn.execute(
            "SELECT 1 FROM article_authors WHERE article_id = ?", (result["id"],)
        ).fetchone() is None
        # Author row itself is untouched (shared/reusable, not article-scoped)
        assert conn.execute(
            "SELECT 1 FROM authors WHERE name = 'Jane Doe'"
        ).fetchone() is not None
    finally:
        conn.close()
