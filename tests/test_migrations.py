"""Migration framework: versioned, backed-up, idempotent over legacy DBs."""

from pathlib import Path

from tiro.database import get_connection, init_db
from tiro.migrations import LATEST_VERSION, canonical_key, run_migrations, schema_version


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


def test_legacy_db_gets_phase0_auth_tables(tmp_path):
    """A hackathon-era DB (predates auth entirely — no sessions/api_tokens
    tables) must come out of the app.py lifespan (init_db + migrate_db) able
    to actually authenticate: create_session/create_api_token must work
    against real tiro.auth, not just "the table exists" (was:
    OperationalError: no such table: sessions on first login)."""
    import sqlite3

    from tiro import auth

    db = tmp_path / "tiro.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "domain TEXT, email_sender TEXT, source_type TEXT NOT NULL, "
        "is_vip BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE articles (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, "
        "title TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, markdown_path TEXT NOT NULL)"
    )
    conn.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL)")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "entity_type TEXT NOT NULL, UNIQUE(name, entity_type))"
    )
    conn.execute("INSERT INTO articles (title, slug, markdown_path) VALUES ('t', 's', 'f.md')")
    conn.commit()
    conn.close()

    from tiro.database import init_db, migrate_db

    init_db(db)      # legacy DB already has `articles` -> no-op fresh-schema path
    migrate_db(db)   # app.py lifespan order

    session_token = auth.create_session(db)
    assert auth.validate_session(db, session_token)

    api_token = auth.create_api_token(db, "mcp")
    assert auth.validate_api_token(db, api_token)

    conn = get_connection(db)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0] == 1
    conn.close()


def test_display_date_and_indexes(tmp_path):
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'web')")
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path, published_at)"
        " VALUES ('01AAAAAAAAAAAAAAAAAAAAAAAA', 1, 't', 'sl', 'f.md', '2026-01-01')"
    )
    conn.commit()
    row = conn.execute("SELECT display_date FROM articles").fetchone()
    assert row[0] == "2026-01-01"
    index_names = {r[1] for r in conn.execute("PRAGMA index_list(articles)").fetchall()}
    assert "idx_articles_display_date" in index_names
    assert "idx_articles_source_id" in index_names
    conn.close()


def test_canonical_key_normalizes():
    assert canonical_key("  Open AI ") == "open ai"
    assert canonical_key("OpenAI") == "openai"
    assert canonical_key("Sam Altman") == "sam altman"  # nbsp collapses


def test_entity_merge_migration(tmp_path):
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'web')")
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
        " VALUES ('01AAAAAAAAAAAAAAAAAAAAAAAA', 1, 't', 'sl', 'f.md')"
    )
    # Two spellings of the same company, each linked to the article
    conn.execute("INSERT INTO entities (uid, name, entity_type) VALUES ('01B1', 'OpenAI', 'company')")
    conn.execute("INSERT INTO entities (uid, name, entity_type) VALUES ('01B2', 'openai', 'company')")
    conn.execute("INSERT INTO article_entities (article_id, entity_id) VALUES (1, 1)")
    conn.execute("INSERT INTO article_entities (article_id, entity_id) VALUES (1, 2)")
    conn.execute("UPDATE entities SET canonical_key = NULL")
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    run_migrations(db)

    conn = get_connection(db)
    rows = conn.execute("SELECT id, name FROM entities WHERE entity_type='company'").fetchall()
    assert len(rows) == 1  # merged; survivor is the lowest id ("OpenAI")
    links = conn.execute("SELECT COUNT(*) FROM article_entities").fetchone()[0]
    assert links == 1
    conn.close()


def test_wiki_dir_property(test_config):
    """config.wiki_dir resolves next to articles/, db, chroma (Phase 1b prep)."""
    assert test_config.wiki_dir == test_config.library / "wiki"


def test_wiki_dir_reserved(client, initialized_library):
    """Lifespan (run via the `client` fixture's TestClient context manager)
    creates wiki/ alongside the other library directories."""
    assert (initialized_library.library / "wiki").is_dir()


def test_m007_creates_author_and_view_tables(tmp_path):
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"authors", "article_authors", "saved_views"} <= names
    conn.close()


def test_m008_creates_wiki_tables(tmp_path):
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"wiki_pages", "wiki_page_articles"} <= names
    index_names = {r[1] for r in conn.execute("PRAGMA index_list(wiki_pages)").fetchall()}
    assert "idx_wiki_pages_uid" in index_names
    index_names = {r[1] for r in conn.execute("PRAGMA index_list(wiki_page_articles)").fetchall()}
    assert "idx_wiki_page_articles_article" in index_names
    conn.close()


def test_m008_legacy_db_at_version_7_gains_wiki_tables(tmp_path):
    """A DB stamped at user_version 7 (pre-wiki) must gain wiki_pages and
    wiki_page_articles via run_migrations, with no backfill (files-win
    reconcile owns population later; a fresh 008 DB has no wiki files)."""
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    conn.execute("DROP TABLE wiki_page_articles")
    conn.execute("DROP TABLE wiki_pages")
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    conn.close()

    applied = run_migrations(db)
    assert any("wiki" in a for a in applied)

    conn = get_connection(db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"wiki_pages", "wiki_page_articles"} <= names
    assert conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0] == 0
    conn.close()


def test_m008_wiki_migration_is_idempotent(tmp_path):
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    conn.close()

    run_migrations(db)
    assert run_migrations(db) == []  # re-run: nothing pending

    conn = get_connection(db)
    conn.execute(
        "INSERT INTO wiki_pages (slug, kind, title) VALUES ('slug-1', 'entity', 'Title')"
    )
    conn.commit()
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    conn.close()

    run_migrations(db)  # re-running the migration must not raise or wipe data
    conn = get_connection(db)
    assert conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0] == 1
    conn.close()


def test_m007_backfills_authors_from_articles(tmp_path):
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'web')")
    for i, author in enumerate(["Matt Levine", "matt levine", "  Matt Levine ", None, ""]):
        conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path, author)"
            " VALUES (?, 1, ?, ?, ?, ?)",
            (f"01A{i}".ljust(26, "0"), f"t{i}", f"s{i}", f"f{i}.md", author),
        )
    conn.execute("DELETE FROM authors")
    conn.execute("DELETE FROM article_authors")
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    conn.close()

    run_migrations(db)

    conn = get_connection(db)
    authors = conn.execute("SELECT name, canonical_key FROM authors").fetchall()
    assert len(authors) == 1  # three spellings collapse to one
    assert authors[0]["canonical_key"] == "matt levine"
    links = conn.execute("SELECT COUNT(*) FROM article_authors").fetchone()[0]
    assert links == 3  # null/empty authors produce no rows
    conn.close()


def test_m009_creates_highlights_notes_tables(tmp_path):
    """Fresh DB path: init_db() runs SCHEMA directly (no migration involved)."""
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"highlights", "notes"} <= names
    index_names = {r[1] for r in conn.execute("PRAGMA index_list(highlights)").fetchall()}
    assert "idx_highlights_article" in index_names
    index_names = {r[1] for r in conn.execute("PRAGMA index_list(notes)").fetchall()}
    assert "idx_notes_article" in index_names
    conn.close()


def test_m009_legacy_db_at_version_8_gains_highlights_notes_tables(tmp_path):
    """A DB stamped at user_version 8 (pre-highlights) must gain highlights
    and notes via run_migrations, with no backfill (sidecar files are the
    source of truth; a fresh 009 DB has no sidecar files yet)."""
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    conn.execute("DROP TABLE notes")
    conn.execute("DROP TABLE highlights")
    conn.execute("PRAGMA user_version = 8")
    conn.commit()
    conn.close()

    applied = run_migrations(db)
    assert any("highlights" in a for a in applied)

    conn = get_connection(db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"highlights", "notes"} <= names
    assert conn.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0
    conn.close()


def test_m009_highlights_notes_migration_is_idempotent(tmp_path):
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    conn.execute("PRAGMA user_version = 8")
    conn.commit()
    conn.close()

    run_migrations(db)
    assert run_migrations(db) == []  # re-run: nothing pending

    conn = get_connection(db)
    conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'web')")
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
        " VALUES ('01AAAAAAAAAAAAAAAAAAAAAAAA', 1, 't', 'sl', 'f.md')"
    )
    conn.execute(
        "INSERT INTO highlights (uid, article_id, quote_text) VALUES ('01H1', 1, 'hi')"
    )
    conn.commit()
    conn.execute("PRAGMA user_version = 8")
    conn.commit()
    conn.close()

    run_migrations(db)  # re-running the migration must not raise or wipe data
    conn = get_connection(db)
    assert conn.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 1
    conn.close()


def test_m009_fresh_schema_and_legacy_migration_produce_identical_tables(tmp_path):
    """Cross-path schema equality (Finding 2): the SCHEMA path (fresh DB) and
    the migrate_db/_m009 path (legacy DB upgraded from user_version 8) are
    two independently-maintained copies of the same CREATE TABLE DDL — this
    proves they actually stay in sync, not just that both produce tables
    named the same thing. Compares PRAGMA table_xinfo (column name/type/
    notnull/default — the fields that matter for schema equality; cid/pk/
    hidden are position/quirk metadata, not compared) and each table's index
    list for both `highlights` and `notes`."""
    fresh_db = tmp_path / "fresh.db"
    init_db(fresh_db)

    legacy_db = tmp_path / "legacy.db"
    init_db(legacy_db)
    conn = get_connection(legacy_db)
    conn.execute("DROP TABLE notes")
    conn.execute("DROP TABLE highlights")
    conn.execute("PRAGMA user_version = 8")
    conn.commit()
    conn.close()
    run_migrations(legacy_db)

    def xinfo(db_path, table):
        conn = get_connection(db_path)
        try:
            rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
            # (name, type, notnull, dflt_value) per column, order-independent.
            return {(r["name"], r["type"], r["notnull"], r["dflt_value"]) for r in rows}
        finally:
            conn.close()

    def index_list(db_path, table):
        conn = get_connection(db_path)
        try:
            rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
            return {(r["name"], r["unique"]) for r in rows}
        finally:
            conn.close()

    for table in ("highlights", "notes"):
        assert xinfo(fresh_db, table) == xinfo(legacy_db, table), (
            f"{table} column schema diverges between SCHEMA and migration paths"
        )
        assert index_list(fresh_db, table) == index_list(legacy_db, table), (
            f"{table} index list diverges between SCHEMA and migration paths"
        )


def test_m009_highlights_notes_schema_columns(tmp_path):
    """Column-level check against the brief's exact schema (both convergence
    paths use the same CREATE TABLE, so checking the fresh path suffices)."""
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    highlight_cols = {r[1] for r in conn.execute("PRAGMA table_info(highlights)").fetchall()}
    assert highlight_cols == {
        "id", "uid", "article_id", "quote_text", "prefix_context", "suffix_context",
        "text_position_start", "text_position_end", "content_hash", "color",
        "created_at", "updated_at",
    }
    note_cols = {r[1] for r in conn.execute("PRAGMA table_info(notes)").fetchall()}
    assert note_cols == {
        "id", "uid", "article_id", "highlight_id", "body_markdown",
        "created_at", "updated_at",
    }
    conn.close()


def test_m010_fresh_schema_has_reading_sessions_table(tmp_path):
    """Fresh DB path: init_db() runs SCHEMA directly (no migration involved)."""
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "reading_sessions" in names
    index_names = {r[1] for r in conn.execute("PRAGMA index_list(reading_sessions)").fetchall()}
    assert "idx_reading_sessions_article" in index_names
    conn.close()


def test_m010_legacy_db_at_version_9_gains_reading_sessions_table(tmp_path):
    """A DB stamped at user_version 9 (pre-telemetry) must gain
    reading_sessions via run_migrations, with no backfill (fresh telemetry
    tables start empty)."""
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    conn.execute("DROP TABLE reading_sessions")
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    conn.close()

    applied = run_migrations(db)
    assert any("reading_sessions" in a for a in applied)

    conn = get_connection(db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "reading_sessions" in names
    assert conn.execute("SELECT COUNT(*) FROM reading_sessions").fetchone()[0] == 0
    conn.close()


def test_m010_reading_sessions_migration_is_idempotent(tmp_path):
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    conn.close()

    run_migrations(db)
    assert run_migrations(db) == []  # re-run: nothing pending

    conn = get_connection(db)
    conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'web')")
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
        " VALUES ('01AAAAAAAAAAAAAAAAAAAAAAAA', 1, 't', 'sl', 'f.md')"
    )
    conn.execute(
        "INSERT INTO reading_sessions (uid, article_id) VALUES ('01S1', 1)"
    )
    conn.commit()
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    conn.close()

    run_migrations(db)  # re-running the migration must not raise or wipe data
    conn = get_connection(db)
    assert conn.execute("SELECT COUNT(*) FROM reading_sessions").fetchone()[0] == 1
    conn.close()


def test_m010_fresh_schema_and_legacy_migration_produce_identical_tables(tmp_path):
    """Cross-path schema equality: the SCHEMA path (fresh DB) and the
    migrate_db/_m010 path (legacy DB upgraded from user_version 9) are two
    independently-maintained copies of the same CREATE TABLE DDL."""
    fresh_db = tmp_path / "fresh.db"
    init_db(fresh_db)

    legacy_db = tmp_path / "legacy.db"
    init_db(legacy_db)
    conn = get_connection(legacy_db)
    conn.execute("DROP TABLE reading_sessions")
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    conn.close()
    run_migrations(legacy_db)

    def xinfo(db_path, table):
        conn = get_connection(db_path)
        try:
            rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
            return {(r["name"], r["type"], r["notnull"], r["dflt_value"]) for r in rows}
        finally:
            conn.close()

    def index_list(db_path, table):
        conn = get_connection(db_path)
        try:
            rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
            return {(r["name"], r["unique"]) for r in rows}
        finally:
            conn.close()

    assert xinfo(fresh_db, "reading_sessions") == xinfo(legacy_db, "reading_sessions")
    assert index_list(fresh_db, "reading_sessions") == index_list(legacy_db, "reading_sessions")


def test_m010_reading_sessions_schema_columns(tmp_path):
    """Column-level check against the brief's exact schema (both convergence
    paths use the same CREATE TABLE, so checking the fresh path suffices)."""
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reading_sessions)").fetchall()}
    assert cols == {
        "id", "uid", "article_id", "started_at", "ended_at",
        "max_scroll_pct", "active_seconds", "dwell_json",
    }
    conn.close()


def _rebuild_articles_without_snooze(conn) -> None:
    """Simulate a real pre-M3.0 (version <= 10) `articles` table honestly:
    rebuild without `snoozed_until`/`display_date`'s newer generated form
    intact (display_date already existed pre-M3.0, just not snoozed_until),
    then restore the indexes a real version-10 DB would already have (from
    _m003_uid_columns/_m004_indexes, which won't re-run once stamped at
    version 10) — otherwise this simulation would understate the legacy
    DB's real index set and the cross-path equality test would compare
    apples to oranges."""
    conn.execute("ALTER TABLE articles RENAME TO articles_old")
    conn.execute("""
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT,
            source_id INTEGER REFERENCES sources(id),
            title TEXT NOT NULL,
            author TEXT,
            url TEXT,
            slug TEXT UNIQUE NOT NULL,
            markdown_path TEXT NOT NULL,
            summary TEXT,
            word_count INTEGER,
            reading_time_min INTEGER,
            published_at TIMESTAMP,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_read BOOLEAN DEFAULT FALSE,
            rating INTEGER,
            opened_count INTEGER DEFAULT 0,
            ai_tier TEXT,
            relevance_weight REAL DEFAULT 1.0,
            ingenuity_analysis TEXT,
            ingestion_method TEXT DEFAULT 'manual',
            vector_status TEXT DEFAULT 'pending',
            display_date TEXT GENERATED ALWAYS AS (COALESCE(published_at, ingested_at)) VIRTUAL
        )
    """)
    conn.execute("""
        INSERT INTO articles (id, uid, source_id, title, author, url, slug,
            markdown_path, summary, word_count, reading_time_min, published_at,
            ingested_at, is_read, rating, opened_count, ai_tier,
            relevance_weight, ingenuity_analysis, ingestion_method, vector_status)
        SELECT id, uid, source_id, title, author, url, slug,
            markdown_path, summary, word_count, reading_time_min, published_at,
            ingested_at, is_read, rating, opened_count, ai_tier,
            relevance_weight, ingenuity_analysis, ingestion_method, vector_status
        FROM articles_old
    """)
    conn.execute("DROP TABLE articles_old")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_uid ON articles(uid)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_display_date ON articles(display_date DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_source_id ON articles(source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_is_read ON articles(is_read)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_vector_status ON articles(vector_status)")


def test_m011_fresh_schema_has_snooze_column_and_login_tokens_table(tmp_path):
    """Fresh DB path: init_db() runs SCHEMA directly (no migration involved)."""
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    assert "snoozed_until" in {
        r[1] for r in conn.execute("PRAGMA table_xinfo(articles)").fetchall()
    }
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "login_tokens" in names
    index_names = {r[1] for r in conn.execute("PRAGMA index_list(login_tokens)").fetchall()}
    assert "idx_login_tokens_expires" in index_names
    conn.close()


def test_m011_legacy_db_at_version_10_gains_snooze_and_login_tokens(tmp_path):
    """A DB stamped at user_version 10 (pre-M3.0) must gain snoozed_until
    and login_tokens via run_migrations, with no backfill (no article is
    snoozed and no login token exists yet on a legacy DB either)."""
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    conn.execute("DROP TABLE login_tokens")
    # SQLite can't drop a column pre-3.35 cleanly via simple DDL in this
    # test harness, and _has_column must treat a version-10 DB as lacking
    # it regardless — rebuild articles without snoozed_until to simulate a
    # real pre-M3.0 DB honestly instead of trusting DROP COLUMN support.
    _rebuild_articles_without_snooze(conn)
    conn.execute("PRAGMA user_version = 10")
    conn.commit()
    conn.close()

    applied = run_migrations(db)
    assert any("snooze_and_login_tokens" in a for a in applied)

    conn = get_connection(db)
    assert "snoozed_until" in {
        r[1] for r in conn.execute("PRAGMA table_xinfo(articles)").fetchall()
    }
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "login_tokens" in names
    assert conn.execute("SELECT COUNT(*) FROM login_tokens").fetchone()[0] == 0
    conn.close()


def test_m011_migration_is_idempotent(tmp_path):
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    conn.execute("PRAGMA user_version = 10")
    conn.commit()
    conn.close()

    run_migrations(db)
    assert run_migrations(db) == []  # re-run: nothing pending

    conn = get_connection(db)
    conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'web')")
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path, snoozed_until)"
        " VALUES ('01AAAAAAAAAAAAAAAAAAAAAAAA', 1, 't', 'sl', 'f.md', '2099-01-01 00:00:00')"
    )
    conn.execute(
        "INSERT INTO login_tokens (token_hash, created_at, expires_at)"
        " VALUES ('h1', '2026-01-01 00:00:00', '2026-01-01 00:05:00')"
    )
    conn.commit()
    conn.execute("PRAGMA user_version = 10")
    conn.commit()
    conn.close()

    run_migrations(db)  # re-running the migration must not raise or wipe data
    conn = get_connection(db)
    assert conn.execute("SELECT snoozed_until FROM articles").fetchone()[0] == "2099-01-01 00:00:00"
    assert conn.execute("SELECT COUNT(*) FROM login_tokens").fetchone()[0] == 1
    conn.close()


def test_m011_fresh_schema_and_legacy_migration_produce_identical_tables(tmp_path):
    """Cross-path schema equality: the SCHEMA path (fresh DB) and the
    migrate_db/_m011 path (legacy DB upgraded from user_version 10, articles
    table rebuilt without snoozed_until to honestly simulate a pre-M3.0 DB)
    are two independently-maintained copies of the same end state."""
    fresh_db = tmp_path / "fresh.db"
    init_db(fresh_db)

    legacy_db = tmp_path / "legacy.db"
    init_db(legacy_db)
    conn = get_connection(legacy_db)
    conn.execute("DROP TABLE login_tokens")
    _rebuild_articles_without_snooze(conn)
    conn.execute("PRAGMA user_version = 10")
    conn.commit()
    conn.close()
    run_migrations(legacy_db)

    def xinfo(db_path, table):
        conn = get_connection(db_path)
        try:
            rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
            return {(r["name"], r["type"], r["notnull"], r["dflt_value"]) for r in rows}
        finally:
            conn.close()

    def index_list(db_path, table):
        conn = get_connection(db_path)
        try:
            rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
            return {(r["name"], r["unique"]) for r in rows}
        finally:
            conn.close()

    for table in ("articles", "login_tokens"):
        assert xinfo(fresh_db, table) == xinfo(legacy_db, table), (
            f"{table} column schema diverges between SCHEMA and migration paths"
        )
        assert index_list(fresh_db, table) == index_list(legacy_db, table), (
            f"{table} index list diverges between SCHEMA and migration paths"
        )


def test_m011_login_tokens_schema_columns(tmp_path):
    """Column-level check against the brief's exact schema (both convergence
    paths use the same CREATE TABLE, so checking the fresh path suffices)."""
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(login_tokens)").fetchall()}
    assert cols == {"id", "token_hash", "created_at", "expires_at", "used_at"}
    conn.close()


def test_m013_fresh_schema_has_feeds_tables(tmp_path):
    """Fresh DB path: init_db() runs SCHEMA directly (no migration involved)."""
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"feeds", "feed_entries"} <= names
    index_names = {r[1] for r in conn.execute("PRAGMA index_list(feed_entries)").fetchall()}
    assert "idx_feed_entries_article" in index_names
    conn.close()


def test_m013_legacy_db_at_version_12_gains_feeds_tables(tmp_path):
    """A DB stamped at user_version 12 (pre-RSS) must gain feeds and
    feed_entries via run_migrations, with no backfill (a fresh 013 DB has no
    feeds subscribed yet)."""
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    conn.execute("DROP TABLE feed_entries")
    conn.execute("DROP TABLE feeds")
    conn.execute("PRAGMA user_version = 12")
    conn.commit()
    conn.close()

    applied = run_migrations(db)
    assert any("feeds" in a for a in applied)

    conn = get_connection(db)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"feeds", "feed_entries"} <= names
    assert conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM feed_entries").fetchone()[0] == 0
    conn.close()


def test_m013_feeds_migration_is_idempotent(tmp_path):
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    conn.execute("PRAGMA user_version = 12")
    conn.commit()
    conn.close()

    run_migrations(db)
    assert run_migrations(db) == []  # re-run: nothing pending

    conn = get_connection(db)
    conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'rss')")
    conn.execute(
        "INSERT INTO feeds (uid, url, title, source_id) VALUES ('01F1', 'https://x/rss', 'X', 1)"
    )
    conn.execute("INSERT INTO feed_entries (feed_id, guid) VALUES (1, 'g1')")
    conn.commit()
    conn.execute("PRAGMA user_version = 12")
    conn.commit()
    conn.close()

    run_migrations(db)  # re-running the migration must not raise or wipe data
    conn = get_connection(db)
    assert conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM feed_entries").fetchone()[0] == 1
    conn.close()


def test_m013_fresh_schema_and_legacy_migration_produce_identical_tables(tmp_path):
    """Cross-path schema equality: the SCHEMA path (fresh DB) and the
    migrate_db/_m013 path (legacy DB upgraded from user_version 12) are two
    independently-maintained copies of the same CREATE TABLE DDL."""
    fresh_db = tmp_path / "fresh.db"
    init_db(fresh_db)

    legacy_db = tmp_path / "legacy.db"
    init_db(legacy_db)
    conn = get_connection(legacy_db)
    conn.execute("DROP TABLE feed_entries")
    conn.execute("DROP TABLE feeds")
    conn.execute("PRAGMA user_version = 12")
    conn.commit()
    conn.close()
    run_migrations(legacy_db)

    def xinfo(db_path, table):
        conn = get_connection(db_path)
        try:
            rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
            return {(r["name"], r["type"], r["notnull"], r["dflt_value"]) for r in rows}
        finally:
            conn.close()

    def index_list(db_path, table):
        conn = get_connection(db_path)
        try:
            rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
            return {(r["name"], r["unique"]) for r in rows}
        finally:
            conn.close()

    for table in ("feeds", "feed_entries"):
        assert xinfo(fresh_db, table) == xinfo(legacy_db, table), (
            f"{table} column schema diverges between SCHEMA and migration paths"
        )
        assert index_list(fresh_db, table) == index_list(legacy_db, table), (
            f"{table} index list diverges between SCHEMA and migration paths"
        )


def test_m013_feeds_schema_columns(tmp_path):
    """Column-level check against the spec D2 exact schema (both convergence
    paths use the same CREATE TABLE, so checking the fresh path suffices)."""
    db = tmp_path / "tiro.db"
    init_db(db)
    conn = get_connection(db)
    feed_cols = {r[1] for r in conn.execute("PRAGMA table_info(feeds)").fetchall()}
    assert feed_cols == {
        "id", "uid", "url", "title", "site_url", "folder", "source_id",
        "fetch_interval_minutes", "status", "error_count", "last_error",
        "last_fetched_at", "last_etag", "last_modified", "created_at",
    }
    entry_cols = {r[1] for r in conn.execute("PRAGMA table_info(feed_entries)").fetchall()}
    assert entry_cols == {"id", "feed_id", "guid", "article_id", "first_seen_at"}
    conn.close()
