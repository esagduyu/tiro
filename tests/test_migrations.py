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
