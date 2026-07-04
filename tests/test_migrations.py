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
