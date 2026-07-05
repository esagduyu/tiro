from datetime import UTC, datetime, timedelta

import pytest

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


# --- Author VIP feeds decay ---------------------------------------------------


def test_recalculate_decay_vip_author_slows_decay(initialized_library):
    """An article by a VIP author (source itself not VIP) decays at
    decay_rate_vip, same as a VIP-source article would — matching the
    OR semantics: s.is_vip OR any linked author.is_vip."""
    from tiro.database import get_connection
    from tiro.decay import GRACE_PERIOD_DAYS, recalculate_decay

    config = initialized_library
    old_ts = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection(config.db_path)
    try:
        conn.execute("INSERT INTO sources (name, source_type) VALUES ('Neutral Source', 'web')")
        source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

        # VIP-author article: source is NOT VIP, but the linked author is.
        conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path, ingested_at)"
            " VALUES (?, ?, 'VIP author piece', 'vip-a', 'vip-a.md', ?)",
            ("VIPA".ljust(26, "0"), source_id, old_ts),
        )
        vip_article_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        author_id = ensure_author(conn, "VIP Writer")
        conn.execute("UPDATE authors SET is_vip = 1 WHERE id = ?", (author_id,))
        link_article_author(conn, vip_article_id, "VIP Writer")

        # Non-VIP twin: identical source and age, no VIP author link.
        conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path, ingested_at)"
            " VALUES (?, ?, 'Plain piece', 'plain-a', 'plain-a.md', ?)",
            ("PLAINA".ljust(26, "0"), source_id, old_ts),
        )
        plain_article_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
    finally:
        conn.close()

    recalculate_decay(config)

    conn = get_connection(config.db_path)
    try:
        vip_weight = conn.execute(
            "SELECT relevance_weight FROM articles WHERE id = ?", (vip_article_id,)
        ).fetchone()["relevance_weight"]
        plain_weight = conn.execute(
            "SELECT relevance_weight FROM articles WHERE id = ?", (plain_article_id,)
        ).fetchone()["relevance_weight"]
    finally:
        conn.close()

    decay_days = 30 - GRACE_PERIOD_DAYS
    expected_vip = config.decay_rate_vip**decay_days
    expected_plain = config.decay_rate_default**decay_days
    assert vip_weight == pytest.approx(expected_vip, rel=1e-6)
    assert plain_weight == pytest.approx(expected_plain, rel=1e-6)
    assert vip_weight > plain_weight


def test_recalculate_decay_liked_vip_author_article_stays_immune(initialized_library):
    """Liked/Loved articles are immune to decay regardless of VIP-author
    status — the rating check must still short-circuit before the VIP
    rate is even considered."""
    from tiro.database import get_connection
    from tiro.decay import recalculate_decay

    config = initialized_library
    old_ts = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection(config.db_path)
    try:
        conn.execute("INSERT INTO sources (name, source_type) VALUES ('Neutral Source', 'web')")
        source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path, ingested_at, rating)"
            " VALUES (?, ?, 'Liked VIP author piece', 'liked-vip-a', 'liked-vip-a.md', ?, 1)",
            ("LIKEDVIPA".ljust(26, "0"), source_id, old_ts),
        )
        article_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        author_id = ensure_author(conn, "VIP Writer 2")
        conn.execute("UPDATE authors SET is_vip = 1 WHERE id = ?", (author_id,))
        link_article_author(conn, article_id, "VIP Writer 2")
        conn.commit()
    finally:
        conn.close()

    recalculate_decay(config)

    conn = get_connection(config.db_path)
    try:
        weight = conn.execute(
            "SELECT relevance_weight FROM articles WHERE id = ?", (article_id,)
        ).fetchone()["relevance_weight"]
    finally:
        conn.close()
    assert weight == 1.0


# --- Author VIP feeds the digest prompt ---------------------------------------


def test_digest_prompt_includes_vip_author(initialized_library, fake_llm, monkeypatch):
    """A VIP author's name reaches the composed digest prompt via the new
    `vip_authors` argument, the same seam pattern as the extraction
    truncation test in test_llm.py: monkeypatch `daily_digest_prompt`
    where digest.py imports it, capture args, delegate to the real impl."""
    from tiro.database import get_connection
    from tiro.intelligence import digest as digest_mod

    config = initialized_library
    conn = get_connection(config.db_path)
    try:
        conn.execute("INSERT INTO sources (name, source_type) VALUES ('Neutral Source', 'web')")
        source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path, summary)"
            " VALUES (?, ?, 'By a VIP author', 'vip-author-piece', 'vip-author-piece.md', 'sum')",
            ("VIPAUTHORDIGEST".ljust(26, "0"), source_id),
        )
        article_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        author_id = ensure_author(conn, "Digest VIP Writer")
        conn.execute("UPDATE authors SET is_vip = 1 WHERE id = ?", (author_id,))
        link_article_author(conn, article_id, "Digest VIP Writer")
        conn.commit()
    finally:
        conn.close()

    real_prompt_fn = digest_mod.daily_digest_prompt
    captured = {}

    def capture_prompt(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        composed = real_prompt_fn(*args, **kwargs)
        captured["composed"] = composed
        return composed

    monkeypatch.setattr(digest_mod, "daily_digest_prompt", capture_prompt)

    fake_llm(
        "## 1. Ranked by Importance\n1. T\n\n"
        "## 2. Grouped by Topic\n- T\n\n"
        "## 3. Grouped by Entity\n- T"
    )
    result = digest_mod.generate_digest(config)
    assert set(result.keys()) == {"ranked", "by_topic", "by_entity"}

    # vip_authors reached daily_digest_prompt (positionally or by kwarg).
    all_args = list(captured["args"]) + list(captured["kwargs"].values())
    assert any(
        isinstance(a, list) and "Digest VIP Writer" in a for a in all_args
    ), captured

    # Pin that the author name actually makes it into the COMPOSED prompt
    # string (not just passed as an argument) — guards against the
    # `{vip_authors_line}` placeholder being silently dropped from
    # daily_digest.txt, since str.format() ignores unused kwargs.
    assert "Digest VIP Writer" in captured["composed"], captured["composed"]

    # The article gathered for the article itself is also flagged VIP via
    # the author link even though its source is not VIP.
    articles_arg = next(a for a in captured["args"] if isinstance(a, list) and a and isinstance(a[0], dict))
    gathered = next(a for a in articles_arg if a["id"] == article_id)
    assert gathered["is_vip"] is True
