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
    vector_status TEXT DEFAULT 'pending'
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_uid ON articles(uid);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_uid ON entities(uid);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_uid ON tags(uid);
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
    """Initialize the database with the full schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    try:
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
