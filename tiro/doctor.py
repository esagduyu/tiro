"""Four-store consistency doctor: SQLite, ChromaDB, markdown files, audio.

Built on the M4a lifecycle contract: residual inconsistencies are
recoverable states, not crashes. scan() is read-only; fix() (Task 7)
repairs. Callers must have initialized the stores (init_db + migrate_db +
init_vectorstore) first. Run the doctor with the server STOPPED — both
SQLite (WAL) and ChromaDB tolerate readers, but repairs racing a live
server's writes can produce false positives.
"""

import logging

from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.vectorstore import get_collection

logger = logging.getLogger(__name__)


def scan(config: TiroConfig) -> dict:
    """Walk all four stores in both directions; return the discrepancy report."""
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute(
            "SELECT id, title, markdown_path, vector_status FROM articles"
        ).fetchall()
        audio_rows = conn.execute("SELECT article_id, file_path FROM audio").fetchall()
        expired_sessions = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE expires_at < datetime('now')"
        ).fetchone()["n"]
        unreferenced_tags = conn.execute(
            "SELECT COUNT(*) AS n FROM tags WHERE id NOT IN "
            "(SELECT DISTINCT tag_id FROM article_tags)"
        ).fetchone()["n"]
        unreferenced_entities = conn.execute(
            "SELECT COUNT(*) AS n FROM entities WHERE id NOT IN "
            "(SELECT DISTINCT entity_id FROM article_entities)"
        ).fetchone()["n"]
    finally:
        conn.close()

    known_files = {row["markdown_path"] for row in rows}
    disk_files = {p.name for p in config.articles_dir.glob("*.md")}

    orphaned_markdown = sorted(disk_files - known_files)
    missing_markdown = [
        {"id": row["id"], "title": row["title"], "markdown_path": row["markdown_path"]}
        for row in rows
        if not (config.articles_dir / row["markdown_path"]).exists()
    ]

    collection = get_collection()
    vec_ids = set(collection.get(include=[])["ids"])
    row_vec_ids = {f"article_{row['id']}" for row in rows}
    orphaned_vectors = sorted(vec_ids - row_vec_ids)

    vector_missing = [
        row["id"] for row in rows
        if row["vector_status"] == "indexed" and f"article_{row['id']}" not in vec_ids
    ]
    vector_unmarked = [
        row["id"] for row in rows
        if row["vector_status"] in ("pending", "failed")
        and f"article_{row['id']}" in vec_ids
    ]

    audio_dir = config.library / "audio"
    audio_known = {row["file_path"] for row in audio_rows}
    audio_disk = {p.name for p in audio_dir.glob("*.mp3")} if audio_dir.exists() else set()
    audio_rows_missing_file = [
        row["article_id"] for row in audio_rows
        if row["file_path"] not in audio_disk
    ]
    audio_files_without_row = sorted(audio_disk - audio_known)

    report = {
        "orphaned_markdown": orphaned_markdown,
        "missing_markdown": missing_markdown,
        "orphaned_vectors": orphaned_vectors,
        "vector_missing": vector_missing,
        "vector_unmarked": vector_unmarked,
        "audio_rows_missing_file": audio_rows_missing_file,
        "audio_files_without_row": audio_files_without_row,
        "unreferenced_tags": unreferenced_tags,
        "unreferenced_entities": unreferenced_entities,
        "expired_sessions": expired_sessions,
    }
    report["clean"] = not any(
        v for k, v in report.items()
        if k not in ("unreferenced_tags", "unreferenced_entities", "expired_sessions")
    ) and unreferenced_tags == 0 and unreferenced_entities == 0 and expired_sessions == 0
    return report
