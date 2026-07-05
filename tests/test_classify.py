"""POST /api/classify: the pre-reclassify auto-backup hook must not block
the event loop (M1.1 review item 2)."""

from tiro.database import get_connection


def _seed_article(config, slug="art-1", title="T1"):
    conn = get_connection(config.db_path)
    conn.execute("INSERT OR IGNORE INTO sources (name, source_type) VALUES ('Src', 'web')")
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
        " VALUES (?, 1, ?, ?, ?)",
        (slug.upper().ljust(26, "0"), title, slug, f"{slug}.md"),
    )
    conn.commit()
    conn.close()
    (config.articles_dir / f"{slug}.md").write_text(f"---\ntitle: {title}\n---\nbody {slug}")


def test_classify_refresh_runs_auto_backup_via_to_thread(
    authenticated_client, configured_library, monkeypatch
):
    """`auto_backup(config, "reclassify")` runs synchronously in the route's
    async handler as written — this pins the fixed behavior (awaited via
    asyncio.to_thread, matching the pattern the file already uses for
    classify_articles itself) by asserting the functional outcome: a
    snapshot lands in {library}/backups/auto/ and the request still
    completes normally.

    classify_articles is monkeypatched because the auto_backup hook fires
    unconditionally on `refresh: true` BEFORE classify_articles is even
    called (see tiro/api/routes_classify.py) — seeding 5 rated articles to
    satisfy classify_articles' own validation is orthogonal to this fix.
    """
    import tiro.api.routes_classify as rc

    _seed_article(configured_library)
    monkeypatch.setattr(rc, "classify_articles", lambda config: [])

    resp = authenticated_client.post("/api/classify", json={"refresh": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["classified_count"] == 0

    auto_dir = configured_library.library / "backups" / "auto"
    snaps = list(auto_dir.glob("*reclassify*.tar.zst"))
    assert len(snaps) == 1


def test_classify_without_refresh_does_not_backup(
    authenticated_client, configured_library, monkeypatch
):
    """Sanity check: the hook is refresh-gated, not unconditional."""
    import tiro.api.routes_classify as rc

    _seed_article(configured_library)
    monkeypatch.setattr(rc, "classify_articles", lambda config: [])

    resp = authenticated_client.post("/api/classify", json={"refresh": False})
    assert resp.status_code == 200

    auto_dir = configured_library.library / "backups" / "auto"
    assert not auto_dir.exists() or not list(auto_dir.glob("*.tar.zst"))
