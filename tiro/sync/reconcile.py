"""Local reconcile engine (sync S1 — the absorbed Phase 2b).

External edits to `library_path` (Obsidian et al.) reconcile into SQLite/
ChromaDB/anchors. Shared by the scheduler loop (tiro/app.py) and the
`tiro reconcile` CLI. Spec: docs/plans/2026-07-06-sync-engine-spec.md §7.

Design points (see the S1 plan header for the full decision record):
- Stateless two-poll hash-settle per pass: scan, sleep SETTLE_SECONDS,
  re-hash candidates; a hash that moved between polls -> skipped_unsettled,
  retried next pass. Defeats temp+rename and partial writes.
- Files are truth. S1 NEVER rewrites an externally-owned article file
  (unknown frontmatter fields preserved by construction); derived fields
  sync into SQLite only.
- Deletes go through lifecycle.delete_article, and only under the
  mass-delete guard (articles dir missing / all-missing / > max(10, 20%)).
- body_hash NULL means "no baseline": lazily adopted, never treated as an
  external edit.
- Every pass re-hashes every non-conflict .md body (no mtime cache —
  personal-library scale; revisit if profiling ever says otherwise).
- Requires an initialized ChromaDB collection (delete/create paths touch
  it): the server lifespan provides one; the CLI calls init_vectorstore.
"""

import logging
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import frontmatter

from tiro.anchors import content_hash, reconcile_anchor
from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.migrations import new_ulid

logger = logging.getLogger(__name__)

SETTLE_SECONDS = 2.0
MASS_DELETE_FLOOR = 10
MASS_DELETE_FRACTION = 0.2
ROW_LEAD_SLACK_SECONDS = 1.0

# {stem}.conflict-{deviceshort}-{yyyymmdd}[-n].md  (spec §4; device 'local' in S1)
_CONFLICT_RE = re.compile(r"\.conflict-[A-Za-z0-9]+-\d{8}(-\d+)?\.md$")


@dataclass
class ReconcileReport:
    """FROZEN shape (skeleton S1): counts + details."""
    changed: int = 0
    ingested: int = 0
    deleted: int = 0
    conflicts: int = 0
    re_anchored: int = 0
    skipped_unsettled: int = 0
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        return (
            f"changed={self.changed} ingested={self.ingested} deleted={self.deleted} "
            f"conflicts={self.conflicts} re_anchored={self.re_anchored} "
            f"skipped_unsettled={self.skipped_unsettled}"
        )


def is_conflict_file(name: str) -> bool:
    return bool(_CONFLICT_RE.search(name))


def body_hash_of_file(path: Path) -> str | None:
    """sha256 of the markdown body (frontmatter stripped); None if unreadable."""
    try:
        return content_hash(frontmatter.load(str(path)).content)
    except Exception as e:
        logger.warning("Unreadable markdown %s: %s", path, e)
        return None


def _detail(report: ReconcileReport, key: str):
    return report.details.setdefault(key, [])


# --- scan + settle ----------------------------------------------------------


@dataclass
class _Scan:
    rows_by_name: dict          # markdown basename -> sqlite3.Row
    disk: dict                  # basename -> Path (conflict files excluded)
    changed: list               # settled basenames with row + hash drift
    created: list               # settled basenames with no row
    missing: list               # rows whose file is (still) absent
    hashes: dict                # basename -> settled body hash


def _scan_with_settle(config: TiroConfig, report: ReconcileReport,
                      dry_run: bool) -> _Scan:
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute(
            "SELECT id, uid, title, author, summary, url, markdown_path, body_hash "
            "FROM articles"
        ).fetchall()
    finally:
        conn.close()
    rows_by_name = {Path(r["markdown_path"]).name: r for r in rows}

    disk: dict[str, Path] = {}
    if config.articles_dir.exists():
        for p in sorted(config.articles_dir.glob("*.md")):
            if is_conflict_file(p.name):
                continue
            disk[p.name] = p

    changed, created, backfill, hashes = [], [], [], {}
    for name, path in disk.items():
        h = body_hash_of_file(path)
        if h is None:
            _detail(report, "unreadable").append(name)
            continue
        hashes[name] = h
        row = rows_by_name.get(name)
        if row is None:
            created.append(name)
        elif row["body_hash"] is None:
            backfill.append(name)
        elif h != row["body_hash"]:
            changed.append(name)
    missing = [r for name, r in rows_by_name.items() if name not in disk]

    # Settle poll: anything we might act on must hash identically twice.
    if changed or created or missing:
        time.sleep(SETTLE_SECONDS)
        settled_changed, settled_created = [], []
        for bucket, out in ((changed, settled_changed), (created, settled_created)):
            for name in bucket:
                h2 = body_hash_of_file(disk[name])
                if h2 is not None and h2 == hashes[name]:
                    out.append(name)
                else:
                    report.skipped_unsettled += 1
        changed, created = settled_changed, settled_created
        settled_missing = []
        for row in missing:
            if (config.articles_dir / Path(row["markdown_path"]).name).exists():
                report.skipped_unsettled += 1  # reappeared mid-pass (temp+rename)
            else:
                settled_missing.append(row)
        missing = settled_missing

    # Lazy backfill (not "changed": no baseline to diff against).
    if backfill:
        _detail(report, "backfilled").extend(backfill)
    if backfill and not dry_run:
        conn = get_connection(config.db_path)
        try:
            for name in backfill:
                conn.execute(
                    "UPDATE articles SET body_hash = ? WHERE id = ?",
                    (hashes[name], rows_by_name[name]["id"]),
                )
            conn.commit()
        finally:
            conn.close()

    return _Scan(rows_by_name, disk, changed, created, missing, hashes)


# --- changed-body pipeline --------------------------------------------------


def _sync_tags_from_frontmatter(conn, article_id: int, tag_names: list[str]) -> None:
    conn.execute("DELETE FROM article_tags WHERE article_id = ?", (article_id,))
    for tag_name in tag_names:
        conn.execute(
            "INSERT OR IGNORE INTO tags (uid, name) VALUES (?, ?)",
            (new_ulid(), tag_name),
        )
        tag_row = conn.execute(
            "SELECT id FROM tags WHERE name = ?", (tag_name,)
        ).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (?, ?)",
            (article_id, tag_row["id"]),
        )


def _reanchor_census(conn, article_id: int, body: str, report: ReconcileReport) -> None:
    """Live census only — statuses are computed on GET by the annotations API;
    sidecar content_hash is creation-time provenance and is NEVER rewritten."""
    rows = conn.execute(
        "SELECT uid, quote_text, prefix_context, suffix_context, "
        "text_position_start, text_position_end, content_hash "
        "FROM highlights WHERE article_id = ?",
        (article_id,),
    ).fetchall()
    for row in rows:
        status = reconcile_anchor(body, {
            "quote": row["quote_text"],
            "prefix": row["prefix_context"],
            "suffix": row["suffix_context"],
            "position_start": row["text_position_start"],
            "position_end": row["text_position_end"],
            "content_hash": row["content_hash"],
        })["status"]
        if status in ("exact", "shifted"):
            report.re_anchored += 1
        else:
            _detail(report, "anchor_warnings").append(
                {"highlight_uid": row["uid"], "article_id": article_id, "status": status}
            )
            logger.warning(
                "Highlight %s on article %d no longer anchors after external edit (%s)",
                row["uid"], article_id, status,
            )


def _apply_changed(config: TiroConfig, scan: _Scan, report: ReconcileReport,
                   dry_run: bool) -> None:
    if not scan.changed:
        return
    if dry_run:
        report.changed = len(scan.changed)
        _detail(report, "changed_files").extend(scan.changed)
        return
    conn = get_connection(config.db_path)
    try:
        for name in scan.changed:
            row = scan.rows_by_name[name]
            post = frontmatter.load(str(scan.disk[name]))
            body = post.content
            meta = post.metadata or {}
            title = str(meta.get("title") or row["title"])
            author = meta["author"] if "author" in meta else row["author"]
            summary = meta["summary"] if "summary" in meta else row["summary"]
            word_count = len(body.split())
            conn.execute(
                "UPDATE articles SET title = ?, author = ?, summary = ?, "
                "word_count = ?, reading_time_min = ?, body_hash = ?, "
                "vector_status = 'pending' WHERE id = ?",
                (title, author, summary, word_count,
                 max(1, math.ceil(word_count / 250)), scan.hashes[name], row["id"]),
            )
            if isinstance(meta.get("tags"), list):
                _sync_tags_from_frontmatter(
                    conn, row["id"], [str(t) for t in meta["tags"]]
                )
            try:
                from tiro.wiki import mark_pages_stale
                mark_pages_stale(config, conn, row["id"])
            except Exception as e:
                logger.error("mark_pages_stale failed for %d (non-fatal): %s",
                             row["id"], e)
            _reanchor_census(conn, row["id"], body, report)
            report.changed += 1
            _detail(report, "changed_files").append(name)
        conn.commit()
    finally:
        conn.close()


# --- entry point -------------------------------------------------------------


def reconcile_library(config: TiroConfig, *, dry_run: bool = False) -> ReconcileReport:
    """One reconcile pass (FROZEN signature). See module docstring."""
    report = ReconcileReport()
    report.details["dry_run"] = dry_run
    if not config.articles_dir.exists():
        rows_exist = False
        if config.db_path.exists():
            conn = get_connection(config.db_path)
            try:
                rows_exist = conn.execute(
                    "SELECT 1 FROM articles LIMIT 1"
                ).fetchone() is not None
            finally:
                conn.close()
        if rows_exist:
            report.details["delete_guard"] = "articles directory missing"
            logger.warning(
                "Reconcile: articles dir %s missing while rows exist — guarded, "
                "nothing done (fix library_path or run tiro doctor)",
                config.articles_dir,
            )
        return report

    scan = _scan_with_settle(config, report, dry_run)
    _apply_changed(config, scan, report, dry_run)
    return report
