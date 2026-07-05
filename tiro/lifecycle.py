"""Article lifecycle coordinator: delete across all four stores.

delete_article() is best-effort per store so it doubles as ingestion
rollback — a missing file or vector is not an error. SQLite's articles-row
deletion is the authoritative "did it exist" signal.

Two residual-orphan classes are possible and both are tolerated by design:
- Ingestion crashes (process killed, not a caught exception) can leave a
  markdown FILE with no DB row referencing it, since the file is written
  before the row is inserted.
- Delete crashes: vectors/files are removed before the SQLite row (so a
  mid-delete crash after that point leaves a DB ROW pointing at an
  already-deleted file/vector, not the reverse).
Either way the reader degrades gracefully (missing file/vector is handled,
not fatal) and re-running delete_article() on the same id is idempotent.
Reconciling both classes is `tiro doctor`'s job (M5), not this module's.
"""

import logging
from pathlib import Path

from tiro.config import TiroConfig
from tiro.database import get_connection

logger = logging.getLogger(__name__)


def _delete_vector(config: TiroConfig, article_id: int) -> None:
    try:
        from tiro.vectorstore import get_collection

        get_collection().delete(ids=[f"article_{article_id}"])
    except Exception as e:
        logger.warning("ChromaDB delete failed for article %d: %s", article_id, e)


def _delete_files(config: TiroConfig, markdown_path: str | None, article_id: int) -> None:
    if markdown_path:
        md = Path(markdown_path)
        if not md.is_absolute():
            md = config.articles_dir / md
        try:
            md.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Markdown unlink failed for article %d: %s", article_id, e)
    audio = config.library / "audio" / f"{article_id}.mp3"
    try:
        audio.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Audio unlink failed for article %d: %s", article_id, e)


def delete_article(config: TiroConfig, article_id: int) -> bool:
    """Remove an article from all stores. Returns True if it existed."""
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT markdown_path FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        markdown_path = row["markdown_path"] if row else None

        # Non-SQLite stores first (best-effort)
        _delete_vector(config, article_id)
        _delete_files(config, markdown_path, article_id)

        # SQLite last, in one transaction: junctions (both directions) + audio + article
        conn.execute("DELETE FROM article_tags WHERE article_id = ?", (article_id,))
        conn.execute("DELETE FROM article_entities WHERE article_id = ?", (article_id,))
        conn.execute("DELETE FROM article_authors WHERE article_id = ?", (article_id,))
        conn.execute(
            "DELETE FROM article_relations WHERE article_id = ? OR related_article_id = ?",
            (article_id, article_id),
        )
        conn.execute("DELETE FROM audio WHERE article_id = ?", (article_id,))
        cursor = conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()
