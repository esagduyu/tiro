"""Migration 016 (sync S2): sync_shadow table.

Campaign note: migration numbers are pre-assigned across tracks (014 K-track,
015 S1, 016 S2) and the framework tolerates version gaps generally
(run_migrations applies sorted versions > current); 014/015 are both present
on this branch post-rebase, so migrating from user_version 15 applies 016 only.
"""
import sqlite3
from pathlib import Path

from tiro.database import get_connection
from tiro.migrations import MIGRATIONS, run_migrations


def _pre016_library(tmp_path: Path) -> Path:
    lib = tmp_path / "lib"
    (lib / "articles").mkdir(parents=True)
    db_path = lib / "tiro.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT, title TEXT NOT NULL, slug TEXT UNIQUE NOT NULL,
            markdown_path TEXT NOT NULL
        );
        INSERT INTO articles (uid, title, slug, markdown_path)
            VALUES ('01X', 'T', 's', 's.md');
        """
    )
    conn.execute("PRAGMA user_version = 15")
    conn.commit()
    conn.close()
    return db_path


def test_migration_016_creates_sync_shadow(tmp_path):
    db_path = _pre016_library(tmp_path)
    applied = run_migrations(db_path, backup=False)
    assert any(a.startswith("016") for a in applied)
    # Sync S2 claimed exactly migration 016 (sync_shadow). Assert that
    # specific claim rather than "016 is newest": K3 (017) lands on merge —
    # the durable invariant is the number, not the ceiling (7c2ca0b precedent).
    by_version = {v: desc for v, desc, _ in MIGRATIONS}
    assert "sync_shadow" in by_version[16]
    conn = get_connection(db_path)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_xinfo(sync_shadow)")}
        assert cols == {"kind", "uid", "hash", "fields_json", "hlc", "deleted_at"}
    finally:
        conn.close()


def test_migration_016_idempotent(tmp_path):
    db_path = _pre016_library(tmp_path)
    run_migrations(db_path, backup=False)
    assert run_migrations(db_path, backup=False) == []


def test_fresh_schema_has_sync_shadow(initialized_library):
    conn = get_connection(initialized_library.db_path)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_xinfo(sync_shadow)")}
        assert cols == {"kind", "uid", "hash", "fields_json", "hlc", "deleted_at"}
    finally:
        conn.close()
