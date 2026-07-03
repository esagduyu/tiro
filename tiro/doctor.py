"""Four-store consistency doctor: SQLite, ChromaDB, markdown files, audio.

Built on the M4a lifecycle contract: residual inconsistencies are
recoverable states, not crashes. scan() is read-only; fix() (Task 7)
repairs. Callers must have initialized the stores (init_db + migrate_db +
init_vectorstore) first. Run the doctor with the server STOPPED — both
SQLite (WAL) and ChromaDB tolerate readers, but repairs racing a live
server's writes can produce false positives.
"""

import logging
from pathlib import Path

from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.vectorstore import get_collection, retry_pending_vectors

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
            "(SELECT tag_id FROM article_tags WHERE tag_id IS NOT NULL)"
        ).fetchone()["n"]
        unreferenced_entities = conn.execute(
            "SELECT COUNT(*) AS n FROM entities WHERE id NOT IN "
            "(SELECT entity_id FROM article_entities WHERE entity_id IS NOT NULL)"
        ).fetchone()["n"]
    finally:
        conn.close()

    # Compare basenames, not raw values: a legacy row may store an absolute
    # markdown_path (M-1) which would never match the on-disk filename set.
    known_files = {Path(row["markdown_path"]).name for row in rows}
    disk_files = {p.name for p in config.articles_dir.glob("*.md")}

    orphaned_markdown = sorted(disk_files - known_files)
    missing_markdown = [
        {"id": row["id"], "title": row["title"], "markdown_path": row["markdown_path"]}
        for row in rows
        if not (config.articles_dir / Path(row["markdown_path"]).name).exists()
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
        "total_articles": len(rows),
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
    structural_keys = (
        "orphaned_markdown", "missing_markdown", "orphaned_vectors",
        "vector_missing", "vector_unmarked",
        "audio_rows_missing_file", "audio_files_without_row",
    )
    report["structurally_consistent"] = not any(report[k] for k in structural_keys)
    report["clean"] = report["structurally_consistent"] and \
        unreferenced_tags == 0 and unreferenced_entities == 0 and expired_sessions == 0
    return report


def fix(config: TiroConfig) -> dict:
    """Repair every discrepancy scan() finds. Returns the pre-fix report
    plus an 'actions' list. Safe to re-run: every repair is idempotent."""
    from tiro.lifecycle import delete_article

    report = scan(config)
    actions: list[str] = []

    # (a) markdown files with no DB row -> preserve under .orphaned/
    if report["orphaned_markdown"]:
        orphan_dir = config.library / ".orphaned"
        orphan_dir.mkdir(parents=True, exist_ok=True)
        for name in report["orphaned_markdown"]:
            dest = orphan_dir / name
            if dest.exists():
                stem, suffix = dest.stem, dest.suffix
                n = 1
                while (orphan_dir / f"{stem}.{n}{suffix}").exists():
                    n += 1
                dest = orphan_dir / f"{stem}.{n}{suffix}"
            (config.articles_dir / name).rename(dest)
            actions.append(f"moved orphaned markdown {name} -> .orphaned/{dest.name}")

    # (b) DB rows whose markdown file is gone -> complete the interrupted delete.
    # Guard against a moved/unmounted articles dir masquerading as mass delete
    # residue (interim review I-1): refuse when the articles dir itself is
    # absent, or when EVERY row is missing_markdown (more than one row) —
    # per-article delete-crash residue should never cover the whole library.
    total_articles = report["total_articles"]
    mass_delete = (
        not config.articles_dir.exists()
        or (len(report["missing_markdown"]) == total_articles and total_articles > 1)
    )
    deleted_ids: set[int] = set()
    refused_ids: set[int] = set()
    if mass_delete and report["missing_markdown"]:
        refused_ids = {item["id"] for item in report["missing_markdown"]}
        actions.append(
            f"REFUSED: {len(report['missing_markdown'])}/{total_articles} articles have no "
            "markdown file — articles directory missing or moved? Fix library_path "
            "(or delete rows individually with tiro delete) and re-run."
        )
    else:
        for item in report["missing_markdown"]:
            delete_article(config, item["id"])
            deleted_ids.add(item["id"])
            actions.append(
                f"deleted article {item['id']} ({item['title']!r}): markdown file missing"
            )

    # (c) vectors with no DB row -> delete from ChromaDB
    if report["orphaned_vectors"]:
        get_collection().delete(ids=report["orphaned_vectors"])
        actions.append(f"deleted {len(report['orphaned_vectors'])} orphaned vector(s)")

    # (d) status drift -> correct the status, then re-embed pending rows.
    #     Rows actually deleted by (b) are excluded (their ids are gone from
    #     articles). Rows REFUSED by the mass-delete guard are also excluded
    #     from the indexed->pending flip only: their markdown is unreachable
    #     (that's why the refusal fired), so retry_pending_vectors would
    #     immediately mark them 'failed', making the drift invisible to a
    #     future scan() (failed + no vector + present file matches no
    #     detection class). Leaving them 'indexed' keeps the drift visible as
    #     vector_missing so the user's mandated re-run (after fixing
    #     library_path) heals it. The indexed<-unmarked flip stays
    #     unconditional — it never touches missing-markdown rows.
    conn = get_connection(config.db_path)
    try:
        for aid in report["vector_missing"]:
            if aid in deleted_ids or aid in refused_ids:
                continue
            conn.execute(
                "UPDATE articles SET vector_status = 'pending' WHERE id = ?", (aid,)
            )
            actions.append(f"article {aid}: vector_status indexed -> pending (no vector)")
        for aid in report["vector_unmarked"]:
            if aid in deleted_ids:
                continue
            conn.execute(
                "UPDATE articles SET vector_status = 'indexed' WHERE id = ?", (aid,)
            )
            actions.append(f"article {aid}: vector_status -> indexed (vector present)")
        conn.commit()
    finally:
        conn.close()
    n = retry_pending_vectors(config)
    if n:
        actions.append(f"re-embedded {n} article(s)")

    conn = get_connection(config.db_path)
    try:
        still_pending = conn.execute(
            "SELECT COUNT(*) AS n FROM articles WHERE vector_status = 'pending'"
        ).fetchone()["n"]
    finally:
        conn.close()
    report["reembed_failures"] = still_pending
    if still_pending:
        actions.append(
            f"WARNING: {still_pending} article(s) still pending re-embed "
            "(retry failed — see logs; re-run tiro doctor --fix or check the embedding model)"
        )

    # (e) audio mismatches
    conn = get_connection(config.db_path)
    try:
        for aid in report["audio_rows_missing_file"]:
            cur = conn.execute("DELETE FROM audio WHERE article_id = ?", (aid,))
            if cur.rowcount:
                actions.append(f"deleted audio row for article {aid} (file missing)")
        # vacuum + session purge
        cur = conn.execute(
            "DELETE FROM tags WHERE id NOT IN "
            "(SELECT tag_id FROM article_tags WHERE tag_id IS NOT NULL)"
        )
        if cur.rowcount:
            actions.append(f"vacuumed {cur.rowcount} unreferenced tag(s)")
        cur = conn.execute(
            "DELETE FROM entities WHERE id NOT IN "
            "(SELECT entity_id FROM article_entities WHERE entity_id IS NOT NULL)"
        )
        if cur.rowcount:
            actions.append(f"vacuumed {cur.rowcount} unreferenced entit(y/ies)")
        cur = conn.execute("DELETE FROM sessions WHERE expires_at < datetime('now')")
        if cur.rowcount:
            actions.append(f"purged {cur.rowcount} expired session(s)")
        conn.commit()
    finally:
        conn.close()
    for name in report["audio_files_without_row"]:
        (config.library / "audio" / name).unlink(missing_ok=True)
        actions.append(f"deleted orphaned audio file {name}")

    report["actions"] = actions
    return report
