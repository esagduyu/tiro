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

from tiro.annotations import (
    annotations_dir,
    annotations_mass_delete_guard,
    notes_dir,
    reconcile_annotations,
    sidecar_stem,
)
from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.vectorstore import get_collection, retry_pending_vectors
from tiro.wiki import _RESERVED_FILENAMES, reconcile_wiki_index

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
        unreferenced_authors = conn.execute(
            "SELECT COUNT(*) AS n FROM authors WHERE id NOT IN "
            "(SELECT author_id FROM article_authors WHERE author_id IS NOT NULL)"
        ).fetchone()["n"]
        wiki_page_slugs = {
            row["slug"] for row in conn.execute("SELECT slug FROM wiki_pages").fetchall()
        }
        highlight_article_ids = {
            r["article_id"]
            for r in conn.execute("SELECT DISTINCT article_id FROM highlights").fetchall()
        }
        note_article_ids = {
            r["article_id"]
            for r in conn.execute(
                "SELECT DISTINCT article_id FROM notes WHERE highlight_id IS NULL"
            ).fetchall()
        }
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
    vector_failed = [
        row["id"] for row in rows
        if row["vector_status"] == "failed"
        and f"article_{row['id']}" not in vec_ids
        and (config.articles_dir / Path(row["markdown_path"]).name).exists()
    ]

    audio_dir = config.library / "audio"
    audio_known = {row["file_path"] for row in audio_rows}
    audio_disk = {p.name for p in audio_dir.glob("*.mp3")} if audio_dir.exists() else set()
    audio_rows_missing_file = [
        row["article_id"] for row in audio_rows
        if row["file_path"] not in audio_disk
    ]
    audio_files_without_row = sorted(audio_disk - audio_known)

    # Wiki index drift: derived wiki_pages rows vs. files actually on disk,
    # matched by slug (relative path minus .md). Cheap comparison, not a
    # full reconcile -- excludes the bookkeeping files (_schema.md/index.md/
    # log.md) the same way reconcile_wiki_index() does, since those never
    # get a derived row. Counts BOTH directions: a file with no row, and a
    # row with no file (e.g. a hand-deleted page).
    wiki_files = set()
    if config.wiki_dir.exists():
        wiki_files = {
            p.relative_to(config.wiki_dir).with_suffix("").as_posix()
            for p in config.wiki_dir.rglob("*.md")
            if p.name not in _RESERVED_FILENAMES
        }
    wiki_index_drift = len(wiki_files ^ wiki_page_slugs)

    # Annotations (highlights/notes, Phase 2 M2.1) index drift: same cheap
    # presence-based comparison as wiki_index_drift above (stems on disk vs.
    # stems with rows), not a full reconcile -- reconcile_annotations() does
    # the real content-level matching and row repair, on --fix only. Guard
    # classification uses the SAME `annotations_mass_delete_guard` predicate
    # reconcile_annotations() itself uses (tiro/annotations.py), so scan()'s
    # cheap pre-check can never disagree with what --fix would actually
    # refuse to do: both "the sidecar directory is missing entirely" and
    # "the directory exists but is empty relative to every stem with rows"
    # count as guarded here, not just the dir-missing case. structurally_
    # consistent (below) folds the guard in so a guard event stays visible
    # in the exit code even without --fix; plain drift is housekeeping only,
    # same as wiki_index_drift.
    ann_dir = annotations_dir(config)
    nt_dir = notes_dir(config)
    ann_dir_exists = ann_dir.exists()
    nt_dir_exists = nt_dir.exists()
    ann_file_stems = {p.stem for p in ann_dir.glob("*.jsonl")} if ann_dir_exists else set()
    note_file_stems = {p.stem for p in nt_dir.glob("*.md")} if nt_dir_exists else set()
    stem_by_article_id = {row["id"]: sidecar_stem(row) for row in rows}
    highlight_stems_with_rows = {
        stem_by_article_id[aid] for aid in highlight_article_ids if aid in stem_by_article_id
    }
    note_stems_with_rows = {
        stem_by_article_id[aid] for aid in note_article_ids if aid in stem_by_article_id
    }

    annotations_index_drift = 0
    if ann_dir_exists:
        annotations_index_drift += len(ann_file_stems ^ highlight_stems_with_rows)
    if nt_dir_exists:
        annotations_index_drift += len(note_file_stems ^ note_stems_with_rows)
    annotations_guarded = annotations_mass_delete_guard(
        ann_dir_exists, highlight_stems_with_rows, ann_file_stems
    ) or annotations_mass_delete_guard(nt_dir_exists, note_stems_with_rows, note_file_stems)

    report = {
        "total_articles": len(rows),
        "orphaned_markdown": orphaned_markdown,
        "missing_markdown": missing_markdown,
        "orphaned_vectors": orphaned_vectors,
        "vector_missing": vector_missing,
        "vector_unmarked": vector_unmarked,
        "vector_failed": vector_failed,
        "audio_rows_missing_file": audio_rows_missing_file,
        "audio_files_without_row": audio_files_without_row,
        "unreferenced_tags": unreferenced_tags,
        "unreferenced_entities": unreferenced_entities,
        "unreferenced_authors": unreferenced_authors,
        "expired_sessions": expired_sessions,
        "wiki_index_drift": wiki_index_drift,
        "annotations_index_drift": annotations_index_drift,
        "annotations_guarded": annotations_guarded,
    }
    structural_keys = (
        "orphaned_markdown", "missing_markdown", "orphaned_vectors",
        "vector_missing", "vector_unmarked", "vector_failed",
        "audio_rows_missing_file", "audio_files_without_row",
    )
    # annotations_guarded folds into structural (not just "clean"): a guard
    # event means rows are one directory-restore away from a mass deletion
    # that files-win would otherwise apply -- it must stay visible in the
    # exit code even without --fix, the same way a mass-delete-guard refusal
    # for markdown keeps missing_markdown non-empty above.
    report["structurally_consistent"] = (
        not any(report[k] for k in structural_keys) and not annotations_guarded
    )
    report["clean"] = report["structurally_consistent"] and \
        unreferenced_tags == 0 and unreferenced_entities == 0 and \
        unreferenced_authors == 0 and expired_sessions == 0 and \
        wiki_index_drift == 0 and annotations_index_drift == 0
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
    #     from the indexed->pending and failed->pending flips: their markdown
    #     is unreachable (that's why the refusal fired), so retry_pending_vectors
    #     would immediately mark them 'failed' again, making the drift invisible
    #     to a future scan() (failed + no vector + present file matches no
    #     detection class). Leaving them 'indexed'/'failed' keeps the drift
    #     visible (as vector_missing / vector_failed) so the user's mandated
    #     re-run (after fixing library_path) heals it. vector_failed rows are
    #     by definition markdown-present (scan()'s detection requires it), so
    #     they can never coincide with refused_ids in practice, but the same
    #     skip is applied for defense-in-depth and readability. The
    #     indexed<-unmarked flip stays unconditional — it never touches
    #     missing-markdown rows.
    conn = get_connection(config.db_path)
    try:
        for aid in report["vector_missing"]:
            if aid in deleted_ids or aid in refused_ids:
                continue
            conn.execute(
                "UPDATE articles SET vector_status = 'pending' WHERE id = ?", (aid,)
            )
            actions.append(f"article {aid}: vector_status indexed -> pending (no vector)")
        for aid in report["vector_failed"]:
            if aid in deleted_ids or aid in refused_ids:
                continue
            conn.execute(
                "UPDATE articles SET vector_status = 'pending' WHERE id = ?", (aid,)
            )
            actions.append(f"article {aid}: vector_status failed -> pending (re-embed queued)")
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
    # During a mass-delete refusal, EVERY markdown file is unreachable (that's
    # why the refusal fired). retry_pending_vectors() marks any 'pending' row
    # whose file it can't reach as 'failed' — including rows that were
    # legitimately 'pending' for an unrelated reason (e.g. a ChromaDB outage
    # at ingestion time), before the mass-delete ever happened. Once flipped
    # to 'failed', such a row becomes invisible to scan() (vector_missing
    # needs 'indexed'; vector_unmarked needs a vector present) and is
    # excluded from reembed_failures (which only counts 'pending'), so the
    # article would be permanently unsearchable even after the user restores
    # the directory and re-runs as instructed. Skip the doomed-and-destructive
    # retry here; the still-pending re-query below still counts these rows
    # honestly (they DO still need re-embedding), which keeps exit code 1 via
    # structurally_consistent anyway.
    if mass_delete and report["missing_markdown"]:
        actions.append(
            "SKIPPED re-embed retry: articles directory is unreachable during "
            "the mass-delete refusal above — retrying would mark legitimately "
            "pending rows as permanently 'failed'. Fix the articles directory "
            "and re-run to retry them safely."
        )
    else:
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
        cur = conn.execute(
            "DELETE FROM authors WHERE id NOT IN "
            "(SELECT author_id FROM article_authors WHERE author_id IS NOT NULL)"
        )
        if cur.rowcount:
            actions.append(f"vacuumed {cur.rowcount} unreferenced author(s)")
        cur = conn.execute("DELETE FROM sessions WHERE expires_at < datetime('now')")
        if cur.rowcount:
            actions.append(f"purged {cur.rowcount} expired session(s)")
        conn.commit()
    finally:
        conn.close()
    for name in report["audio_files_without_row"]:
        (config.library / "audio" / name).unlink(missing_ok=True)
        actions.append(f"deleted orphaned audio file {name}")

    # (f) wiki index drift -> rebuild the derived wiki_pages/wiki_page_articles
    # rows from what's on disk (files win). NEVER writes or deletes page
    # files -- reconcile_wiki_index() only touches the derived SQLite tables.
    if report["wiki_index_drift"]:
        wiki_result = reconcile_wiki_index(config)
        actions.append(
            f"reconciled wiki index: {wiki_result['pages']} page(s) indexed "
            f"({wiki_result['skipped']} skipped, "
            f"{wiki_result['unresolved_articles']} unresolved article ref(s), "
            f"{wiki_result['duplicate_uids']} duplicate uid(s))"
        )

    # (g) annotations (highlights/notes, Phase 2 M2.1) -> reconcile_annotations()
    # (files win), run UNCONDITIONALLY. It's idempotent and cheap, and gating
    # it on presence-based drift (annotations_index_drift only compares file
    # stems vs. row-bearing stems) misses pure CONTENT drift -- a hand-edited
    # quote/color/note where the row and file both exist for the same stem
    # never trips that drift counter, so --fix would silently skip healing
    # it. Running it every time also still covers the guard case (a whole
    # sidecar directory missing while rows reference it) -- reconcile_
    # annotations() itself is what holds the guard and reports it back via
    # the 'guarded' count, same "refuse, don't delete" posture as the
    # markdown mass-delete guard above. NEVER writes or deletes sidecar file
    # CONTENT -- only moves true orphans into .orphaned/ and repairs the
    # derived highlights/notes rows.
    ann_result = reconcile_annotations(config)
    actions.append(
        "reconciled annotations: "
        f"{ann_result['highlights_inserted']} highlight(s) inserted, "
        f"{ann_result['highlights_updated']} updated, "
        f"{ann_result['highlights_deleted']} deleted; "
        f"{ann_result['notes_inserted']} note(s) inserted, "
        f"{ann_result['notes_updated']} updated, "
        f"{ann_result['notes_deleted']} deleted; "
        f"{ann_result['orphaned_files']} orphaned sidecar(s) moved, "
        f"{ann_result['malformed_lines']} malformed line(s) skipped"
        + (
            f"; {ann_result['duplicate_uid_lines']} duplicate-uid line(s) skipped"
            if ann_result["duplicate_uid_lines"]
            else ""
        )
        + (
            f"; {ann_result['unreadable_files']} unreadable sidecar(s) skipped "
            "(permissions/decode error — check logs)"
            if ann_result["unreadable_files"]
            else ""
        )
        + (
            f"; {ann_result['uid_mismatch_lines']} line(s) with a mismatched "
            "article_uid indexed under their stem-resolved article anyway"
            if ann_result["uid_mismatch_lines"]
            else ""
        )
        + (
            f"; {ann_result['guarded']} guard(s) held — a sidecar "
            "directory is missing or effectively empty while rows still "
            "reference it, restore it and re-run"
            if ann_result["guarded"]
            else ""
        )
    )
    report["annotations_reconcile"] = ann_result

    report["actions"] = actions
    return report
