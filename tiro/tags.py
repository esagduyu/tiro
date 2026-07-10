"""Shared tag helpers: ensure a tag row exists, link tags to an article.

Centralizes the `INSERT OR IGNORE INTO tags` / `article_tags` pattern that
`processor.py` (AI tags), `rss.py` (folder tags), and `importer.py` (bundle
tag rebuild) each hand-rolled. Deterministic, no AI — used for OPML folder
tags, the `import-stub` tag, and Instapaper folder tags. Never commits; the
caller owns the transaction boundary.
"""

import sqlite3

from tiro.migrations import new_ulid


def ensure_tag(conn: sqlite3.Connection, name: str) -> int:
    """Return the id of the tag named `name`, creating it (with a fresh uid)
    if absent."""
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute("INSERT INTO tags (uid, name) VALUES (?, ?)", (new_ulid(), name))
    return cur.lastrowid


def attach_tags(conn: sqlite3.Connection, article_id: int, names) -> None:
    """Ensure + link each tag name to `article_id`. Blank/whitespace-only
    names are skipped; links are idempotent (`INSERT OR IGNORE`). Does not
    commit."""
    for name in names:
        if not name or not name.strip():
            continue
        tag_id = ensure_tag(conn, name.strip())
        conn.execute(
            "INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (?, ?)",
            (article_id, tag_id),
        )
