from tiro.authors import ensure_author, link_article_author, merge_authors
from tiro.database import get_connection, init_db


def _conn(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    return get_connection(db)


def _seed_author(config, name, is_vip=False):
    conn = get_connection(config.db_path)
    try:
        author_id = ensure_author(conn, name)
        if is_vip:
            conn.execute("UPDATE authors SET is_vip = 1 WHERE id = ?", (author_id,))
        conn.commit()
        return author_id
    finally:
        conn.close()


def _seed_article_for_author(config, author_id, slug, title="T"):
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            "INSERT INTO sources (name, source_type) VALUES ('s', 'web')"
        )
        conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
            " VALUES (?, last_insert_rowid(), ?, ?, ?)",
            (slug.upper().ljust(26, "0"), title, slug, f"{slug}.md"),
        )
        article_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO article_authors (article_id, author_id) VALUES (?, ?)",
            (article_id, author_id),
        )
        conn.commit()
        return article_id
    finally:
        conn.close()


# --- GET /api/authors --------------------------------------------------------


def test_list_authors_ordered_with_counts(authenticated_client, configured_library):
    vip_id = _seed_author(configured_library, "Zed VIP", is_vip=True)
    plain_a = _seed_author(configured_library, "Ann Plain")
    _seed_author(configured_library, "Bea Plain")
    _seed_article_for_author(configured_library, vip_id, "a1")
    _seed_article_for_author(configured_library, plain_a, "a2")
    _seed_article_for_author(configured_library, plain_a, "a3")

    r = authenticated_client.get("/api/authors")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    data = body["data"]
    names = [row["name"] for row in data]
    # VIP first, then alphabetical among non-VIPs.
    assert names == ["Zed VIP", "Ann Plain", "Bea Plain"]
    by_name = {row["name"]: row for row in data}
    assert bool(by_name["Zed VIP"]["is_vip"]) is True
    assert by_name["Zed VIP"]["article_count"] == 1
    assert by_name["Ann Plain"]["article_count"] == 2
    assert by_name["Bea Plain"]["article_count"] == 0
    assert set(by_name["Ann Plain"].keys()) == {
        "id", "uid", "name", "is_vip", "article_count",
    }


# --- PATCH /api/authors/{id}/vip ---------------------------------------------


def test_toggle_author_vip(authenticated_client, configured_library):
    author_id = _seed_author(configured_library, "Toggle Me")

    r = authenticated_client.patch(f"/api/authors/{author_id}/vip")
    assert r.status_code == 200, r.text
    assert r.json() == {"success": True, "data": {"id": author_id, "is_vip": True}}

    r = authenticated_client.patch(f"/api/authors/{author_id}/vip")
    assert r.json()["data"]["is_vip"] is False


def test_toggle_author_vip_missing_404(authenticated_client, configured_library):
    r = authenticated_client.patch("/api/authors/999999/vip")
    assert r.status_code == 404


# --- POST /api/authors/merge -------------------------------------------------


def test_merge_authors_endpoint(authenticated_client, configured_library):
    keep_id = _seed_author(configured_library, "Keep Author")
    merge_id = _seed_author(configured_library, "Merge Author", is_vip=True)
    _seed_article_for_author(configured_library, merge_id, "m1")

    r = authenticated_client.post(
        "/api/authors/merge", json={"keep_id": keep_id, "merge_id": merge_id}
    )
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True

    conn = get_connection(configured_library.db_path)
    try:
        rows = conn.execute("SELECT id, is_vip FROM authors").fetchall()
        assert len(rows) == 1 and rows[0]["id"] == keep_id and rows[0]["is_vip"]
        link = conn.execute("SELECT author_id FROM article_authors").fetchone()
        assert link["author_id"] == keep_id
    finally:
        conn.close()


def test_merge_authors_same_id_400(authenticated_client, configured_library):
    author_id = _seed_author(configured_library, "Solo")
    r = authenticated_client.post(
        "/api/authors/merge", json={"keep_id": author_id, "merge_id": author_id}
    )
    assert r.status_code == 400


def test_merge_authors_missing_404(authenticated_client, configured_library):
    author_id = _seed_author(configured_library, "Exists")
    r = authenticated_client.post(
        "/api/authors/merge", json={"keep_id": author_id, "merge_id": 999999}
    )
    assert r.status_code == 404

    r = authenticated_client.post(
        "/api/authors/merge", json={"keep_id": 999999, "merge_id": author_id}
    )
    assert r.status_code == 404


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
