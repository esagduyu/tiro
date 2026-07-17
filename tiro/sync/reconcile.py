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
from datetime import UTC, datetime
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


def list_conflict_files(config: TiroConfig) -> list[str]:
    """Census of conflict files across the library (doctor report-only)."""
    from tiro.annotations import notes_dir

    found: list[str] = []
    for base in (config.articles_dir, notes_dir(config)):
        if base.exists():
            found.extend(p.name for p in base.glob("*.md") if is_conflict_file(p.name))
    if config.wiki_dir.exists():
        found.extend(
            p.relative_to(config.wiki_dir).as_posix()
            for p in config.wiki_dir.rglob("*.md") if is_conflict_file(p.name)
        )
    return sorted(found)


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


def _reanchor_census(conn, article_id: int, body: str,
                     report: ReconcileReport | None) -> None:
    """Live census only — statuses are computed on GET by the annotations API;
    sidecar content_hash is creation-time provenance and is NEVER rewritten.
    `report=None` (the S2 apply path) skips the census bookkeeping but still
    logs anchor warnings."""
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
            if report is not None:
                report.re_anchored += 1
        else:
            if report is not None:
                _detail(report, "anchor_warnings").append(
                    {"highlight_uid": row["uid"], "article_id": article_id, "status": status}
                )
            logger.warning(
                "Highlight %s on article %d no longer anchors after external edit (%s)",
                row["uid"], article_id, status,
            )


def refresh_article_from_file(config: TiroConfig, conn, row, path: Path,
                              body: str, new_hash: str,
                              report: ReconcileReport | None = None, *,
                              meta: dict | None = None) -> None:
    """Refresh one article's derived SQLite state from its (already-settled)
    markdown file. Shared by S1's changed pipeline and S2's apply_ops
    (file_put on a known article). Never rewrites the file; never commits;
    never swallows exceptions — the caller owns transaction/SAVEPOINT policy
    and error classification. `report=None` (the S2 path) skips the re-anchor
    census bookkeeping but still logs anchor warnings.

    `meta` is the file's already-parsed frontmatter metadata; passing it
    keeps meta and `body`/`new_hash` provably from the SAME read (S1's
    third-edit-race posture). When None it is re-read from `path` (the S2
    path passes it explicitly from the op body it just wrote)."""
    if meta is None:
        meta = frontmatter.load(str(path)).metadata or {}
    # title is NOT NULL and display-critical (an empty title
    # would break list rendering), so any falsy frontmatter
    # value (missing key, "", None) falls back to the existing
    # row title. author/summary are nullable, so they instead
    # honor explicit user intent: key absent -> keep row value,
    # key present (even as "" or null) -> overwrite with it.
    title = str(meta.get("title") or row["title"])
    author = meta["author"] if "author" in meta else row["author"]
    summary = meta["summary"] if "summary" in meta else row["summary"]
    word_count = len(body.split())
    conn.execute(
        "UPDATE articles SET title = ?, author = ?, summary = ?, "
        "word_count = ?, reading_time_min = ?, body_hash = ?, "
        "vector_status = 'pending' WHERE id = ?",
        (title, author, summary, word_count,
         max(1, math.ceil(word_count / 250)), new_hash, row["id"]),
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
            # A file can turn unreadable between the settle poll and here
            # (deleted, permissions changed, truncated mid-write again).
            # This is a pure read/extraction step — no DB writes yet — so a
            # failure here means we genuinely never touched the row.
            try:
                post = frontmatter.load(str(scan.disk[name]))
                body = post.content
                meta = post.metadata or {}
                # Recompute from the body we just read (not the settle-time
                # scan.hashes[name]) so the stored body_hash always matches
                # the exact content the other row fields were derived from —
                # closes a third-edit race between settle and apply.
                body_hash = content_hash(body)
            except Exception as e:
                logger.warning(
                    "Unreadable markdown at apply time %s: %s", name, e
                )
                _detail(report, "unreadable").append(name)
                continue

            # DB work is isolated per-file behind a SAVEPOINT so a failure
            # partway through (e.g. tag sync) rolls back everything this
            # file already did — the whole-pass conn.commit() below must
            # never sweep a half-applied file into durable state. Executed
            # as raw SQL (not sqlite3's high-level begin/commit) since the
            # connection's default deferred-transaction handling nests
            # cleanly under an explicit SAVEPOINT/RELEASE/ROLLBACK TO.
            conn.execute("SAVEPOINT apply_file")
            try:
                refresh_article_from_file(config, conn, row, scan.disk[name],
                                          body, body_hash, report, meta=meta)
            except Exception as e:
                conn.execute("ROLLBACK TO apply_file")
                conn.execute("RELEASE apply_file")
                logger.warning(
                    "apply failed for %s (rolled back, will retry next pass): %s",
                    name, e,
                )
                _detail(report, "apply_errors").append(name)
                continue
            conn.execute("RELEASE apply_file")
            report.changed += 1
            _detail(report, "changed_files").append(name)
        # Verified empirically (two-connection test against sqlite3's
        # legacy transaction handling): since this SAVEPOINT is never
        # nested inside an explicit BEGIN, SQLite treats it as the
        # outermost savepoint, so each success-path RELEASE above already
        # commits that file's writes at the SQLite level (a second
        # connection can read the update and acquire BEGIN IMMEDIATE
        # before this line ever runs). This trailing commit() is therefore
        # a no-op backstop, not the thing making the per-file writes
        # durable -- the lock window is per-file, not per-pass.
        conn.commit()
    finally:
        conn.close()


# --- external-file create pipeline ------------------------------------------


_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def _external_title(meta: dict, body: str, path: Path) -> str:
    if meta.get("title"):
        return str(meta["title"])
    m = _HEADING_RE.search(body)
    if m:
        return m.group(1).strip()
    return path.stem


def _external_source_id(conn, url: str, meta: dict) -> int:
    from urllib.parse import urlparse

    from tiro.ingestion.processor import _get_or_create_source

    domain = urlparse(url).netloc if url else ""
    if domain:
        return _get_or_create_source(conn, domain)
    name = str(meta.get("source") or "External")
    row = conn.execute(
        "SELECT id FROM sources WHERE name = ? AND domain IS NULL "
        "AND email_sender IS NULL", (name,)
    ).fetchone()
    if row:
        return row["id"]
    cursor = conn.execute(
        "INSERT INTO sources (uid, name, source_type) VALUES (?, ?, ?)",
        (new_ulid(), name, "web"),
    )
    conn.commit()
    return cursor.lastrowid


def ingest_external_file(config: TiroConfig, path: Path) -> int | None:
    """Ingest a user-created markdown file found in articles/ (S1).

    The file is the USER'S: it is never rewritten, moved, or deleted — not
    even when enrichment fails (deliberate divergence from process_article's
    rollback-via-delete_article, which would destroy user data here).
    Returns the article id, or None when skipped as a URL duplicate.
    """
    from tiro.ingestion.rss import _find_existing_article_by_url
    from tiro.stats import update_stat

    post = frontmatter.load(str(path))
    body = post.content
    meta = post.metadata or {}
    url = str(meta.get("url") or "")
    title = _external_title(meta, body, path)
    word_count = len(body.split())
    now = datetime.now()
    published = str(meta["published"]) if meta.get("published") else None

    conn = get_connection(config.db_path)
    try:
        if url and _find_existing_article_by_url(conn, url) is not None:
            return None
        source_id = _external_source_id(conn, url, meta)
        slug, n = path.stem, 2
        while conn.execute("SELECT 1 FROM articles WHERE slug = ?", (slug,)).fetchone():
            slug = f"{path.stem}-{n}"
            n += 1
        cursor = conn.execute(
            """INSERT INTO articles
               (uid, source_id, title, author, url, slug, markdown_path,
                word_count, reading_time_min, published_at, ingested_at,
                ingestion_method, body_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'external', ?)""",
            (new_ulid(), source_id, title, meta.get("author"), url, slug,
             path.name, word_count, max(1, math.ceil(word_count / 250)),
             published, now.isoformat(), content_hash(body)),
        )
        article_id = cursor.lastrowid
        try:
            from tiro.authors import link_article_author
            link_article_author(conn, article_id, meta.get("author"))
        except Exception as e:
            logger.error("link_article_author failed for %s (non-fatal): %s", path, e)
        if isinstance(meta.get("tags"), list):
            _sync_tags_from_frontmatter(conn, article_id, [str(t) for t in meta["tags"]])
        conn.commit()

        try:
            update_stat(config, "articles_saved")
        except Exception as e:
            logger.error("Failed to update reading stats: %s", e)

        # Enrichment: SQLite only — never rewrite the user's file. Non-fatal.
        try:
            from tiro.ingestion.extractors import extract_metadata
            ai = extract_metadata(title, body, config)
            if ai["summary"]:
                conn.execute("UPDATE articles SET summary = ? WHERE id = ?",
                             (ai["summary"], article_id))
            for tag_name in ai["tags"]:
                conn.execute("INSERT OR IGNORE INTO tags (uid, name) VALUES (?, ?)",
                             (new_ulid(), tag_name))
                tag_row = conn.execute("SELECT id FROM tags WHERE name = ?",
                                       (tag_name,)).fetchone()
                conn.execute(
                    "INSERT OR IGNORE INTO article_tags (article_id, tag_id) "
                    "VALUES (?, ?)", (article_id, tag_row["id"]))
            from tiro.migrations import canonical_key
            for entity in ai["entities"]:
                key = canonical_key(entity["name"])
                ent_row = conn.execute(
                    "SELECT id FROM entities WHERE entity_type = ? AND canonical_key = ?",
                    (entity["type"], key)).fetchone()
                if ent_row:
                    entity_id = ent_row["id"]
                else:
                    entity_id = conn.execute(
                        "INSERT INTO entities (uid, name, entity_type, canonical_key) "
                        "VALUES (?, ?, ?, ?)",
                        (new_ulid(), entity["name"], entity["type"], key)).lastrowid
                conn.execute(
                    "INSERT OR IGNORE INTO article_entities (article_id, entity_id) "
                    "VALUES (?, ?)", (article_id, entity_id))
            try:
                from tiro.wiki import mark_pages_stale
                mark_pages_stale(config, conn, article_id)
            except Exception as e:
                logger.error("mark_pages_stale failed for %d (non-fatal): %s",
                             article_id, e)
            conn.commit()
        except Exception as e:
            logger.error("Enrichment failed for external %s (row kept): %s", path, e)

        # ChromaDB (non-fatal: pending + retry loop, same posture as ingest).
        try:
            from tiro.vectorstore import get_collection
            source_row = conn.execute(
                "SELECT name, is_vip FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
            get_collection().upsert(
                ids=[f"article_{article_id}"],
                documents=[body],
                metadatas=[{
                    "title": title,
                    "source": source_row["name"] if source_row else "External",
                    "is_vip": bool(source_row["is_vip"]) if source_row else False,
                    "tags": "",
                    "published_at": (published or now.strftime("%Y-%m-%d"))[:10],
                    "article_id": article_id,
                }],
            )
            conn.execute("UPDATE articles SET vector_status = 'indexed' WHERE id = ?",
                         (article_id,))
            conn.commit()
        except Exception as e:
            logger.error("ChromaDB add failed for external %d (will retry): %s",
                         article_id, e)
            conn.execute("UPDATE articles SET vector_status = 'pending' WHERE id = ?",
                         (article_id,))
            conn.commit()

        # Relations (non-fatal; no LLM connection notes for external ingests).
        try:
            from tiro.search.semantic import find_related_articles, store_relations
            relations = find_related_articles(article_id, config, limit=5)
            if relations:
                store_relations(article_id, relations, config)
        except Exception as e:
            logger.error("Related articles failed for external %d: %s", article_id, e)

        return article_id
    finally:
        conn.close()


def _delete_guarded(total: int, missing: int) -> bool:
    """Refuse a pass's deletes when the missing set smells like a directory
    mishap, not an editor delete: all-missing (>1, doctor's rule) or more
    than max(MASS_DELETE_FLOOR, 20% of the library) at once (spec §4)."""
    if missing == 0:
        return False
    if missing == total and total > 1:
        return True
    return missing > max(MASS_DELETE_FLOOR, math.ceil(MASS_DELETE_FRACTION * total))


def _apply_deleted(config: TiroConfig, scan: _Scan, report: ReconcileReport,
                   dry_run: bool) -> None:
    if not scan.missing:
        return
    total = len(scan.rows_by_name)
    if _delete_guarded(total, len(scan.missing)):
        report.details["delete_guard"] = (
            f"{len(scan.missing)}/{total} article files missing — refusing to "
            "delete (directory mishap? fix library_path or run tiro doctor)"
        )
        logger.warning("Reconcile: %s", report.details["delete_guard"])
        return
    from tiro.lifecycle import delete_article

    for row in scan.missing:
        if dry_run:
            report.deleted += 1
            _detail(report, "deleted_articles").append(
                {"id": row["id"], "title": row["title"]})
            continue
        try:
            deleted = delete_article(config, row["id"])
        except Exception as e:
            logger.error(
                "Reconcile: delete_article failed for %d (%r) — will retry "
                "next pass: %s", row["id"], row["title"], e)
            _detail(report, "delete_errors").append(
                {"id": row["id"], "title": row["title"]})
            continue
        if deleted:
            report.deleted += 1
            _detail(report, "deleted_articles").append(
                {"id": row["id"], "title": row["title"]})
            logger.info("Reconcile: completed deletion of article %d (%r) — "
                        "markdown removed externally", row["id"], row["title"])


def _apply_created(config: TiroConfig, scan: _Scan, report: ReconcileReport,
                   dry_run: bool) -> None:
    for name in scan.created:
        if dry_run:
            report.ingested += 1
            _detail(report, "ingested_files").append(name)
            continue
        try:
            article_id = ingest_external_file(config, scan.disk[name])
        except Exception as e:
            logger.error("External ingest failed for %s (file untouched): %s", name, e)
            _detail(report, "ingest_errors").append(name)
            continue
        if article_id is None:
            _detail(report, "skipped_duplicates").append(name)
        else:
            report.ingested += 1
            _detail(report, "ingested_files").append(name)
            logger.info("Ingested external file %s as article %d", name, article_id)


# --- sidecar (highlights/notes) reconcile -----------------------------------


def write_conflict_file(dir_path: Path, stem: str, body: str, *,
                        device: str = "local") -> Path:
    """Preserve a losing version as {stem}.conflict-{device}-{yyyymmdd}[-n].md
    (spec §4 naming; S1 passes the default 'local', S2's merge passes the
    losing op's device label). Collision-safe; never overwrites. `device` is
    sanitized to the `_CONFLICT_RE` alphabet ([A-Za-z0-9]) so the file always
    round-trips as a conflict file (excluded from ingest/orphan handling).

    Same-content dedupe (D19#2): before minting a new file, the existing
    {stem}.conflict-* files are scanned and a byte-identical one is returned
    as-is. A re-applied journal segment (crash between apply and watermark
    persist) must not mint duplicate conflict files — the dedupe makes
    conflict-file creation idempotent."""
    device = re.sub(r"[^A-Za-z0-9]", "", device) or "peer"
    if dir_path.is_dir():
        prefix = f"{stem}.conflict-"
        for existing in sorted(dir_path.iterdir()):
            if not existing.name.startswith(prefix) or not existing.is_file():
                continue
            try:
                if existing.read_text() == body:
                    return existing
            except OSError:  # unreadable candidate never blocks preservation
                continue
    dir_path.mkdir(parents=True, exist_ok=True)
    day = datetime.now(UTC).strftime("%Y%m%d")
    dest = dir_path / f"{stem}.conflict-{device}-{day}.md"
    n = 2
    while dest.exists():
        dest = dir_path / f"{stem}.conflict-{device}-{day}-{n}.md"
        n += 1
    dest.write_text(body)
    return dest


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _notes_conflict_prepass(config: TiroConfig, report: ReconcileReport) -> None:
    """FROZEN rule: notes prefer the external version when ambiguous — but
    never silently. When the article-note ROW both differs from the file and
    leads its mtime by > ROW_LEAD_SLACK_SECONDS (sidecar-first writes put the
    row a hair after the file, hence the slack), the DB version is preserved
    as a conflict file BEFORE reconcile_annotations applies files-win."""
    from tiro.annotations import notes_dir, sidecar_stem

    nt_dir = notes_dir(config)
    if not nt_dir.exists():
        return
    conn = get_connection(config.db_path)
    try:
        articles = conn.execute(
            "SELECT id, uid, markdown_path FROM articles").fetchall()
        stem_to_article = {sidecar_stem(a): a for a in articles}
        for path in sorted(nt_dir.glob("*.md")):
            if is_conflict_file(path.name):
                continue
            article = stem_to_article.get(path.stem)
            if article is None:
                continue  # reconcile_annotations orphans it
            row = conn.execute(
                "SELECT body_markdown, updated_at FROM notes "
                "WHERE article_id = ? AND highlight_id IS NULL",
                (article["id"],),
            ).fetchone()
            if row is None:
                continue
            try:
                file_body = path.read_text()
            except (OSError, UnicodeDecodeError):
                continue  # reconcile_annotations counts it unreadable
            # Exact-string coupling: this compares the raw on-disk bytes to
            # the DB's stored body_markdown verbatim. Any future
            # newline/whitespace normalization divergence between this
            # write path and the note write paths (API, importers) could
            # make an unmodified note look "changed" and spuriously emit a
            # conflict file here. The equality short-circuit plus the
            # ROW_LEAD_SLACK_SECONDS check below mitigate false positives
            # in the common case but don't eliminate this class of bug.
            if row["body_markdown"] == file_body:
                continue
            row_ts = _parse_ts(row["updated_at"] or "")
            file_ts = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if row_ts is None:
                continue
            if (row_ts - file_ts).total_seconds() > ROW_LEAD_SLACK_SECONDS:
                dest = write_conflict_file(nt_dir, path.stem, row["body_markdown"])
                report.conflicts += 1
                _detail(report, "conflict_files").append(str(dest))
                logger.warning(
                    "Note conflict for %s: external version kept, prior DB "
                    "version preserved at %s", path.stem, dest.name)
    finally:
        conn.close()


def _reconcile_sidecars(config: TiroConfig, report: ReconcileReport,
                        dry_run: bool) -> None:
    if dry_run:
        report.details["annotations"] = "skipped (dry-run)"
        return
    _notes_conflict_prepass(config, report)
    from tiro.annotations import reconcile_annotations

    counts = reconcile_annotations(config)
    report.details["annotations"] = counts
    if counts.get("guarded"):
        logger.warning(
            "Reconcile: annotations mass-delete guard fired (%d) — sidecar "
            "rows preserved; run tiro doctor", counts["guarded"])


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
    _apply_created(config, scan, report, dry_run)
    _apply_deleted(config, scan, report, dry_run)
    _reconcile_sidecars(config, report, dry_run)
    return report
