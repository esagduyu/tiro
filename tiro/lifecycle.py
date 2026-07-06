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


def _delete_annotation_sidecars(config: TiroConfig, markdown_path: str | None, article_id: int) -> None:
    """Remove the article's highlights sidecar (`annotations/{stem}.jsonl`)
    and article-level note sidecar (`notes/{stem}.md`), if either exists.
    `markdown_path` must be read BEFORE the article row is deleted -- it's
    the only thing the stem can be derived from (Phase 2 M2.1)."""
    if not markdown_path:
        return
    from tiro.annotations import annotations_dir, notes_dir, sidecar_stem

    stem = sidecar_stem(markdown_path)
    try:
        (annotations_dir(config) / f"{stem}.jsonl").unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Annotations sidecar unlink failed for article %d: %s", article_id, e)
    try:
        (notes_dir(config) / f"{stem}.md").unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Note sidecar unlink failed for article %d: %s", article_id, e)


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
        _delete_annotation_sidecars(config, markdown_path, article_id)

        # SQLite last, in one transaction: junctions (both directions) + audio + article
        conn.execute("DELETE FROM article_tags WHERE article_id = ?", (article_id,))
        conn.execute("DELETE FROM article_entities WHERE article_id = ?", (article_id,))
        conn.execute("DELETE FROM article_authors WHERE article_id = ?", (article_id,))
        conn.execute(
            "DELETE FROM article_relations WHERE article_id = ? OR related_article_id = ?",
            (article_id, article_id),
        )
        conn.execute("DELETE FROM audio WHERE article_id = ?", (article_id,))
        # Highlights + notes (Phase 2 M2.1): notes.highlight_id REFERENCES
        # highlights(id) with foreign_keys=ON, so notes must go first.
        # notes.article_id is set for BOTH kinds (article-level and
        # highlight-anchored), so this one DELETE clears both.
        conn.execute("DELETE FROM notes WHERE article_id = ?", (article_id,))
        conn.execute("DELETE FROM highlights WHERE article_id = ?", (article_id,))
        # Reading-session telemetry (Phase 2 M2.3): reading_sessions.article_id
        # REFERENCES articles(id) with foreign_keys=ON, same pattern as the
        # highlights/notes cleanup above -- must go before the article row is
        # deleted below or the DELETE FROM articles raises IntegrityError.
        conn.execute("DELETE FROM reading_sessions WHERE article_id = ?", (article_id,))

        # Wiki (Phase 1b): wiki_page_articles.article_id REFERENCES articles(id)
        # with foreign_keys=ON, so any page citing this article would make the
        # DELETE FROM articles below raise IntegrityError unless the junction
        # is cleared first. Collect the affected page ids BEFORE clearing the
        # junction (afterwards there's no way to tell which pages cited this
        # article), then flip those pages stale -- a deleted article's
        # citations must degrade the page's trust status, not just silently
        # disappear from source_count.
        page_ids = [
            row["page_id"]
            for row in conn.execute(
                "SELECT DISTINCT page_id FROM wiki_page_articles WHERE article_id = ?",
                (article_id,),
            ).fetchall()
        ]
        conn.execute("DELETE FROM wiki_page_articles WHERE article_id = ?", (article_id,))
        if page_ids:
            from tiro.wiki import mark_page_ids_stale

            mark_page_ids_stale(config, conn, page_ids)

        cursor = conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()
