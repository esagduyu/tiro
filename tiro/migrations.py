"""Versioned SQLite migrations.

PRAGMA user_version tracks the applied schema version. init_db() stamps
fresh databases at LATEST_VERSION (the base SCHEMA already includes every
migration's end state); run_migrations() walks pending versions on existing
databases, taking a file-copy backup of tiro.db first.

Writing a migration:
- append (N, "name", fn) to MIGRATIONS with N = previous max + 1
- fn(conn) must be IDEMPOTENT where cheap (column-adds check PRAGMA
  table_info) because pre-framework libraries sit at user_version 0 with
  some later state already present
- update database.SCHEMA to match the end state for fresh installs
"""

import logging
import shutil
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return column in [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _m001_ingestion_method(conn: sqlite3.Connection) -> None:
    if _has_column(conn, "articles", "ingestion_method"):
        return
    conn.execute("ALTER TABLE articles ADD COLUMN ingestion_method TEXT DEFAULT 'manual'")
    conn.execute("""
        UPDATE articles SET ingestion_method = 'email'
        WHERE source_id IN (SELECT id FROM sources WHERE source_type = 'email')
        AND ingestion_method = 'manual'
    """)


def _m002_vector_status(conn: sqlite3.Connection) -> None:
    if _has_column(conn, "articles", "vector_status"):
        return
    conn.execute("ALTER TABLE articles ADD COLUMN vector_status TEXT DEFAULT 'pending'")
    conn.execute("UPDATE articles SET vector_status = 'indexed' WHERE vector_status = 'pending'")


def new_ulid() -> str:
    """Stable external identity for rows (sortable, 26 chars). All new
    articles/entities/tags get one at insert; sync and the wiki key on it."""
    from ulid import ULID

    return str(ULID())


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _m003_uid_columns(conn: sqlite3.Connection) -> None:
    for table in ("articles", "entities", "tags"):
        if not _has_table(conn, table):
            continue
        if not _has_column(conn, table, "uid"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN uid TEXT")
        rows = conn.execute(f"SELECT rowid FROM {table} WHERE uid IS NULL").fetchall()
        for row in rows:
            conn.execute(f"UPDATE {table} SET uid = ? WHERE rowid = ?", (new_ulid(), row[0]))
        conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_uid ON {table}(uid)")


MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "ingestion_method column", _m001_ingestion_method),
    (2, "vector_status column", _m002_vector_status),
    (3, "uid ULID columns on articles/entities/tags", _m003_uid_columns),
]

LATEST_VERSION = max(v for v, _, _ in MIGRATIONS)


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def run_migrations(db_path: Path, *, backup: bool = True) -> list[str]:
    """Apply pending migrations. Returns list of applied '00N name' strings."""
    from tiro.database import get_connection

    conn = get_connection(db_path)
    try:
        current = schema_version(conn)
        pending = [(v, n, f) for v, n, f in sorted(MIGRATIONS) if v > current]
        if not pending:
            return []
        # Backup before touching anything: checkpoint WAL so the copy is complete.
        if backup:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            dest = db_path.with_name(f"{db_path.name}.pre-migrate-{ts}")
            shutil.copy2(db_path, dest)
            logger.info("Pre-migration backup: %s", dest)
        applied = []
        for version, name, fn in pending:
            fn(conn)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
            applied.append(f"{version:03d} {name}")
            logger.info("Migration %03d applied: %s", version, name)
        return applied
    finally:
        conn.close()
