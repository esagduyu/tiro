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


FIXTURE_EML = None  # set in test below


def _ingest_one(client):
    from pathlib import Path

    eml = (Path(__file__).parent / "fixtures" / "newsletter.eml").read_bytes()
    r = client.post("/api/ingest/email",
                    files={"file": ("newsletter.eml", eml, "message/rfc822")})
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"]


def test_delete_removes_all_stores(authenticated_client, configured_library):
    from tiro.lifecycle import delete_article
    from tiro.vectorstore import get_collection

    article_id = _ingest_one(authenticated_client)
    conn = get_connection(configured_library.db_path)
    try:
        md = conn.execute("SELECT markdown_path FROM articles WHERE id = ?",
                          (article_id,)).fetchone()["markdown_path"]
    finally:
        conn.close()
    md_file = configured_library.articles_dir / md
    assert md_file.exists()
    assert get_collection().get(ids=[f"article_{article_id}"])["ids"]

    assert delete_article(configured_library, article_id) is True

    # SQLite: article + junctions gone
    conn = get_connection(configured_library.db_path)
    try:
        assert conn.execute("SELECT 1 FROM articles WHERE id = ?", (article_id,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM article_tags WHERE article_id = ?", (article_id,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM article_entities WHERE article_id = ?", (article_id,)).fetchone() is None
    finally:
        conn.close()
    # File gone, vector gone
    assert not md_file.exists()
    assert not get_collection().get(ids=[f"article_{article_id}"])["ids"]


def test_delete_returns_false_for_missing(configured_library):
    from tiro.lifecycle import delete_article

    assert delete_article(configured_library, 99999) is False


def test_delete_clears_inbound_relations(authenticated_client, configured_library):
    """Deleting an article removes rows where it is the related_article_id too."""
    from tiro.lifecycle import delete_article

    a = _ingest_one(authenticated_client)
    conn = get_connection(configured_library.db_path)
    try:
        # Simulate another article relating TO `a`. article_relations.article_id
        # has a FOREIGN KEY REFERENCES articles(id) and get_connection() runs
        # with PRAGMA foreign_keys=ON, so the "other" article must be a real
        # row (a fabricated id like a+1000 raises IntegrityError).
        other = conn.execute(
            "INSERT INTO articles (title, slug, markdown_path) VALUES (?, ?, ?)",
            ("Other Article", f"other-article-{a}", f"other-{a}.md"),
        ).lastrowid
        conn.execute(
            "INSERT INTO article_relations (article_id, related_article_id, similarity_score) VALUES (?, ?, ?)",
            (other, a, 0.9),
        )
        conn.commit()
    finally:
        conn.close()
    delete_article(configured_library, a)
    conn = get_connection(configured_library.db_path)
    try:
        assert conn.execute(
            "SELECT 1 FROM article_relations WHERE related_article_id = ?", (a,)
        ).fetchone() is None
    finally:
        conn.close()


def test_delete_cited_article_no_integrity_error(authenticated_client, configured_library):
    """A wiki page citing the deleted article must not FK-crash the delete,
    must lose its junction row, and must flip stale (both DB row and file) --
    the page file itself must never be removed."""
    import frontmatter

    from tiro.lifecycle import delete_article
    from tiro.wiki import page_path, write_page

    article_id = _ingest_one(authenticated_client)
    conn = get_connection(configured_library.db_path)
    try:
        article_uid = conn.execute(
            "SELECT uid FROM articles WHERE id = ?", (article_id,)
        ).fetchone()["uid"]
    finally:
        conn.close()

    write_page(
        configured_library,
        slug="entities/acme",
        kind="entity",
        title="Acme",
        entity_type="company",
        article_uids=[article_uid],
        body="Acme body citing the article.",
        generated_by=None,
    )

    assert delete_article(configured_library, article_id) is True

    conn = get_connection(configured_library.db_path)
    try:
        assert conn.execute(
            "SELECT 1 FROM wiki_page_articles WHERE article_id = ?", (article_id,)
        ).fetchone() is None
        page = conn.execute(
            "SELECT status FROM wiki_pages WHERE slug = 'entities/acme'"
        ).fetchone()
        assert page is not None
        assert page["status"] == "stale"
    finally:
        conn.close()

    path = page_path(configured_library, "entities/acme")
    assert path.exists()
    assert frontmatter.load(str(path)).metadata["status"] == "stale"


def test_delete_source_with_cited_article(authenticated_client, configured_library):
    """Source delete loops delete_article per article -- must not 500 when
    one of the source's articles is cited by a wiki page."""
    from tiro.wiki import write_page

    article_id = _ingest_one(authenticated_client)
    conn = get_connection(configured_library.db_path)
    try:
        row = conn.execute(
            "SELECT uid, source_id FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        article_uid, source_id = row["uid"], row["source_id"]
    finally:
        conn.close()

    write_page(
        configured_library,
        slug="entities/acme2",
        kind="entity",
        title="Acme2",
        entity_type="company",
        article_uids=[article_uid],
        body="Acme2 body citing the article.",
        generated_by=None,
    )

    r = authenticated_client.delete(f"/api/sources/{source_id}")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["deleted_articles"] == 1


def test_delete_endpoint(authenticated_client, configured_library):
    from tiro.vectorstore import get_collection

    article_id = _ingest_one(authenticated_client)
    r = authenticated_client.delete(f"/api/articles/{article_id}")
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] == article_id
    assert authenticated_client.get(f"/api/articles/{article_id}").status_code == 404
    assert not get_collection().get(ids=[f"article_{article_id}"])["ids"]


def test_delete_endpoint_404(authenticated_client):
    assert authenticated_client.delete("/api/articles/99999").status_code == 404


def test_delete_endpoint_requires_auth(auth_client):
    assert auth_client.delete("/api/articles/1").status_code == 401
