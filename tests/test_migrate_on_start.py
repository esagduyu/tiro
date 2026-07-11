"""Migrate-on-start hardening (spec D4): before a version-crossing migration on
server start / `tiro migrate`, take a full auto_backup snapshot and log a
prominent WARNING — best-effort (a failed snapshot warns and migration
proceeds; fresh/up-to-date installs skip the ceremony entirely).
"""

import logging

from fastapi.testclient import TestClient

from tiro.config import TiroConfig
from tiro.database import get_connection


def _build_library(tmp_path, _shared_embeddings) -> TiroConfig:
    """A real, fully-migrated library with one article (so it has real data)."""
    from tiro.database import init_db, migrate_db
    from tiro.vectorstore import init_vectorstore

    config = TiroConfig(library_path=str(tmp_path / "lib"))
    config.articles_dir.mkdir(parents=True, exist_ok=True)
    (config.library / "audio").mkdir(parents=True, exist_ok=True)
    init_db(config.db_path)
    migrate_db(config.db_path)
    init_vectorstore(config.chroma_dir, config.default_embedding_model)
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            "INSERT INTO articles (title, slug, markdown_path) VALUES ('A', 'a', 'a.md')"
        )
        conn.commit()
    finally:
        conn.close()
    return config


def _set_user_version(config: TiroConfig, v: int) -> None:
    conn = get_connection(config.db_path)
    try:
        conn.execute(f"PRAGMA user_version = {v}")
        conn.commit()
    finally:
        conn.close()


def _user_version(config: TiroConfig) -> int:
    conn = get_connection(config.db_path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# pre_migrate_snapshot unit behavior
# ---------------------------------------------------------------------------


def test_behind_nonfresh_takes_snapshot_and_warns(tmp_path, _shared_embeddings, monkeypatch, caplog):
    from tiro import migrations

    config = _build_library(tmp_path, _shared_embeddings)
    _set_user_version(config, 12)  # behind LATEST

    calls = {}

    def spy(cfg, reason):
        calls["reason"] = reason
        return tmp_path / "snap.tar.zst"

    monkeypatch.setattr("tiro.backup.auto_backup", spy)

    with caplog.at_level(logging.WARNING):
        result = migrations.pre_migrate_snapshot(config)

    assert calls["reason"] == "pre-migrate"
    assert result == str(tmp_path / "snap.tar.zst")
    assert any(
        f"Migrating library schema v12 -> v{migrations.LATEST_VERSION}" in r.message
        for r in caplog.records
    )


def test_fresh_install_skips_snapshot(tmp_path, _shared_embeddings, monkeypatch):
    from tiro import migrations

    config = _build_library(tmp_path, _shared_embeddings)  # stamped at LATEST

    called = []
    monkeypatch.setattr("tiro.backup.auto_backup", lambda *a, **k: called.append(1))

    assert migrations.pre_migrate_snapshot(config) is None
    assert not called


def test_up_to_date_skips_snapshot(tmp_path, _shared_embeddings, monkeypatch):
    from tiro import migrations

    config = _build_library(tmp_path, _shared_embeddings)
    _set_user_version(config, migrations.LATEST_VERSION)

    called = []
    monkeypatch.setattr("tiro.backup.auto_backup", lambda *a, **k: called.append(1))

    assert migrations.pre_migrate_snapshot(config) is None
    assert not called


def test_no_articles_table_skips_snapshot(tmp_path, _shared_embeddings, monkeypatch):
    from tiro import migrations

    # A minimal DB with NO articles table but a behind user_version.
    config = TiroConfig(library_path=str(tmp_path / "lib"))
    config.library.mkdir(parents=True, exist_ok=True)
    conn = get_connection(config.db_path)
    try:
        conn.execute("CREATE TABLE misc (id INTEGER)")
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
    finally:
        conn.close()

    called = []
    monkeypatch.setattr("tiro.backup.auto_backup", lambda *a, **k: called.append(1))

    assert migrations.pre_migrate_snapshot(config) is None
    assert not called


def test_missing_db_returns_none(tmp_path):
    from tiro import migrations

    config = TiroConfig(library_path=str(tmp_path / "nope"))
    assert migrations.pre_migrate_snapshot(config) is None


def test_snapshot_failure_warns_and_returns_none(tmp_path, _shared_embeddings, monkeypatch, caplog):
    from tiro import migrations

    config = _build_library(tmp_path, _shared_embeddings)
    _set_user_version(config, 12)
    monkeypatch.setattr("tiro.backup.auto_backup", lambda *a, **k: None)  # simulate failure

    with caplog.at_level(logging.WARNING):
        result = migrations.pre_migrate_snapshot(config)

    assert result is None
    assert any("FAILED" in r.message for r in caplog.records)


def test_backups_disabled_distinct_message(tmp_path, _shared_embeddings, monkeypatch, caplog):
    from tiro import migrations

    config = _build_library(tmp_path, _shared_embeddings)
    config.backup_auto_keep = 0  # backups disabled by config
    _set_user_version(config, 12)
    monkeypatch.setattr("tiro.backup.auto_backup", lambda *a, **k: None)

    with caplog.at_level(logging.WARNING):
        migrations.pre_migrate_snapshot(config)

    assert any("disabled" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Ordering + integration: snapshot fires BEFORE migrate_db in the lifespan
# ---------------------------------------------------------------------------


def test_lifespan_snapshots_before_migrating(tmp_path, _shared_embeddings, monkeypatch):
    from tiro import migrations
    from tiro.app import create_app

    config = _build_library(tmp_path, _shared_embeddings)
    _set_user_version(config, 12)  # behind → migration will run on start

    seen = {}

    def spy(cfg, reason):
        # Capture the schema version AT snapshot time: migrate_db hasn't run yet,
        # so it must still read 12 here (proving snapshot precedes migration).
        seen["reason"] = reason
        seen["version_at_backup"] = _user_version(cfg)
        return tmp_path / "snap.tar.zst"

    monkeypatch.setattr("tiro.backup.auto_backup", spy)

    app = create_app(config)
    with TestClient(app, base_url="http://localhost"):
        pass  # lifespan runs init_db → pre_migrate_snapshot → migrate_db

    assert seen["reason"] == "pre-migrate"
    assert seen["version_at_backup"] == 12
    # Migration completed after the snapshot.
    assert _user_version(config) == migrations.LATEST_VERSION
    # Article survived the migration.
    conn = get_connection(config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1
    finally:
        conn.close()


def test_fresh_lifespan_takes_no_snapshot(initialized_library, monkeypatch):
    from tiro.app import create_app

    called = []
    monkeypatch.setattr("tiro.backup.auto_backup", lambda *a, **k: called.append(1))

    app = create_app(initialized_library)
    with TestClient(app, base_url="http://localhost"):
        pass

    # A freshly-initialized library is at LATEST; no pre-migrate snapshot.
    assert not called
