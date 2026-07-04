"""Migration framework: versioned, backed-up, idempotent over legacy DBs."""

from pathlib import Path

from tiro.database import get_connection, init_db
from tiro.migrations import LATEST_VERSION, run_migrations, schema_version


def _version(db_path: Path) -> int:
    conn = get_connection(db_path)
    try:
        return schema_version(conn)
    finally:
        conn.close()


def test_fresh_db_is_stamped_latest(tmp_path):
    db = tmp_path / "tiro.db"
    init_db(db)
    assert _version(db) == LATEST_VERSION


def test_migrations_apply_once_and_backup(tmp_path):
    db = tmp_path / "tiro.db"
    init_db(db)
    # Simulate an old DB: reset version to 0 (columns already exist,
    # exactly like a real pre-framework library)
    conn = get_connection(db)
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    applied = run_migrations(db)
    assert applied  # every migration re-applied idempotently
    assert _version(db) == LATEST_VERSION
    backups = list(tmp_path.glob("tiro.db.pre-migrate-*"))
    assert len(backups) == 1

    assert run_migrations(db) == []  # second run: nothing pending, no new backup
    assert len(list(tmp_path.glob("tiro.db.pre-migrate-*"))) == 1


def test_legacy_column_migrations_are_idempotent(tmp_path):
    """A DB that already has ingestion_method/vector_status (added by the old
    ad-hoc migrate_db) must survive the framework re-running those steps."""
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()]
    assert "ingestion_method" in cols and "vector_status" in cols
    conn.close()
    run_migrations(db)  # must not raise "duplicate column name"


def test_uid_migration_backfills_unique_ulids(tmp_path):
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'web')")
    for i in range(3):
        conn.execute(
            "INSERT INTO articles (source_id, title, slug, markdown_path) VALUES (1, ?, ?, ?)",
            (f"t{i}", f"slug-{i}", f"f{i}.md"),
        )
    conn.execute("INSERT INTO tags (name) VALUES ('ai')")
    conn.execute("INSERT INTO entities (name, entity_type) VALUES ('OpenAI', 'company')")
    conn.commit()
    # Fresh init_db already stamps LATEST_VERSION and SCHEMA includes uid —
    # so simulate the pre-uid world: version back to 2, columns dropped is not
    # possible in SQLite, so instead assert the backfill path fills NULLs.
    conn.execute("UPDATE articles SET uid = NULL")
    conn.execute("UPDATE tags SET uid = NULL")
    conn.execute("UPDATE entities SET uid = NULL")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    run_migrations(db)

    conn = get_connection(db)
    uids = [r[0] for r in conn.execute("SELECT uid FROM articles").fetchall()]
    assert all(u and len(u) == 26 for u in uids)
    assert len(set(uids)) == 3
    assert conn.execute("SELECT uid FROM tags").fetchone()[0]
    assert conn.execute("SELECT uid FROM entities").fetchone()[0]
    conn.close()


def test_startup_order_on_legacy_db(tmp_path):
    """init_db() then migrate_db() — the app.py lifespan order — must not
    crash on a pre-uid database (the upgrade path for every existing install)."""
    import sqlite3

    db = tmp_path / "tiro.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, domain TEXT, email_sender TEXT, source_type TEXT NOT NULL, is_vip BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, title TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, markdown_path TEXT NOT NULL)")
    conn.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL)")
    conn.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, entity_type TEXT NOT NULL, UNIQUE(name, entity_type))")
    conn.execute("INSERT INTO articles (title, slug, markdown_path) VALUES ('t', 's', 'f.md')")
    conn.commit()
    conn.close()

    from tiro.database import init_db, migrate_db

    init_db(db)      # must NOT raise (was: OperationalError no such column: uid)
    migrate_db(db)   # brings the legacy DB up: uid columns + backfill

    conn = get_connection(db)
    assert conn.execute("SELECT uid FROM articles").fetchone()[0]
    conn.close()
