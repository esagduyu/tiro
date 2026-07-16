"""Suggestions storage + migration 017 (Phase 6 K3)."""

SUGGESTIONS_COLUMNS = {
    "id", "uid", "persona", "kind", "payload_json",
    "citations_json", "created_at", "status",
}


def _table_columns(db_path, table):
    from tiro.database import get_connection

    conn = get_connection(db_path)
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_xinfo({table})")}
    finally:
        conn.close()


def test_fresh_install_has_suggestions(tmp_path):
    from tiro.database import init_db

    db = tmp_path / "fresh.db"
    init_db(db)
    assert _table_columns(db, "suggestions") == SUGGESTIONS_COLUMNS


def test_migration_017_upgrades_existing_db(tmp_path):
    from tiro.database import get_connection, init_db, migrate_db

    db = tmp_path / "old.db"
    init_db(db)
    conn = get_connection(db)
    try:
        conn.execute("DROP TABLE suggestions")   # simulate a pre-017 library
        conn.execute("PRAGMA user_version = 16")
        conn.commit()
    finally:
        conn.close()
    migrate_db(db)
    assert _table_columns(db, "suggestions") == SUGGESTIONS_COLUMNS
    migrate_db(db)                               # idempotent re-run
    assert _table_columns(db, "suggestions") == SUGGESTIONS_COLUMNS


def test_suggestions_is_migration_017():
    # K3 claimed exactly migration 017 (suggestions). Assert that specific
    # claim rather than "017 is newest" or "no gaps": migration 016 (sync S2)
    # lands on a concurrent branch and later milestones add more — the
    # durable invariant is the number, not the ceiling (7c2ca0b precedent;
    # transient 15->17 gap on this branch authorized by D17, coordinator
    # enforces merge order).
    from tiro.migrations import MIGRATIONS

    versions = [v for v, _, _ in MIGRATIONS]
    assert len(versions) == len(set(versions))        # no duplicate claims
    assert versions == sorted(versions)               # ordered
    by_version = {v: desc for v, desc, _ in MIGRATIONS}
    assert "suggestions" in by_version[17]


def test_personas_disabled_config_default(test_config):
    assert test_config.personas_disabled == []
