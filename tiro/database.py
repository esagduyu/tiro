"""SQLite database initialization and helpers for Tiro."""

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
-- Sources (domains, newsletter senders)
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    domain TEXT,
    email_sender TEXT,
    source_type TEXT NOT NULL,
    is_vip BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Articles (core content metadata)
CREATE TABLE IF NOT EXISTS articles (
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
);

-- Tags (extracted topics)
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS article_tags (
    article_id INTEGER REFERENCES articles(id),
    tag_id INTEGER REFERENCES tags(id),
    PRIMARY KEY (article_id, tag_id)
);

-- Named entities (people, companies, orgs)
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    canonical_key TEXT,
    UNIQUE(name, entity_type)
);

CREATE TABLE IF NOT EXISTS article_entities (
    article_id INTEGER REFERENCES articles(id),
    entity_id INTEGER REFERENCES entities(id),
    PRIMARY KEY (article_id, entity_id)
);

-- Daily digests (cached)
CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    digest_type TEXT NOT NULL,
    content TEXT NOT NULL,
    article_ids TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(date, digest_type)
);

-- Article relationships (related articles via similarity)
CREATE TABLE IF NOT EXISTS article_relations (
    article_id INTEGER REFERENCES articles(id),
    related_article_id INTEGER REFERENCES articles(id),
    similarity_score REAL,
    connection_note TEXT,
    PRIMARY KEY (article_id, related_article_id)
);

-- Reading stats (daily aggregates for the stats dashboard)
CREATE TABLE IF NOT EXISTS reading_stats (
    date TEXT NOT NULL,
    articles_saved INTEGER DEFAULT 0,
    articles_read INTEGER DEFAULT 0,
    articles_rated INTEGER DEFAULT 0,
    total_reading_time_min INTEGER DEFAULT 0,
    PRIMARY KEY (date)
);

-- Audio cache (TTS-generated MP3 files linked to articles)
CREATE TABLE IF NOT EXISTS audio (
    article_id INTEGER PRIMARY KEY REFERENCES articles(id),
    file_path TEXT NOT NULL,
    duration_seconds REAL,
    voice TEXT NOT NULL,
    model TEXT NOT NULL,
    file_size_bytes INTEGER,
    generated_at TEXT NOT NULL
);

-- Browser sessions (opaque tokens, stored hashed)
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- API tokens for non-browser clients (extension, MCP, scripts)
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP
);

-- Authors (deduped by canonical_key across article.author spellings)
CREATE TABLE IF NOT EXISTS authors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT,
    name TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    is_vip BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS article_authors (
    article_id INTEGER REFERENCES articles(id),
    author_id INTEGER REFERENCES authors(id),
    PRIMARY KEY (article_id, author_id)
);

-- Saved views (named filter+sort presets)
CREATE TABLE IF NOT EXISTS saved_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT,
    name TEXT NOT NULL,
    filter_json TEXT NOT NULL,
    sort_mode TEXT DEFAULT 'unread',
    position INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Wiki derived-index tables (Phase 1b W1): pages synthesized from the
-- library's articles, one row per entity/topic/source; source_count and
-- status track staleness. Population is owned by files-win reconcile, not
-- this schema (tables only, no backfill).
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
);

CREATE TABLE IF NOT EXISTS wiki_page_articles (
    page_id INTEGER REFERENCES wiki_pages(id),
    article_id INTEGER REFERENCES articles(id),
    PRIMARY KEY (page_id, article_id)
);

-- Highlights + notes (Phase 2 M2.1): sidecar files are the source of truth,
-- these tables are the derived SQLite index (files-win, same pattern as
-- wiki_pages above). Anchors reconcile against the article's current
-- markdown via tiro/anchors.py.
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
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT UNIQUE NOT NULL,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    highlight_id INTEGER REFERENCES highlights(id),
    body_markdown TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT
);

-- Reading-session telemetry (Phase 2 M2.3): opt-in
-- (reading_telemetry_enabled, default False), strictly local-only — feeds
-- the future wiki-importance ranking signal (Decision #8). Ephemeral
-- telemetry, not user-authored content, so unlike wiki_pages/highlights/
-- notes above there is no sidecar file — SQLite is the only store.
CREATE TABLE IF NOT EXISTS reading_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT UNIQUE NOT NULL,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    started_at TEXT,
    ended_at TEXT,
    max_scroll_pct INTEGER,
    active_seconds INTEGER,
    dwell_json TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_uid ON articles(uid);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_uid ON entities(uid);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_uid ON tags(uid);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_canonical ON entities(entity_type, canonical_key);

CREATE INDEX IF NOT EXISTS idx_articles_display_date ON articles(display_date DESC);
CREATE INDEX IF NOT EXISTS idx_articles_source_id ON articles(source_id);
CREATE INDEX IF NOT EXISTS idx_articles_is_read ON articles(is_read);
CREATE INDEX IF NOT EXISTS idx_articles_vector_status ON articles(vector_status);
CREATE INDEX IF NOT EXISTS idx_article_tags_tag ON article_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_article_entities_entity ON article_entities(entity_id);
CREATE INDEX IF NOT EXISTS idx_article_relations_related ON article_relations(related_article_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_authors_canonical ON authors(canonical_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_authors_uid ON authors(uid);
CREATE INDEX IF NOT EXISTS idx_article_authors_author ON article_authors(author_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_saved_views_uid ON saved_views(uid);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_pages_uid ON wiki_pages(uid);
CREATE INDEX IF NOT EXISTS idx_wiki_page_articles_article ON wiki_page_articles(article_id);
CREATE INDEX IF NOT EXISTS idx_highlights_article ON highlights(article_id);
CREATE INDEX IF NOT EXISTS idx_notes_article ON notes(article_id);
CREATE INDEX IF NOT EXISTS idx_reading_sessions_article ON reading_sessions(article_id);
"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode and foreign keys enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def dir_bytes(path: Path) -> int:
    """Total size in bytes of all files under `path` (0 if it doesn't exist)."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def init_db(db_path: Path) -> None:
    """Create the full schema for fresh databases; existing databases are
    evolved exclusively by migrate_db(). Detected via presence of the
    `articles` table (not file existence, so a stray empty/corrupt-touched
    file still gets the fresh-install schema)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    try:
        has_schema = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='articles'"
        ).fetchone() is not None
        if not has_schema:
            conn.executescript(SCHEMA)
            from tiro.migrations import LATEST_VERSION
            conn.execute(f"PRAGMA user_version = {LATEST_VERSION}")
            conn.commit()
            logger.info("Database initialized at %s", db_path)
    finally:
        conn.close()


def migrate_db(db_path: Path) -> None:
    """Run schema migrations (delegates to the versioned framework)."""
    from tiro.migrations import run_migrations

    run_migrations(db_path)
