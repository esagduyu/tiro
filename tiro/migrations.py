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


def _m011_snooze_and_login_tokens(conn: sqlite3.Connection) -> None:
    """Snooze column + login_tokens table (Phase 3 M3.0 Task 1, first
    migration of the milestone): `articles.snoozed_until` backs inbox
    snooze/triage (tiro/snooze.py computes values, tiro/queries.py's
    `include_snoozed` builder param scopes visibility); `login_tokens` is
    QR-login's one-time-token table, added here since M3.0's plan bundles
    it with this task's migration even though QR login itself lands in a
    later task (T2) — see the M3.0 plan's Decisions of record.

    New-table contract sixth practice (see module docstring / _m006..
    _m010): the table is created with no backfill (a fresh 011 DB has no
    login tokens yet), same as the column add (no existing article is
    snoozed)."""
    if not _has_column(conn, "articles", "snoozed_until"):
        conn.execute("ALTER TABLE articles ADD COLUMN snoozed_until TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS login_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT UNIQUE NOT NULL,
            created_at TEXT,
            expires_at TEXT,
            used_at TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_tokens_expires ON login_tokens(expires_at)"
    )


def _m012_device_pair_codes(conn: sqlite3.Connection) -> None:
    """device_pair_codes table (M-iOS Task 1): one-time codes the native iOS
    client exchanges for a long-lived API token via POST /api/auth/pair.

    Mirrors login_tokens (migration 011) exactly — same one-time-code shape
    (sha256-only storage, 15-min TTL, atomic single-use consume in
    tiro/auth.py), differing only in what redemption mints: login_tokens mint
    a *session cookie* for a browser, device_pair_codes mint an *api_tokens*
    row for the app. No backfill (a fresh 012 DB has no pair codes yet), same
    as 011's login_tokens create."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS device_pair_codes (
            id INTEGER PRIMARY KEY,
            code_hash TEXT NOT NULL UNIQUE,
            label TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_device_pair_codes_expires "
        "ON device_pair_codes(expires_at)"
    )


def _m013_feeds_tables(conn: sqlite3.Connection) -> None:
    """feeds + feed_entries tables (Phase 4 M4.0 first commit): recurring
    RSS/Atom ingestion. `feeds` holds subscriptions + per-feed fetch state
    (etag/last-modified conditional-GET validators, backoff error_count);
    `feed_entries` is a dedup LEDGER (not a content store) keyed on
    (feed_id, guid) — a row survives its article's deletion (article_id
    nulled by lifecycle.delete_article) so a deleted article is never
    resurrected by the next poll.

    New-table contract seventh practice (see module docstring / _m006..
    _m012): tables + index only, no backfill — a fresh 013 DB has no feeds
    subscribed yet."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT UNIQUE,
            url TEXT UNIQUE NOT NULL,
            title TEXT,
            site_url TEXT,
            folder TEXT,
            source_id INTEGER REFERENCES sources(id),
            fetch_interval_minutes INTEGER NOT NULL DEFAULT 60,
            status TEXT NOT NULL DEFAULT 'active',
            error_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            last_fetched_at TEXT,
            last_etag TEXT,
            last_modified TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feed_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_id INTEGER NOT NULL REFERENCES feeds(id),
            guid TEXT NOT NULL,
            article_id INTEGER,
            first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (feed_id, guid)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_feed_entries_article ON feed_entries(article_id)"
    )


def _m014_agent_runs(conn: sqlite3.Connection) -> None:
    """Phase 6 K1: agent_runs — the queryable index over trace files
    ({library}/agents/traces/{run_uid}.jsonl). Rows are kept forever
    (small); trace files are pruned by retention config. Columns FROZEN
    from the agent-runtime spec §2."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_uid TEXT UNIQUE NOT NULL,
            agent_name TEXT NOT NULL,
            agent_version TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            provider TEXT,
            model TEXT,
            input_json TEXT,
            output_json TEXT,
            citations_json TEXT,
            tokens_in INTEGER,
            tokens_out INTEGER,
            cost_usd REAL,
            error TEXT,
            replay_of TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_runs_name "
        "ON agent_runs(agent_name, started_at)"
    )


def _m015_sync_columns(conn: sqlite3.Connection) -> None:
    """Sync-engine S1 change-detection columns (weekend campaign: number 015
    is PRE-ASSIGNED; 014 is reserved for the agent track's agent_runs table
    and may be absent on this branch — the version gap is tolerated).

    - articles.body_hash: sha256 of the markdown BODY (frontmatter stripped,
      tiro/anchors.py content_hash semantics — the same text the annotations
      API hashes). Backfilled from disk where readable; missing/unparseable
      file -> NULL (the reconcile pass lazily adopts a hash for NULL rows
      without treating them as external edits).
    - articles.meta_updated_at: LWW timestamp for the per-field meta merge
      (spec §3/§4), bumped by the rate/read/snooze routes. Backfilled NULL
      ("never meta-modified").
    - sources.uid: ULID sync identity (spec §3 — sources lacked uids).
      Backfilled here; creation sites stamp it for new rows from S1 on.

    The disk backfill locates the library via PRAGMA database_list: tiro.db
    always lives at {library}/tiro.db with articles/ as a sibling
    (config.articles_dir invariant, unchanged by Phase 5's tiro/paths.py).
    Migration fns only receive a connection, hence the pragma. A pathless
    (:memory:) DB skips the backfill.
    """
    if not _has_column(conn, "articles", "body_hash"):
        conn.execute("ALTER TABLE articles ADD COLUMN body_hash TEXT")
    if not _has_column(conn, "articles", "meta_updated_at"):
        conn.execute("ALTER TABLE articles ADD COLUMN meta_updated_at TEXT")
    if not _has_column(conn, "sources", "uid"):
        conn.execute("ALTER TABLE sources ADD COLUMN uid TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_uid ON sources(uid)")
    for row in conn.execute("SELECT id FROM sources WHERE uid IS NULL").fetchall():
        conn.execute("UPDATE sources SET uid = ? WHERE id = ?", (new_ulid(), row["id"]))

    row = conn.execute("PRAGMA database_list").fetchone()
    db_file = row["file"] if row else None
    if not db_file:
        return
    articles_dir = Path(db_file).resolve().parent / "articles"
    if not articles_dir.exists():
        return

    import frontmatter

    from tiro.anchors import content_hash

    for art in conn.execute(
        "SELECT id, markdown_path FROM articles WHERE body_hash IS NULL"
    ).fetchall():
        path = articles_dir / Path(art["markdown_path"]).name
        try:
            body = frontmatter.load(str(path)).content
        except Exception as e:  # missing file, bad YAML, decode error: skip, stay NULL
            logger.debug("body_hash backfill skipped for %s: %s", path, e)
            continue
        conn.execute(
            "UPDATE articles SET body_hash = ? WHERE id = ?",
            (content_hash(body), art["id"]),
        )


def _m016_sync_shadow(conn: sqlite3.Connection) -> None:
    """Sync-engine S2 shadow store (weekend campaign: number 016 is
    PRE-ASSIGNED; 014 is the agent track's — the framework tolerates
    version gaps generally, though both 014 and 015 are present here).
    sync_shadow persists what THIS device last synced, one row per manifest
    entry: uid -> hash/fields/hlc (spec §3's shadow manifest, in SQLite not
    JSON per spec §10 scale note). Rebuildable — losing it means a full
    re-diff, never data loss. Rows with deleted_at set are tombstones
    (TTL-purged by tiro/sync/manifest.py::expire_tombstones);
    kind='alias' rows persist uid dedupe mappings (exempt from TTL).
    sync_state (device registry/watermarks) is deliberately NOT here — S5.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sync_shadow (
            kind TEXT NOT NULL,
            uid TEXT NOT NULL,
            hash TEXT,
            fields_json TEXT NOT NULL DEFAULT '{}',
            hlc TEXT,
            deleted_at TEXT,
            PRIMARY KEY (kind, uid)
        )"""
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
    (11, "snooze_and_login_tokens", _m011_snooze_and_login_tokens),
    (12, "device_pair_codes table", _m012_device_pair_codes),
    (13, "feeds + feed_entries tables", _m013_feeds_tables),
    (14, "agent_runs table", _m014_agent_runs),
    (15, "sync S1 change-detection columns (body_hash/meta_updated_at/sources.uid)", _m015_sync_columns),
    (16, "sync S2 shadow store (sync_shadow)", _m016_sync_shadow),
]

LATEST_VERSION = max(v for v, _, _ in MIGRATIONS)


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def pre_migrate_snapshot(config) -> str | None:
    """Full-library snapshot before a version-crossing migration (spec D4).

    Called from the server-start lifespan and `tiro migrate` BEFORE
    `run_migrations`. If the DB is behind `LATEST_VERSION` AND already holds
    real data (an `articles` table — a fresh install is stamped at
    `LATEST_VERSION` on creation and has nothing to snapshot), take an
    `auto_backup(config, "pre-migrate")` snapshot and log a prominent WARNING
    naming the version jump + snapshot path.

    Best-effort by design: a failed snapshot logs a WARNING and returns None —
    the caller proceeds to migrate regardless, because refusing to start the
    server (or run the CLI) over a backup hiccup is worse, and
    `run_migrations` still takes its own pre-migrate `tiro.db` file-copy no
    matter what. Returns the snapshot path as a string, or None (fresh /
    up-to-date / snapshot failed). Never raises.

    Note: `auto_backup` returns None BOTH when a snapshot fails AND when
    backups are disabled by config (`backup_auto_keep <= 0`); the two are
    distinguished in the logged message so the failure path isn't
    misread as an error when the user has simply turned auto-backups off.
    """
    from tiro.backup import auto_backup
    from tiro.database import get_connection

    db_path = config.db_path
    if not db_path.exists():
        return None
    conn = get_connection(db_path)
    try:
        current = schema_version(conn)
        has_articles = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='articles'"
            ).fetchone()
            is not None
        )
    finally:
        conn.close()

    if current >= LATEST_VERSION or not has_articles:
        return None

    snapshot = auto_backup(config, "pre-migrate")
    if snapshot is None:
        if getattr(config, "backup_auto_keep", 0) <= 0:
            logger.warning(
                "Migrating library schema v%d -> v%d: auto-backups are disabled "
                "(backup_auto_keep=0), so NO pre-migrate snapshot was taken. "
                "The tiro.db file-copy backup inside run_migrations still applies. "
                "Proceeding with migration.",
                current, LATEST_VERSION,
            )
        else:
            logger.warning(
                "Migrating library schema v%d -> v%d: pre-migrate snapshot FAILED "
                "— proceeding with migration anyway (the tiro.db file-copy backup "
                "inside run_migrations still applies).",
                current, LATEST_VERSION,
            )
        return None

    logger.warning(
        "Migrating library schema v%d -> v%d (snapshot: %s)",
        current, LATEST_VERSION, snapshot,
    )
    return str(snapshot)


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
