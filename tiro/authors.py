"""Author find-or-create/merge helpers, layered on top of the `authors` and
`article_authors` tables introduced in migration 007.

`articles.author` (the free-text column written by the ingestion connectors)
stays the authoritative value for display and markdown frontmatter — it is
never rewritten by anything in this module. The `authors` table is a
secondary, deduped layer used for VIP-marking and merging near-duplicate
spellings ("Matt Levine" / "matt LEVINE ") into one identity; it is kept in
sync at ingest time via `link_article_author`, called from
`tiro.ingestion.processor.process_article`.

None of the functions here call `conn.commit()` — same convention as the
tag/entity inserts in processor.py — so the caller controls the transaction
boundary (bundled with whatever else it's committing in the same request).
"""

import sqlite3

from tiro.migrations import canonical_key, new_ulid


def ensure_author(conn: sqlite3.Connection, name: str) -> int | None:
    """Find-or-create an author row by canonical_key. Returns None for
    empty/whitespace-only names. First-seen spelling wins: if an author
    with the same canonical_key already exists, its stored `name` is left
    untouched even if `name` differs in casing/whitespace."""
    stripped = (name or "").strip()
    if not stripped:
        return None
    key = canonical_key(stripped)
    row = conn.execute(
        "SELECT id FROM authors WHERE canonical_key = ?", (key,)
    ).fetchone()
    if row:
        return row["id"]
    cursor = conn.execute(
        "INSERT INTO authors (uid, name, canonical_key) VALUES (?, ?, ?)",
        (new_ulid(), stripped, key),
    )
    return cursor.lastrowid


def link_article_author(conn: sqlite3.Connection, article_id: int, name: str | None) -> None:
    """Link an article to its author, creating the author if needed. No-op
    when `name` is None/empty (articles without a detected author)."""
    author_id = ensure_author(conn, name) if name else None
    if author_id is None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO article_authors (article_id, author_id) VALUES (?, ?)",
        (article_id, author_id),
    )


def merge_authors(conn: sqlite3.Connection, keep_id: int, merge_id: int) -> None:
    """Merge `merge_id` into `keep_id`: repoint article_authors junctions
    (INSERT OR IGNORE, since an article may already link both), OR the VIP
    flags together, then delete the loser. Same pattern as the entity merge
    in migrations.py's _m005_entity_canonical."""
    merge_row = conn.execute(
        "SELECT is_vip FROM authors WHERE id = ?", (merge_id,)
    ).fetchone()
    if merge_row and merge_row["is_vip"]:
        conn.execute("UPDATE authors SET is_vip = 1 WHERE id = ?", (keep_id,))
    conn.execute(
        "INSERT OR IGNORE INTO article_authors (article_id, author_id)"
        " SELECT article_id, ? FROM article_authors WHERE author_id = ?",
        (keep_id, merge_id),
    )
    conn.execute("DELETE FROM article_authors WHERE author_id = ?", (merge_id,))
    conn.execute("DELETE FROM authors WHERE id = ?", (merge_id,))
