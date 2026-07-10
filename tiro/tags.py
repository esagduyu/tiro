"""Shared tag helpers: ensure a tag row exists, link tags to an article.

Deterministic, no AI. Current callers: `tiro/importer.py` (bundle-import tag
rebuild) and `tiro/ingestion/importers/base.py` (the `import-stub` tag plus
the Instapaper/Omnivore folder→tag mapping). `processor.py` still hand-rolls
its own AI-tag `INSERT OR IGNORE INTO tags`, and RSS folder tags don't route
through here — so this module centralizes the pattern for the import paths
only, not library-wide. Never commits; the caller owns the transaction
boundary.
"""

import sqlite3

from tiro.migrations import new_ulid


def ensure_tag(conn: sqlite3.Connection, name: str) -> int:
    """Return the id of the tag named `name`, creating it (with a fresh uid)
    if absent. `name` is used verbatim as the tag's canonical name; callers
    that need case-folding normalize first (the importer folder/label path via
    `attach_tags`; the bundle-restore path in `importer.py` deliberately
    preserves the exported name as-is for round-trip fidelity)."""
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute("INSERT INTO tags (uid, name) VALUES (?, ?)", (new_ulid(), name))
    return cur.lastrowid


def attach_tags(conn: sqlite3.Connection, article_id: int, names) -> None:
    """Ensure + link each tag name to `article_id`. Names are normalized to
    lowercase (stripped) so a folder/label tag like "Tech" collides with the
    lowercase form the RSS folder-tag path (`rss._attach_folder_tag`) writes —
    otherwise the same concept would split into two tag rows. Blank/
    whitespace-only names are skipped; links are idempotent (`INSERT OR
    IGNORE`). Does not commit."""
    for name in names:
        if not name or not name.strip():
            continue
        tag_id = ensure_tag(conn, name.strip().lower())
        conn.execute(
            "INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (?, ?)",
            (article_id, tag_id),
        )
