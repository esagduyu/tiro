"""Feed management API (Phase 4 M4.0): CRUD, autodiscovery, cascade delete.

Autodiscovery is exercised offline by monkeypatching `_fetch_capped` (the one
HTTP seam in `_discover_feed`) to return fixture bytes.
"""

from pathlib import Path

from tiro.api import routes_feeds
from tiro.database import get_connection

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"

RSS_BYTES = (FIXTURES / "valid_rss.xml").read_bytes()

PAGE_WITH_ALTERNATE = (
    b'<!doctype html><html><head><title>A Blog</title>'
    b'<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
    b'</head><body><h1>hi</h1></body></html>'
)
PAGE_NO_FEED = b'<!doctype html><html><head><title>Nothing</title></head><body>x</body></html>'


def _mock_fetch(monkeypatch, responses: dict):
    """Patch routes_feeds._fetch_capped with a canned url -> (final_url, body) map."""
    async def fake(client, url):
        return responses[url]

    monkeypatch.setattr(routes_feeds, "_fetch_capped", fake)


def test_list_feeds_empty(authenticated_client):
    r = authenticated_client.get("/api/feeds")
    assert r.status_code == 200
    assert r.json() == {"success": True, "data": []}


def test_add_feed_direct_url(authenticated_client, monkeypatch):
    url = "https://example.com/rss.xml"
    _mock_fetch(monkeypatch, {url: (url, RSS_BYTES)})
    r = authenticated_client.post("/api/feeds", json={"url": url})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["url"] == url
    assert data["title"] == "Example Feed"
    assert data["source_id"]
    # A source row (source_type='rss') was created.
    r2 = authenticated_client.get("/api/feeds")
    assert len(r2.json()["data"]) == 1


def test_add_feed_via_autodiscovery(authenticated_client, monkeypatch):
    page = "https://blog.example.com/"
    feed = "https://blog.example.com/feed.xml"
    _mock_fetch(monkeypatch, {
        page: (page, PAGE_WITH_ALTERNATE),
        feed: (feed, RSS_BYTES),
    })
    r = authenticated_client.post("/api/feeds", json={"url": page})
    assert r.status_code == 200
    assert r.json()["data"]["url"] == feed  # resolved to the alternate link


def test_add_feed_no_feed_found(authenticated_client, monkeypatch):
    page = "https://nope.example.com/"
    _mock_fetch(monkeypatch, {page: (page, PAGE_NO_FEED)})
    r = authenticated_client.post("/api/feeds", json={"url": page})
    assert r.status_code == 422


def test_add_feed_bad_scheme(authenticated_client):
    r = authenticated_client.post("/api/feeds", json={"url": "ftp://x/y"})
    assert r.status_code == 400


def test_add_duplicate_feed_409(authenticated_client, monkeypatch):
    url = "https://example.com/rss.xml"
    _mock_fetch(monkeypatch, {url: (url, RSS_BYTES)})
    assert authenticated_client.post("/api/feeds", json={"url": url}).status_code == 200
    r = authenticated_client.post("/api/feeds", json={"url": url})
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "already_subscribed"
    assert "id" in body["data"] and "title" in body["data"]


def test_patch_pause_and_resume(authenticated_client, monkeypatch, configured_library):
    url = "https://example.com/rss.xml"
    _mock_fetch(monkeypatch, {url: (url, RSS_BYTES)})
    feed_id = authenticated_client.post("/api/feeds", json={"url": url}).json()["data"]["id"]

    # Put the feed into an error state directly.
    conn = get_connection(configured_library.db_path)
    try:
        conn.execute(
            "UPDATE feeds SET status = 'error', error_count = 5, last_error = 'boom' WHERE id = ?",
            (feed_id,),
        )
        conn.commit()
    finally:
        conn.close()

    # Pause.
    r = authenticated_client.patch(f"/api/feeds/{feed_id}", json={"status": "paused"})
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "paused"

    # Resume: status active + error state reset.
    r = authenticated_client.patch(f"/api/feeds/{feed_id}", json={"status": "active"})
    data = r.json()["data"]
    assert data["status"] == "active"
    assert data["error_count"] == 0
    assert data["last_error"] is None


def test_patch_title_folder_interval(authenticated_client, monkeypatch):
    url = "https://example.com/rss.xml"
    _mock_fetch(monkeypatch, {url: (url, RSS_BYTES)})
    feed_id = authenticated_client.post("/api/feeds", json={"url": url}).json()["data"]["id"]
    r = authenticated_client.patch(
        f"/api/feeds/{feed_id}",
        json={"title": "Renamed", "folder": "News", "fetch_interval_minutes": 180},
    )
    data = r.json()["data"]
    assert data["title"] == "Renamed"
    assert data["folder"] == "News"
    assert data["fetch_interval_minutes"] == 180


def test_patch_bad_status_400(authenticated_client, monkeypatch):
    url = "https://example.com/rss.xml"
    _mock_fetch(monkeypatch, {url: (url, RSS_BYTES)})
    feed_id = authenticated_client.post("/api/feeds", json={"url": url}).json()["data"]["id"]
    r = authenticated_client.patch(f"/api/feeds/{feed_id}", json={"status": "bogus"})
    assert r.status_code == 400


def test_patch_missing_feed_404(authenticated_client):
    assert authenticated_client.patch("/api/feeds/999", json={"title": "x"}).status_code == 404


def _ingest_one_article_for_feed(config, feed_id, monkeypatch_ctx):
    """Ingest a real article (markdown + vector + row) attributed to a feed's
    source via the rss pipeline, so cascade-delete has coordinator effects to
    assert. Uses the sidecar-free feed-fallback content path (offline)."""
    from tiro.ingestion import rss as rss_mod

    class OneShot:
        def __call__(self, client, feed_row):
            return 200, RSS_BYTES, {}

    monkeypatch_ctx.setattr(rss_mod, "_fetch_feed", OneShot())
    monkeypatch_ctx.setattr(rss_mod, "fetch_and_extract_sync",
                            lambda u: (_ for _ in ()).throw(RuntimeError("offline")))
    result = rss_mod.check_feeds(config, feed_id=feed_id)
    assert result["ingested"] >= 1


def test_delete_feed_keeps_articles_and_source(authenticated_client, monkeypatch, configured_library):
    url = "https://example.com/rss.xml"
    _mock_fetch(monkeypatch, {url: (url, RSS_BYTES)})
    resp = authenticated_client.post("/api/feeds", json={"url": url}).json()["data"]
    feed_id, source_id = resp["id"], resp["source_id"]
    _ingest_one_article_for_feed(configured_library, feed_id, monkeypatch)

    conn = get_connection(configured_library.db_path)
    try:
        before = conn.execute(
            "SELECT COUNT(*) AS n FROM articles WHERE source_id = ?", (source_id,)
        ).fetchone()["n"]
    finally:
        conn.close()
    assert before >= 1

    r = authenticated_client.delete(f"/api/feeds/{feed_id}")
    assert r.status_code == 200
    assert r.json()["data"]["deleted_articles"] == 0

    conn = get_connection(configured_library.db_path)
    try:
        # Feed + its ledger gone; articles + source stay.
        assert conn.execute("SELECT COUNT(*) AS n FROM feeds").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM feed_entries").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM articles WHERE source_id = ?", (source_id,)
        ).fetchone()["n"] == before
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE id = ?", (source_id,)
        ).fetchone()["n"] == 1
    finally:
        conn.close()


def test_delete_feed_cascade_deletes_articles(authenticated_client, monkeypatch, configured_library):
    url = "https://example.com/rss.xml"
    _mock_fetch(monkeypatch, {url: (url, RSS_BYTES)})
    resp = authenticated_client.post("/api/feeds", json={"url": url}).json()["data"]
    feed_id, source_id = resp["id"], resp["source_id"]
    _ingest_one_article_for_feed(configured_library, feed_id, monkeypatch)

    conn = get_connection(configured_library.db_path)
    try:
        rows = conn.execute(
            "SELECT id, markdown_path FROM articles WHERE source_id = ?", (source_id,)
        ).fetchall()
    finally:
        conn.close()
    assert rows
    md_paths = [configured_library.articles_dir / r["markdown_path"] for r in rows]
    assert all(p.exists() for p in md_paths)

    r = authenticated_client.delete(f"/api/feeds/{feed_id}?delete_articles=true")
    assert r.status_code == 200
    assert r.json()["data"]["deleted_articles"] == len(rows)

    conn = get_connection(configured_library.db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM articles WHERE source_id = ?", (source_id,)
        ).fetchone()["n"] == 0
        # Source removed (no remaining articles) and feed gone.
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE id = ?", (source_id,)
        ).fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM feeds").fetchone()["n"] == 0
    finally:
        conn.close()
    # Coordinator signature effect: markdown files removed.
    assert all(not p.exists() for p in md_paths)
    # auto_backup snapshot taken before the cascade.
    backups = list((configured_library.library / "backups").glob("*"))
    assert backups


def test_delete_missing_feed_404(authenticated_client):
    assert authenticated_client.delete("/api/feeds/999").status_code == 404


def test_check_single_feed(authenticated_client, monkeypatch, configured_library):
    url = "https://example.com/rss.xml"
    _mock_fetch(monkeypatch, {url: (url, RSS_BYTES)})
    feed_id = authenticated_client.post("/api/feeds", json={"url": url}).json()["data"]["id"]

    # Patch the pipeline's fetch seam so the manual check ingests offline.
    from tiro.ingestion import rss as rss_mod
    monkeypatch.setattr(rss_mod, "_fetch_feed", lambda c, f: (200, RSS_BYTES, {}))
    monkeypatch.setattr(rss_mod, "fetch_and_extract_sync",
                        lambda u: (_ for _ in ()).throw(RuntimeError("offline")))

    r = authenticated_client.post(f"/api/feeds/{feed_id}/check")
    assert r.status_code == 200
    assert r.json()["data"]["ingested"] == 2


def test_check_single_feed_404(authenticated_client):
    assert authenticated_client.post("/api/feeds/999/check").status_code == 404


def test_check_all_feeds(authenticated_client, monkeypatch):
    called = {"n": 0}

    def fake_check(config, feed_id=None):
        called["n"] += 1
        return {"feeds_checked": 0, "feeds_skipped": 0, "entries_seen": 0,
                "ingested": 0, "skipped": 0, "failed": 0, "failed_feeds": 0,
                "errors": [], "articles": []}

    # Patch the name the route imported.
    monkeypatch.setattr(routes_feeds, "check_feeds", fake_check)
    r = authenticated_client.post("/api/feeds/check-all")
    assert r.status_code == 200
    assert called["n"] == 1


def test_article_count_in_list(authenticated_client, monkeypatch, configured_library):
    url = "https://example.com/rss.xml"
    _mock_fetch(monkeypatch, {url: (url, RSS_BYTES)})
    feed_id = authenticated_client.post("/api/feeds", json={"url": url}).json()["data"]["id"]
    _ingest_one_article_for_feed(configured_library, feed_id, monkeypatch)
    r = authenticated_client.get("/api/feeds")
    row = next(f for f in r.json()["data"] if f["id"] == feed_id)
    assert row["article_count"] >= 1
