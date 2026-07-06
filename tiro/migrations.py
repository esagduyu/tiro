"""Versioned SQLite migrations.

PRAGMA user_version tracks the applied schema version. init_db() stamps
fresh databases at LATEST_VERSION (the base SCHEMA already includes every
migration's end state); run_migrations() walks pending versions on existing
databases, taking a file-copy backup of tiro.db first.

Writing a migration:
- append (N, "name", fn) to MIGRATIONS with N = previous max + 1
- fn(conn) must be IDEMPOTENT where cheap (column-adds check PRAGMA
  table_xinfo, not table_info — table_info hides VIRTUAL/STORED generated
  columns) because pre-framework libraries sit at user_version 0 with
  some later state already present
- update database.SCHEMA to match the end state for fresh installs
- init_db() creates the full schema only for FRESH databases (no
  `articles` table). Adding a NEW TABLE to SCHEMA therefore requires a
  matching idempotent CREATE TABLE IF NOT EXISTS migration here —
  existing databases never re-run SCHEMA.
"""

import logging
import shutil
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    # table_xinfo (not table_info) because table_info hides generated
    # (VIRTUAL/STORED) columns like display_date — table_info would report
    # False forever and re-run the ALTER TABLE every migration pass.
    return column in [r[1] for r in conn.execute(f"PRAGMA table_xinfo({table})").fetchall()]


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


def _m004_indexes(conn: sqlite3.Connection) -> None:
    # Ultra-legacy safety net: the generated column below references these
    # by name, so they must exist first (real-world pre-framework DBs have
    # always had them; only synthetic minimal test tables might not).
    if not _has_column(conn, "articles", "published_at"):
        conn.execute("ALTER TABLE articles ADD COLUMN published_at TIMESTAMP")
    if not _has_column(conn, "articles", "ingested_at"):
        # SQLite forbids a non-constant default (CURRENT_TIMESTAMP) on
        # ALTER TABLE ADD COLUMN; backfill existing rows explicitly instead.
        conn.execute("ALTER TABLE articles ADD COLUMN ingested_at TIMESTAMP")
        conn.execute(
            "UPDATE articles SET ingested_at = CURRENT_TIMESTAMP WHERE ingested_at IS NULL"
        )
    if not _has_column(conn, "articles", "display_date"):
        # VIRTUAL (not STORED): SQLite only allows VIRTUAL generated columns
        # via ALTER TABLE. The index below materializes it for sorting.
        conn.execute(
            "ALTER TABLE articles ADD COLUMN display_date TEXT "
            "GENERATED ALWAYS AS (COALESCE(published_at, ingested_at)) VIRTUAL"
        )
    if not _has_column(conn, "articles", "is_read"):
        conn.execute("ALTER TABLE articles ADD COLUMN is_read BOOLEAN DEFAULT FALSE")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_display_date ON articles(display_date DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_source_id ON articles(source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_is_read ON articles(is_read)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_vector_status ON articles(vector_status)")
    if _has_table(conn, "article_tags"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_article_tags_tag ON article_tags(tag_id)")
    if _has_table(conn, "article_entities"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_article_entities_entity ON article_entities(entity_id)"
        )
    if _has_table(conn, "article_relations"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_article_relations_related "
            "ON article_relations(related_article_id)"
        )
    if _has_table(conn, "sessions"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")


def canonical_key(name: str) -> str:
    """Whitespace-collapsed casefolded key for entity dedup ("OpenAI" == "openai").
    Deliberately conservative: exact-after-normalization only, no fuzzy matching."""
    return " ".join(name.split()).casefold()


def _m005_entity_canonical(conn: sqlite3.Connection) -> None:
    if not _has_table(conn, "entities") or not _has_table(conn, "article_entities"):
        return
    if not _has_column(conn, "entities", "canonical_key"):
        conn.execute("ALTER TABLE entities ADD COLUMN canonical_key TEXT")
    # database.py SCHEMA already carries this index for fresh installs, so on
    # a DB whose entities predate the merge (canonical_key NULL/stale) the
    # index may already exist and would block the row-by-row backfill below
    # from ever assigning two rows the same key. Drop and recreate after
    # merging — harmless no-op when the index isn't there yet.
    conn.execute("DROP INDEX IF EXISTS idx_entities_canonical")
    for row in conn.execute("SELECT id, name FROM entities WHERE canonical_key IS NULL").fetchall():
        conn.execute(
            "UPDATE entities SET canonical_key = ? WHERE id = ?",
            (canonical_key(row["name"]), row["id"]),
        )
    # Merge duplicates within each (entity_type, canonical_key): keep lowest id.
    dupes = conn.execute("""
        SELECT entity_type, canonical_key, MIN(id) AS keep_id
        FROM entities GROUP BY entity_type, canonical_key HAVING COUNT(*) > 1
    """).fetchall()
    for d in dupes:
        losers = [r["id"] for r in conn.execute(
            "SELECT id FROM entities WHERE entity_type = ? AND canonical_key = ? AND id != ?",
            (d["entity_type"], d["canonical_key"], d["keep_id"]),
        ).fetchall()]
        for loser in losers:
            conn.execute(
                "INSERT OR IGNORE INTO article_entities (article_id, entity_id) "
                "SELECT article_id, ? FROM article_entities WHERE entity_id = ?",
                (d["keep_id"], loser),
            )
            conn.execute("DELETE FROM article_entities WHERE entity_id = ?", (loser,))
            conn.execute("DELETE FROM entities WHERE id = ?", (loser,))
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_canonical "
        "ON entities(entity_type, canonical_key)"
    )


def _m006_phase0_tables(conn: sqlite3.Connection) -> None:
    """Phase-0 tables (sessions, api_tokens) for pre-auth legacy DBs.

    init_db() creates the full schema only for FRESH databases; every table
    added to SCHEMA after the original hackathon schema needs an idempotent
    migration like this one (see module docstring — this migration is the
    contract's first practice)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            token_hash TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")


def _m007_authors_views(conn: sqlite3.Connection) -> None:
    """Authors, article_authors, saved_views (Phase 1 M1.2 first commit).

    New-table contract second practice (see module docstring / _m006): tables
    created idempotently here AND mirrored in database.SCHEMA for fresh
    installs. Backfills authors from articles.author, deduping spellings by
    canonical_key (first-seen spelling wins)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS authors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT,
            name TEXT NOT NULL,
            canonical_key TEXT NOT NULL,
            is_vip BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_authors_canonical ON authors(canonical_key)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_authors_uid ON authors(uid)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS article_authors (
            article_id INTEGER REFERENCES articles(id),
            author_id INTEGER REFERENCES authors(id),
            PRIMARY KEY (article_id, author_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_article_authors_author ON article_authors(author_id)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS saved_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT,
            name TEXT NOT NULL,
            filter_json TEXT NOT NULL,
            sort_mode TEXT DEFAULT 'unread',
            position INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_saved_views_uid ON saved_views(uid)")

    # Ultra-legacy safety net: synthetic minimal test tables (predating even
    # the hackathon schema) may lack an author column entirely.
    if not _has_column(conn, "articles", "author"):
        return

    for row in conn.execute(
        "SELECT id, TRIM(author) AS author FROM articles"
        " WHERE author IS NOT NULL AND TRIM(author) != ''"
    ).fetchall():
        key = canonical_key(row["author"])
        existing = conn.execute(
            "SELECT id FROM authors WHERE canonical_key = ?", (key,)
        ).fetchone()
        if existing:
            author_id = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO authors (uid, name, canonical_key) VALUES (?, ?, ?)",
                (new_ulid(), row["author"], key),
            )
            author_id = cur.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO article_authors (article_id, author_id) VALUES (?, ?)",
            (row["id"], author_id),
        )


def _m008_wiki_tables(conn: sqlite3.Connection) -> None:
    """Wiki derived-index tables (Phase 1b W1 first commit): wiki_pages and
    wiki_page_articles.

    New-table contract third practice (see module docstring / _m006/_m007):
    tables only, no backfill — files-win reconcile owns population later and
    a fresh 008 DB has no wiki files yet."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wiki_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT,
            slug TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            entity_type TEXT,
            status TEXT DEFAULT 'fresh',
            source_count INTEGER DEFAULT 0,
            updated_at TIMESTAMP
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_pages_uid ON wiki_pages(uid)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wiki_page_articles (
            page_id INTEGER REFERENCES wiki_pages(id),
            article_id INTEGER REFERENCES articles(id),
            PRIMARY KEY (page_id, article_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wiki_page_articles_article ON wiki_page_articles(article_id)"
    )


def _m009_highlights_notes(conn: sqlite3.Connection) -> None:
    """Highlights + notes tables (Phase 2 M2.1 first commit): sidecar files
    are the source of truth (owned by later tasks); these are the derived
    SQLite index, same files-win pattern as wiki_pages (_m008).

    New-table contract fourth practice (see module docstring / _m006/_m007/
    _m008): tables only, no backfill — a fresh 009 DB has no highlights/notes
    sidecar files yet."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS highlights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT UNIQUE NOT NULL,
            article_id INTEGER NOT NULL REFERENCES articles(id),
            quote_text TEXT NOT NULL,
            prefix_context TEXT,
            suffix_context TEXT,
            text_position_start INTEGER,
            text_position_end INTEGER,
            content_hash TEXT,
            color TEXT NOT NULL DEFAULT 'yellow',
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_highlights_article ON highlights(article_id)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT UNIQUE NOT NULL,
            article_id INTEGER NOT NULL REFERENCES articles(id),
            highlight_id INTEGER REFERENCES highlights(id),
            body_markdown TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_article ON notes(article_id)")


def _m010_reading_sessions(conn: sqlite3.Connection) -> None:
    """Reading-session telemetry table (Phase 2 M2.3 first commit): one row
    per reader visit, opt-in (`reading_telemetry_enabled`, default False) and
    strictly local-only — feeds the future wiki-importance ranking signal
    (Decision #8). No sidecar file for this one (unlike wiki_pages/
    highlights/notes above) — sessions are ephemeral telemetry, not
    user-authored content, so SQLite is the only store.

    New-table contract fifth practice (see module docstring / _m006/_m007/
    _m008/_m009): table only, no backfill — a fresh 010 DB has no sessions
    yet."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reading_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT UNIQUE NOT NULL,
            article_id INTEGER NOT NULL REFERENCES articles(id),
            started_at TEXT,
            ended_at TEXT,
            max_scroll_pct INTEGER,
            active_seconds INTEGER,
            dwell_json TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reading_sessions_article"
        " ON reading_sessions(article_id)"
    )


MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "ingestion_method column", _m001_ingestion_method),
    (2, "vector_status column", _m002_vector_status),
    (3, "uid ULID columns on articles/entities/tags", _m003_uid_columns),
    (4, "display_date + hot-path indexes", _m004_indexes),
    (5, "entity canonical_key + duplicate merge", _m005_entity_canonical),
    (6, "phase-0 tables (sessions/api_tokens) for pre-auth DBs", _m006_phase0_tables),
    (7, "authors + article_authors + saved_views", _m007_authors_views),
    (8, "wiki derived-index tables", _m008_wiki_tables),
    (9, "highlights + notes tables", _m009_highlights_notes),
    (10, "reading_sessions telemetry table", _m010_reading_sessions),
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
