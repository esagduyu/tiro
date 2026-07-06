"""Highlights + notes CRUD API (tiro/api/routes_annotations.py).

Every mutation test asserts BOTH the sidecar file content and the derived
SQLite rows, per the sidecar-first convention (file write happens before the
index update)."""

import json

from tiro.annotations import annotations_dir, read_note
from tiro.database import get_connection
from tiro.migrations import new_ulid

BODY = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 3
# len(BODY) is comfortably > 64 so make_anchor's default 32-char context fits.


def _seed_article(config, stem="article-1", title="T", body=BODY, source_vip=False):
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            "INSERT INTO sources (name, source_type, is_vip) VALUES ('s', 'web', ?)",
            (source_vip,),
        )
        source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        article_uid = new_ulid()
        conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
            " VALUES (?, ?, ?, ?, ?)",
            (article_uid, source_id, title, stem, f"{stem}.md"),
        )
        article_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
        return article_id, article_uid
    finally:
        conn.close()


def _write_markdown(config, stem, body=BODY, title="T"):
    (config.articles_dir / f"{stem}.md").write_text(f"---\ntitle: {title}\n---\n{body}")


def _jsonl_lines(config, stem):
    path = annotations_dir(config) / f"{stem}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- GET /api/articles/{id}/annotations --------------------------------------


def test_get_annotations_unknown_article_404(authenticated_client):
    r = authenticated_client.get("/api/articles/999/annotations")
    assert r.status_code == 404


def test_get_annotations_empty(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")

    r = authenticated_client.get(f"/api/articles/{article_id}/annotations")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["highlights"] == []
    assert data["note"] is None
    assert isinstance(data["content_hash"], str) and data["content_hash"]


def test_get_annotations_requires_auth(auth_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")
    r = auth_client.get(f"/api/articles/{article_id}/annotations")
    assert r.status_code in (401, 302)


# --- POST /api/articles/{id}/highlights --------------------------------------


def test_create_highlight_round_trip_file_and_rows(authenticated_client, configured_library):
    config = configured_library
    article_id, article_uid = _seed_article(config)
    _write_markdown(config, "article-1")

    r = authenticated_client.post(
        f"/api/articles/{article_id}/highlights",
        json={"position_start": 0, "position_end": 11},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["quote_text"] == BODY[0:11]
    assert data["color"] == "yellow"
    uid = data["uid"]

    # File assertion: sidecar has exactly one line with the expected fields.
    lines = _jsonl_lines(config, "article-1")
    assert len(lines) == 1
    assert lines[0]["uid"] == uid
    assert lines[0]["article_uid"] == article_uid
    assert lines[0]["quote"] == BODY[0:11]
    assert lines[0]["position_start"] == 0
    assert lines[0]["position_end"] == 11
    assert lines[0]["color"] == "yellow"
    assert lines[0]["content_hash"]

    # Row assertion.
    conn = get_connection(config.db_path)
    try:
        row = conn.execute("SELECT * FROM highlights WHERE uid = ?", (uid,)).fetchone()
        assert row is not None
        assert row["article_id"] == article_id
        assert row["quote_text"] == BODY[0:11]
        assert row["color"] == "yellow"
    finally:
        conn.close()


def test_create_highlight_custom_color(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")

    r = authenticated_client.post(
        f"/api/articles/{article_id}/highlights",
        json={"position_start": 0, "position_end": 5, "color": "green"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["color"] == "green"


def test_create_highlight_invalid_color_400(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")

    r = authenticated_client.post(
        f"/api/articles/{article_id}/highlights",
        json={"position_start": 0, "position_end": 5, "color": "purple"},
    )
    assert r.status_code == 400
    # No file/row created on validation failure.
    assert _jsonl_lines(config, "article-1") == []


def test_create_highlight_bad_bounds_400(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")

    r = authenticated_client.post(
        f"/api/articles/{article_id}/highlights",
        json={"position_start": 10, "position_end": 5},
    )
    assert r.status_code == 400
    assert _jsonl_lines(config, "article-1") == []

    r2 = authenticated_client.post(
        f"/api/articles/{article_id}/highlights",
        json={"position_start": 0, "position_end": len(BODY) + 1000},
    )
    assert r2.status_code == 400


def test_create_highlight_unknown_article_404(authenticated_client):
    r = authenticated_client.post(
        "/api/articles/999/highlights", json={"position_start": 0, "position_end": 5}
    )
    assert r.status_code == 404


# --- PATCH /api/highlights/{uid} ---------------------------------------------


def _create_highlight(client, article_id, start=0, end=11, color=None):
    payload = {"position_start": start, "position_end": end}
    if color:
        payload["color"] = color
    r = client.post(f"/api/articles/{article_id}/highlights", json=payload)
    assert r.status_code == 200
    return r.json()["data"]["uid"]


def test_patch_highlight_color_round_trip(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")
    uid = _create_highlight(authenticated_client, article_id)

    r = authenticated_client.patch(f"/api/highlights/{uid}", json={"color": "blue"})
    assert r.status_code == 200
    assert r.json()["data"]["color"] == "blue"

    lines = _jsonl_lines(config, "article-1")
    assert lines[0]["color"] == "blue"

    conn = get_connection(config.db_path)
    try:
        row = conn.execute("SELECT color FROM highlights WHERE uid = ?", (uid,)).fetchone()
        assert row["color"] == "blue"
    finally:
        conn.close()


def test_patch_highlight_invalid_color_400(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")
    uid = _create_highlight(authenticated_client, article_id)

    r = authenticated_client.patch(f"/api/highlights/{uid}", json={"color": "nope"})
    assert r.status_code == 400
    # Unchanged.
    lines = _jsonl_lines(config, "article-1")
    assert lines[0]["color"] == "yellow"


def test_patch_highlight_sets_and_clears_note_file_and_row(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")
    uid = _create_highlight(authenticated_client, article_id)

    r = authenticated_client.patch(f"/api/highlights/{uid}", json={"note_markdown": "my note"})
    assert r.status_code == 200
    assert r.json()["data"]["note_markdown"] == "my note"

    lines = _jsonl_lines(config, "article-1")
    assert lines[0]["note_markdown"] == "my note"

    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", (uid,)
        ).fetchone()
        note_row = conn.execute(
            "SELECT body_markdown FROM notes WHERE highlight_id = ?", (row["id"],)
        ).fetchone()
        assert note_row["body_markdown"] == "my note"
    finally:
        conn.close()

    # Clear via empty string.
    r2 = authenticated_client.patch(f"/api/highlights/{uid}", json={"note_markdown": ""})
    assert r2.status_code == 200
    assert r2.json()["data"]["note_markdown"] is None

    lines2 = _jsonl_lines(config, "article-1")
    assert lines2[0]["note_markdown"] is None

    conn = get_connection(config.db_path)
    try:
        row = conn.execute("SELECT id FROM highlights WHERE uid = ?", (uid,)).fetchone()
        note_row = conn.execute(
            "SELECT * FROM notes WHERE highlight_id = ?", (row["id"],)
        ).fetchone()
        assert note_row is None
    finally:
        conn.close()


def test_patch_highlight_omitted_fields_unchanged(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")
    uid = _create_highlight(authenticated_client, article_id, color="green")
    authenticated_client.patch(f"/api/highlights/{uid}", json={"note_markdown": "n1"})

    # PATCH color only -> note stays.
    r = authenticated_client.patch(f"/api/highlights/{uid}", json={"color": "pink"})
    assert r.status_code == 200
    assert r.json()["data"]["color"] == "pink"
    assert r.json()["data"]["note_markdown"] == "n1"


def test_patch_highlight_unknown_uid_404(authenticated_client):
    r = authenticated_client.patch("/api/highlights/does-not-exist", json={"color": "blue"})
    assert r.status_code == 404


def test_patch_highlight_orphaned_article_404s_not_500(authenticated_client, configured_library):
    """Defense-in-depth (Task 4 review item): delete_article's cascade makes
    a highlight outliving its article impossible going forward, but
    _get_highlight_or_404 must still 404 cleanly -- not 500 -- if the
    article row is ever missing. Hand-crafted here (FK checks disabled) to
    simulate the impossible-by-construction state."""
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")
    uid = _create_highlight(authenticated_client, article_id)

    conn = get_connection(config.db_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
        conn.commit()
    finally:
        conn.close()

    r = authenticated_client.patch(f"/api/highlights/{uid}", json={"color": "blue"})
    assert r.status_code == 404


# --- DELETE /api/highlights/{uid} --------------------------------------------


def test_delete_highlight_removes_file_line_and_rows(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")
    uid = _create_highlight(authenticated_client, article_id)
    authenticated_client.patch(f"/api/highlights/{uid}", json={"note_markdown": "n"})

    r = authenticated_client.delete(f"/api/highlights/{uid}")
    assert r.status_code == 200

    # Sidecar file is gone entirely (last highlight removed).
    assert not (annotations_dir(config) / "article-1.jsonl").exists()

    conn = get_connection(config.db_path)
    try:
        assert conn.execute("SELECT * FROM highlights WHERE uid = ?", (uid,)).fetchone() is None
        assert conn.execute(
            "SELECT * FROM notes WHERE highlight_id IS NOT NULL"
        ).fetchall() == []
    finally:
        conn.close()


def test_delete_highlight_keeps_sibling_highlights(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")
    uid1 = _create_highlight(authenticated_client, article_id, start=0, end=5)
    uid2 = _create_highlight(authenticated_client, article_id, start=6, end=11)

    r = authenticated_client.delete(f"/api/highlights/{uid1}")
    assert r.status_code == 200

    lines = _jsonl_lines(config, "article-1")
    assert len(lines) == 1
    assert lines[0]["uid"] == uid2


def test_delete_highlight_unknown_uid_404(authenticated_client):
    r = authenticated_client.delete("/api/highlights/does-not-exist")
    assert r.status_code == 404


# --- cross-article uid isolation ---------------------------------------------


def test_patch_and_delete_do_not_leak_across_articles(authenticated_client, configured_library):
    config = configured_library
    a1, _ = _seed_article(config, stem="article-a", title="A")
    a2, _ = _seed_article(config, stem="article-b", title="B")
    _write_markdown(config, "article-a")
    _write_markdown(config, "article-b")

    uid_a = _create_highlight(authenticated_client, a1)
    uid_b = _create_highlight(authenticated_client, a2)

    # PATCH the article-a highlight; article-b's sidecar must be untouched.
    r = authenticated_client.patch(f"/api/highlights/{uid_a}", json={"color": "blue"})
    assert r.status_code == 200

    lines_a = _jsonl_lines(config, "article-a")
    lines_b = _jsonl_lines(config, "article-b")
    assert lines_a[0]["uid"] == uid_a and lines_a[0]["color"] == "blue"
    assert lines_b[0]["uid"] == uid_b and lines_b[0]["color"] == "yellow"

    # DELETE the article-b highlight; article-a's file/row must be untouched.
    r2 = authenticated_client.delete(f"/api/highlights/{uid_b}")
    assert r2.status_code == 200
    assert not (annotations_dir(config) / "article-b.jsonl").exists()
    assert _jsonl_lines(config, "article-a")[0]["uid"] == uid_a

    conn = get_connection(config.db_path)
    try:
        assert conn.execute("SELECT * FROM highlights WHERE uid = ?", (uid_a,)).fetchone() is not None
        assert conn.execute("SELECT * FROM highlights WHERE uid = ?", (uid_b,)).fetchone() is None
    finally:
        conn.close()


# --- PUT/DELETE /api/articles/{id}/note --------------------------------------


def test_put_note_round_trip_file_and_row(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")

    r = authenticated_client.put(
        f"/api/articles/{article_id}/note", json={"body_markdown": "# Notes\n\nhello"}
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["body_markdown"] == "# Notes\n\nhello"

    assert read_note(config, "article-1") == "# Notes\n\nhello"

    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM notes WHERE article_id = ? AND highlight_id IS NULL", (article_id,)
        ).fetchone()
        assert row["body_markdown"] == "# Notes\n\nhello"
    finally:
        conn.close()

    # Update in place (same uid).
    r2 = authenticated_client.put(
        f"/api/articles/{article_id}/note", json={"body_markdown": "updated body"}
    )
    assert r2.status_code == 200
    assert r2.json()["data"]["uid"] == data["uid"]
    assert read_note(config, "article-1") == "updated body"


def test_put_note_empty_body_400(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")

    r = authenticated_client.put(f"/api/articles/{article_id}/note", json={"body_markdown": ""})
    assert r.status_code == 400
    assert read_note(config, "article-1") is None

    r2 = authenticated_client.put(f"/api/articles/{article_id}/note", json={"body_markdown": "   "})
    assert r2.status_code == 400


def test_delete_note_removes_file_and_row(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")
    authenticated_client.put(f"/api/articles/{article_id}/note", json={"body_markdown": "x"})

    r = authenticated_client.delete(f"/api/articles/{article_id}/note")
    assert r.status_code == 200
    assert read_note(config, "article-1") is None

    conn = get_connection(config.db_path)
    try:
        assert conn.execute(
            "SELECT * FROM notes WHERE article_id = ? AND highlight_id IS NULL", (article_id,)
        ).fetchone() is None
    finally:
        conn.close()


def test_delete_note_idempotent_when_none(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")
    r = authenticated_client.delete(f"/api/articles/{article_id}/note")
    assert r.status_code == 200


def test_note_unknown_article_404(authenticated_client):
    r = authenticated_client.put("/api/articles/999/note", json={"body_markdown": "x"})
    assert r.status_code == 404
    r2 = authenticated_client.delete("/api/articles/999/note")
    assert r2.status_code == 404


# --- GET /api/highlights (flat list) ------------------------------------------


def test_list_highlights_filters_and_join(authenticated_client, configured_library):
    config = configured_library
    a1, _ = _seed_article(config, stem="article-a", title="A")
    a2, _ = _seed_article(config, stem="article-b", title="B", source_vip=True)
    _write_markdown(config, "article-a")
    _write_markdown(config, "article-b")

    uid1 = _create_highlight(authenticated_client, a1, color="green")
    uid2 = _create_highlight(authenticated_client, a2, color="blue")

    r = authenticated_client.get("/api/highlights")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 2
    uids = {h["uid"] for h in data["highlights"]}
    assert uids == {uid1, uid2}
    by_uid = {h["uid"]: h for h in data["highlights"]}
    assert by_uid[uid1]["article_title"] == "A"
    assert by_uid[uid2]["article_title"] == "B"

    # Filter by article_id.
    r2 = authenticated_client.get("/api/highlights", params={"article_id": a1})
    assert [h["uid"] for h in r2.json()["data"]["highlights"]] == [uid1]

    # Filter by color.
    r3 = authenticated_client.get("/api/highlights", params={"color": "blue"})
    assert [h["uid"] for h in r3.json()["data"]["highlights"]] == [uid2]

    # Filter by source_id.
    conn = get_connection(config.db_path)
    try:
        source_id = conn.execute(
            "SELECT source_id FROM articles WHERE id = ?", (a2,)
        ).fetchone()["source_id"]
    finally:
        conn.close()
    r4 = authenticated_client.get("/api/highlights", params={"source_id": source_id})
    assert [h["uid"] for h in r4.json()["data"]["highlights"]] == [uid2]


def test_list_highlights_pagination(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")
    for i in range(3):
        _create_highlight(authenticated_client, article_id, start=i * 12, end=i * 12 + 5)

    r = authenticated_client.get("/api/highlights", params={"limit": 2, "offset": 0})
    data = r.json()["data"]
    assert data["total"] == 3
    assert len(data["highlights"]) == 2

    r2 = authenticated_client.get("/api/highlights", params={"limit": 2, "offset": 2})
    assert len(r2.json()["data"]["highlights"]) == 1


# --- GET annotations reconcile statuses --------------------------------------


def test_get_annotations_reconcile_exact_status(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")
    uid = _create_highlight(authenticated_client, article_id, start=0, end=11)

    r = authenticated_client.get(f"/api/articles/{article_id}/annotations")
    highlight = r.json()["data"]["highlights"][0]
    assert highlight["uid"] == uid
    assert highlight["anchor_status"]["status"] == "exact"
    assert highlight["anchor_status"]["position_start"] == 0


def test_get_annotations_reconcile_shifted_status(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")
    _create_highlight(authenticated_client, article_id, start=0, end=11)

    # Prepend text so the same quote now sits at a different offset, with the
    # same surrounding context still findable -> "shifted", not "exact".
    _write_markdown(config, "article-1", body="PREFIX TEXT HERE. " + BODY)

    r = authenticated_client.get(f"/api/articles/{article_id}/annotations")
    highlight = r.json()["data"]["highlights"][0]
    assert highlight["anchor_status"]["status"] == "shifted"
    assert highlight["anchor_status"]["position_start"] == len("PREFIX TEXT HERE. ")


def test_get_annotations_reconcile_hash_mismatch_status(authenticated_client, configured_library):
    config = configured_library
    article_id, _ = _seed_article(config)
    _write_markdown(config, "article-1")
    _create_highlight(authenticated_client, article_id, start=0, end=11)

    # Replace the article body entirely -- the quote is no longer findable.
    _write_markdown(config, "article-1", body="Completely different content, nothing matches.")

    r = authenticated_client.get(f"/api/articles/{article_id}/annotations")
    highlight = r.json()["data"]["highlights"][0]
    assert highlight["anchor_status"]["status"] == "hash_mismatch"
    assert highlight["anchor_status"]["position_start"] is None

    # Top-level content_hash reflects the CURRENT body, not the stored one.
    from tiro.anchors import content_hash

    assert r.json()["data"]["content_hash"] == content_hash(
        "Completely different content, nothing matches."
    )
