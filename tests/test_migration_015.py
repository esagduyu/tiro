"""Migration 015 (sync S1): articles.body_hash + meta_updated_at + sources.uid.

Campaign note: 014 is RESERVED (K-track agent_runs) and absent on this branch;
the framework tolerates the gap (run_migrations applies sorted versions > current).
"""
import sqlite3
from pathlib import Path

import frontmatter

from tiro.anchors import content_hash
from tiro.database import get_connection
from tiro.ingestion.processor import process_article
from tiro.migrations import LATEST_VERSION, run_migrations

BODY = "# Hello\n\nBody text for hashing."


def _legacy_library(tmp_path: Path) -> Path:
    """Minimal pre-015 library stamped at user_version 13, with one article
    markdown file on disk for the body_hash backfill."""
    lib = tmp_path / "lib"
    (lib / "articles").mkdir(parents=True)
    db_path = lib / "tiro.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, domain TEXT, email_sender TEXT,
            source_type TEXT NOT NULL, is_vip BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT, source_id INTEGER, title TEXT NOT NULL,
            author TEXT, url TEXT, slug TEXT UNIQUE NOT NULL,
            markdown_path TEXT NOT NULL, summary TEXT
        );
        INSERT INTO sources (name, domain, source_type)
            VALUES ('Example', 'example.com', 'web');
        INSERT INTO articles (uid, source_id, title, slug, markdown_path)
            VALUES ('01LEGACYUID', 1, 'Hello', '2026-07-10_hello', '2026-07-10_hello.md');
        INSERT INTO articles (uid, source_id, title, slug, markdown_path)
            VALUES ('01LEGACYUI2', 1, 'Gone', '2026-07-10_gone', '2026-07-10_gone.md');
        """
    )
    conn.execute("PRAGMA user_version = 13")
    conn.commit()
    conn.close()
    post = frontmatter.Post(BODY)
    post.metadata = {"title": "Hello"}
    (lib / "articles" / "2026-07-10_hello.md").write_text(frontmatter.dumps(post))
    # 'Gone' deliberately has no file: backfill must leave body_hash NULL, not crash.
    return db_path


def test_migration_015_adds_and_backfills(tmp_path):
    db_path = _legacy_library(tmp_path)
    applied = run_migrations(db_path, backup=False)
    assert any(a.startswith("015") for a in applied)
    assert LATEST_VERSION == 15

    conn = get_connection(db_path)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_xinfo(articles)")}
        assert {"body_hash", "meta_updated_at"} <= cols
        scols = {r["name"] for r in conn.execute("PRAGMA table_xinfo(sources)")}
        assert "uid" in scols

        src = conn.execute("SELECT uid FROM sources WHERE id = 1").fetchone()
        assert src["uid"]  # backfilled ULID

        hello = conn.execute(
            "SELECT body_hash, meta_updated_at FROM articles WHERE slug = '2026-07-10_hello'"
        ).fetchone()
        assert hello["body_hash"] == content_hash(BODY)
        assert hello["meta_updated_at"] is None

        gone = conn.execute(
            "SELECT body_hash FROM articles WHERE slug = '2026-07-10_gone'"
        ).fetchone()
        assert gone["body_hash"] is None  # missing file: NULL, no crash
    finally:
        conn.close()


def test_migration_015_idempotent(tmp_path):
    db_path = _legacy_library(tmp_path)
    run_migrations(db_path, backup=False)
    # Re-running from 15 applies nothing and doesn't raise on existing columns.
    assert run_migrations(db_path, backup=False) == []


def test_fresh_schema_has_sync_columns(initialized_library):
    conn = get_connection(initialized_library.db_path)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_xinfo(articles)")}
        assert {"body_hash", "meta_updated_at"} <= cols
        scols = {r["name"] for r in conn.execute("PRAGMA table_xinfo(sources)")}
        assert "uid" in scols
    finally:
        conn.close()


def test_new_sources_created_with_uid(initialized_library):
    # Offline conftest: extract_metadata degrades to empty defaults, no key needed.
    process_article(
        title="T", author=None, content_md="Body words here.",
        url="https://uidcheck.example.com/a", config=initialized_library,
    )
    conn = get_connection(initialized_library.db_path)
    try:
        row = conn.execute(
            "SELECT uid FROM sources WHERE domain = 'uidcheck.example.com'"
        ).fetchone()
        assert row is not None and row["uid"]
    finally:
        conn.close()
