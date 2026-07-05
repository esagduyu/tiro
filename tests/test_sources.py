"""Sources API: delete (via lifecycle coordinator + auto-backup), merge, edit."""

from tiro.database import get_connection


def _seed_source(config, name="Src", source_type="web", is_vip=False):
    conn = get_connection(config.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO sources (name, source_type, is_vip) VALUES (?, ?, ?)",
            (name, source_type, is_vip),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _seed_article(config, source_id, slug, title="T"):
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
            " VALUES (?, ?, ?, ?, ?)",
            (slug.upper().ljust(26, "0"), source_id, title, slug, f"{slug}.md"),
        )
        conn.commit()
    finally:
        conn.close()
    (config.articles_dir / f"{slug}.md").write_text(
        f"---\ntitle: {title}\n---\nbody {slug}"
    )


# --- DELETE /api/sources/{id} ------------------------------------------------


def test_delete_source_removes_articles_files_and_source(
    authenticated_client, configured_library
):
    source_id = _seed_source(configured_library)
    _seed_article(configured_library, source_id, "art-a")
    _seed_article(configured_library, source_id, "art-b")

    r = authenticated_client.delete(f"/api/sources/{source_id}")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["deleted_articles"] == 2

    conn = get_connection(configured_library.db_path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM articles WHERE source_id = ?", (source_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sources WHERE id = ?", (source_id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()

    assert not (configured_library.articles_dir / "art-a.md").exists()
    assert not (configured_library.articles_dir / "art-b.md").exists()


def test_delete_source_writes_auto_backup(authenticated_client, configured_library):
    source_id = _seed_source(configured_library)
    _seed_article(configured_library, source_id, "art-c")

    r = authenticated_client.delete(f"/api/sources/{source_id}")
    assert r.status_code == 200, r.text

    auto_dir = configured_library.library / "backups" / "auto"
    assert auto_dir.is_dir()
    snapshots = list(auto_dir.glob("*source-delete*.tar.zst"))
    assert len(snapshots) == 1


def test_delete_source_with_zero_articles(authenticated_client, configured_library):
    source_id = _seed_source(configured_library, name="Empty")

    r = authenticated_client.delete(f"/api/sources/{source_id}")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["deleted_articles"] == 0

    conn = get_connection(configured_library.db_path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sources WHERE id = ?", (source_id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_delete_source_404_unknown(authenticated_client):
    r = authenticated_client.delete("/api/sources/99999")
    assert r.status_code == 404


# --- POST /api/sources/merge -------------------------------------------------


def test_merge_repoints_articles_and_deletes_from_source(
    authenticated_client, configured_library
):
    from_id = _seed_source(configured_library, name="From", source_type="web")
    into_id = _seed_source(configured_library, name="Into", source_type="web")
    _seed_article(configured_library, from_id, "m-1")
    _seed_article(configured_library, from_id, "m-2")

    r = authenticated_client.post(
        "/api/sources/merge", json={"from_id": from_id, "into_id": into_id}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["moved_articles"] == 2

    conn = get_connection(configured_library.db_path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM articles WHERE source_id = ?", (into_id,)
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sources WHERE id = ?", (from_id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_merge_ors_vip_into_target(authenticated_client, configured_library):
    from_id = _seed_source(
        configured_library, name="From", source_type="web", is_vip=True
    )
    into_id = _seed_source(
        configured_library, name="Into", source_type="web", is_vip=False
    )

    r = authenticated_client.post(
        "/api/sources/merge", json={"from_id": from_id, "into_id": into_id}
    )
    assert r.status_code == 200, r.text

    conn = get_connection(configured_library.db_path)
    try:
        is_vip = conn.execute(
            "SELECT is_vip FROM sources WHERE id = ?", (into_id,)
        ).fetchone()["is_vip"]
        assert bool(is_vip) is True
    finally:
        conn.close()


def test_merge_409_type_mismatch_without_force(authenticated_client, configured_library):
    from_id = _seed_source(configured_library, name="From", source_type="web")
    into_id = _seed_source(configured_library, name="Into", source_type="email")

    r = authenticated_client.post(
        "/api/sources/merge", json={"from_id": from_id, "into_id": into_id}
    )
    assert r.status_code == 409
    assert r.json()["error"] == "type_mismatch"

    # Nothing changed.
    conn = get_connection(configured_library.db_path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sources WHERE id = ?", (from_id,)
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_merge_succeeds_with_force_despite_type_mismatch(
    authenticated_client, configured_library
):
    from_id = _seed_source(configured_library, name="From", source_type="web")
    into_id = _seed_source(configured_library, name="Into", source_type="email")
    _seed_article(configured_library, from_id, "m-3")

    r = authenticated_client.post(
        "/api/sources/merge",
        json={"from_id": from_id, "into_id": into_id, "force": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["moved_articles"] == 1


def test_merge_400_same_id(authenticated_client, configured_library):
    source_id = _seed_source(configured_library)
    r = authenticated_client.post(
        "/api/sources/merge", json={"from_id": source_id, "into_id": source_id}
    )
    assert r.status_code == 400


def test_merge_400_missing_source(authenticated_client, configured_library):
    source_id = _seed_source(configured_library)
    r = authenticated_client.post(
        "/api/sources/merge", json={"from_id": source_id, "into_id": 99999}
    )
    assert r.status_code == 400


# --- PATCH /api/sources/{id} --------------------------------------------------


def test_patch_source_updates_only_provided_fields(
    authenticated_client, configured_library
):
    source_id = _seed_source(configured_library, name="Old Name")
    conn = get_connection(configured_library.db_path)
    try:
        conn.execute(
            "UPDATE sources SET domain = ?, email_sender = ? WHERE id = ?",
            ("old.example.com", "old@example.com", source_id),
        )
        conn.commit()
    finally:
        conn.close()

    r = authenticated_client.patch(
        f"/api/sources/{source_id}", json={"name": "New Name"}
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["name"] == "New Name"
    assert data["domain"] == "old.example.com"
    assert data["email_sender"] == "old@example.com"


def test_patch_source_404_unknown(authenticated_client):
    r = authenticated_client.patch("/api/sources/99999", json={"name": "X"})
    assert r.status_code == 404


def test_delete_requires_auth(auth_client, configured_library):
    source_id = _seed_source(configured_library)
    assert auth_client.delete(f"/api/sources/{source_id}").status_code == 401


def test_merge_requires_auth(auth_client, configured_library):
    a = _seed_source(configured_library, name="A")
    b = _seed_source(configured_library, name="B")
    assert (
        auth_client.post("/api/sources/merge", json={"from_id": a, "into_id": b}).status_code
        == 401
    )


def test_patch_requires_auth(auth_client, configured_library):
    source_id = _seed_source(configured_library)
    assert (
        auth_client.patch(f"/api/sources/{source_id}", json={"name": "X"}).status_code
        == 401
    )


# --- /sources page ------------------------------------------------------


def test_sources_page_redirects_when_anonymous(auth_client):
    r = auth_client.get("/sources", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_sources_page_renders_authenticated(authenticated_client):
    r = authenticated_client.get("/sources")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_sources_page_has_both_tabs(authenticated_client):
    r = authenticated_client.get("/sources")
    assert ">Sources<" in r.text
    assert ">Authors<" in r.text


def test_sources_page_loads_sources_js(authenticated_client):
    r = authenticated_client.get("/sources")
    assert "/static/sources.js?v=" in r.text


def test_sidebar_has_sources_link(authenticated_client):
    r = authenticated_client.get("/inbox")
    assert 'href="/sources"' in r.text
