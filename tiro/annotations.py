"""Highlights + notes sidecar store (Phase 2 M2.1): files-as-truth.

`{library}/annotations/{stem}.jsonl` (one line per highlight, the
highlight's own note inlined via `note_markdown`) and `{library}/notes/
{stem}.md` (the whole-file, article-level note) are the source of truth.
`stem` is the article's `markdown_path` stem (same article, same base
filename, three extensions: `.md` for the article, `.jsonl` for its
highlights, `.md` under `notes/` for its article-level note).

The `highlights` + `notes` tables (migration 009) are a *derived* index --
a queryable cache reconciled files-win by `reconcile_annotations()`
(doctor's job, wired into `create_app`'s lifespan next to
`reconcile_wiki_index`). Nothing in this module ever treats the derived
tables as authoritative for content; only for fast listing/joins. This
mirrors `tiro/wiki.py` deliberately -- same files-win precedent, same
"reconcile never writes/deletes content files, only moves true orphans"
rule, same never-crash-the-caller posture on malformed input.

Two note "kinds" share the `notes` table, disambiguated by `highlight_id`:
  - Highlight-anchored note: lives inside its highlight's JSONL line, in
    the `note_markdown` field. Row has `highlight_id` set. Identity is the
    highlight's uid (there's no separate uid *in the file* for this kind
    of note -- the row's own `uid` is minted at insert and kept stable
    across updates by matching on `highlight_id`, not by anything the file
    stores for the note itself).
  - Article-level note: the whole `notes/{stem}.md` file. Row has
    `highlight_id IS NULL`. Identity is `(article_id, highlight_id IS
    NULL)` -- at most one such row per article -- since the file has no
    frontmatter/uid of its own to key on.

JSONL line schema (`_FIELD_ORDER` below is the stable on-disk field
order, enforced by `write_annotations` regardless of caller dict order):
`uid, article_uid, quote, prefix, suffix, position_start, position_end,
content_hash, color, note_markdown (nullable), created_at, updated_at`.

`rebuild_sidecars_for_article()` is the index-→files direction (used by
import/repair: regenerate a stem's sidecars from what's currently in
SQLite). `reconcile_annotations()` is the primary files-→index direction
(files win): T3's sidecar-first CRUD writes do NOT go through either of
these -- they write files directly then update rows, using the small read/
write primitives here (`read_annotations`/`write_annotations`/`read_note`/
`write_note`/`delete_note`) as their plumbing.

**Stem-wins / uid-mismatch rule.** Sidecars are matched to articles by
filename STEM only (`sidecar_stem()`, derived from `markdown_path`) -- a
JSONL line's own `article_uid` field is informational, never authoritative
for routing. If a hand-edited line's `article_uid` disagrees with the
stem-resolved article's actual uid, the stem still wins: the line is
indexed under the stem-resolved article regardless, and the disagreement
is only logged and counted (`uid_mismatch_lines`), never used to relocate
the line or rewrite the file. This matches the module's broader
never-rewrite-sidecar-content posture -- reconcile corrects DERIVED rows,
never file content, even to fix an inconsistency within the file itself.

**Global stem uniqueness assumption.** Because sidecars are keyed purely by
stem, `markdown_path` stems are assumed globally unique across the whole
library. This is a pre-existing assumption carried over from the rest of
the codebase (article filenames, wiki page slugs are also stem/slug-keyed),
not something new introduced by this module.
"""

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.migrations import new_ulid

logger = logging.getLogger(__name__)

_FIELD_ORDER = (
    "uid",
    "article_uid",
    "quote",
    "prefix",
    "suffix",
    "position_start",
    "position_end",
    "content_hash",
    "color",
    "note_markdown",
    "created_at",
    "updated_at",
)


def annotations_dir(config: TiroConfig) -> Path:
    """`{library}/annotations/` -- one `{stem}.jsonl` per article with at
    least one highlight. Not created here; callers that write create it
    lazily (`write_annotations`), same as `wiki_dir` was pre-W1."""
    return config.library / "annotations"


def notes_dir(config: TiroConfig) -> Path:
    """`{library}/notes/` -- one `{stem}.md` per article with an
    article-level note. Created lazily by `write_note`."""
    return config.library / "notes"


def sidecar_stem(article_row_or_markdown_path) -> str:
    """Derive the sidecar stem (base filename, no extension) from either an
    article row/mapping (anything supporting `row["markdown_path"]` --
    `sqlite3.Row`, a plain dict, ...) or a raw markdown_path string/Path.

    Accepting both means a call site that already has a fetched article
    row can pass it straight through (no extra `row["markdown_path"]`
    boilerplate at every call site), while reconcile's per-migration dicts
    and any raw-path caller work too."""
    if isinstance(article_row_or_markdown_path, (str, Path)):
        markdown_path = article_row_or_markdown_path
    else:
        markdown_path = article_row_or_markdown_path["markdown_path"]
    return Path(markdown_path).stem


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_text(path: Path, text: str) -> None:
    """Temp file + os.replace, same pattern as `config.persist_config` /
    `tiro/wiki.py` page writes -- never leaves a half-written sidecar."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp_path.write_text(text)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _parse_jsonl_lines(path: Path) -> tuple[list[dict], int]:
    """Parse `path` line-by-line. Malformed lines (invalid JSON, non-object,
    or missing the required `uid`) are skipped and counted, never raised --
    one corrupt line can't block the rest of the file or crash the caller.
    The file itself is never rewritten by this function."""
    lines: list[dict] = []
    malformed = 0
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict) or not obj.get("uid"):
                raise ValueError("not an object with a 'uid' field")
        except Exception as e:
            logger.warning("Skipping malformed annotation line %d in %s: %s", lineno, path, e)
            malformed += 1
            continue
        lines.append(obj)
    return lines, malformed


def read_annotations(config: TiroConfig, stem: str) -> list[dict]:
    """Read `{annotations}/{stem}.jsonl`. Missing file -> `[]`. Malformed
    lines are skipped (logged), never raised."""
    path = annotations_dir(config) / f"{stem}.jsonl"
    if not path.exists():
        return []
    lines, _malformed = _parse_jsonl_lines(path)
    return lines


def _ordered_line(line: dict) -> dict:
    """Project `line` onto the stable `_FIELD_ORDER` -- unknown keys are
    dropped, missing keys become `None`, and the ON-DISK field order is
    always the same regardless of the order the caller's dict happened to
    have (Python dicts preserve insertion order, and so does `json.dumps`,
    so building the dict in `_FIELD_ORDER` order is sufficient)."""
    return {k: line.get(k) for k in _FIELD_ORDER}


def write_annotations(config: TiroConfig, stem: str, lines: list[dict]) -> None:
    """Atomically (over)write `{annotations}/{stem}.jsonl`, one JSON object
    per line, stable field order. `lines=[]` writes an empty file -- callers
    that want the file removed instead (e.g. "no highlights left") must
    unlink it themselves; this is a pure write primitive, not a policy."""
    path = annotations_dir(config) / f"{stem}.jsonl"
    text = "".join(json.dumps(_ordered_line(line), ensure_ascii=False) + "\n" for line in lines)
    _atomic_write_text(path, text)


def read_note(config: TiroConfig, stem: str) -> str | None:
    """Read `{notes}/{stem}.md`. Missing file -> `None`."""
    path = notes_dir(config) / f"{stem}.md"
    if not path.exists():
        return None
    return path.read_text()


def write_note(config: TiroConfig, stem: str, body_markdown: str) -> None:
    """Atomically (over)write `{notes}/{stem}.md`."""
    path = notes_dir(config) / f"{stem}.md"
    _atomic_write_text(path, body_markdown)


def delete_note(config: TiroConfig, stem: str) -> None:
    """Remove `{notes}/{stem}.md` if present. No-op if already absent."""
    path = notes_dir(config) / f"{stem}.md"
    path.unlink(missing_ok=True)


def _move_to_orphaned(config: TiroConfig, path: Path) -> Path:
    """Move a sidecar file whose stem matches no known article to
    `{library}/.orphaned/`, collision-safe (never overwrites an existing
    file there) -- same pattern as `tiro/doctor.py fix()`'s orphaned-
    markdown handling. Never deletes; the file is preserved."""
    orphan_dir = config.library / ".orphaned"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    dest = orphan_dir / path.name
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        n = 1
        while (orphan_dir / f"{stem}.{n}{suffix}").exists():
            n += 1
        dest = orphan_dir / f"{stem}.{n}{suffix}"
    path.rename(dest)
    logger.warning("Moved orphaned annotation sidecar %s -> .orphaned/%s", path.name, dest.name)
    return dest


def rebuild_sidecars_for_article(config: TiroConfig, article_id: int) -> None:
    """Index -> files direction: regenerate BOTH of an article's sidecars
    (the `.jsonl` highlight file and the `.md` article-level note) purely
    from what's currently in SQLite. Used by import/repair -- the opposite
    direction from `reconcile_annotations()` (files win). Not on T3's
    sidecar-first write path (which writes files directly, then updates
    rows), but a clean primitive for anything that needs to (re)materialize
    sidecars from the index -- e.g. restoring a bundle whose sidecars
    didn't survive the transfer."""
    conn = get_connection(config.db_path)
    try:
        article = conn.execute(
            "SELECT id, uid, markdown_path FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if article is None:
            raise ValueError(f"no article with id {article_id}")
        stem = sidecar_stem(article)

        highlight_rows = conn.execute(
            "SELECT * FROM highlights WHERE article_id = ? ORDER BY id", (article_id,)
        ).fetchall()
        lines = []
        for h in highlight_rows:
            note_row = conn.execute(
                "SELECT body_markdown FROM notes WHERE highlight_id = ?", (h["id"],)
            ).fetchone()
            lines.append(
                {
                    "uid": h["uid"],
                    "article_uid": article["uid"],
                    "quote": h["quote_text"],
                    "prefix": h["prefix_context"],
                    "suffix": h["suffix_context"],
                    "position_start": h["text_position_start"],
                    "position_end": h["text_position_end"],
                    "content_hash": h["content_hash"],
                    "color": h["color"],
                    "note_markdown": note_row["body_markdown"] if note_row else None,
                    "created_at": h["created_at"],
                    "updated_at": h["updated_at"],
                }
            )

        if lines:
            write_annotations(config, stem, lines)
        else:
            (annotations_dir(config) / f"{stem}.jsonl").unlink(missing_ok=True)

        article_note = conn.execute(
            "SELECT body_markdown FROM notes WHERE article_id = ? AND highlight_id IS NULL",
            (article_id,),
        ).fetchone()
        if article_note:
            write_note(config, stem, article_note["body_markdown"])
        else:
            delete_note(config, stem)
    finally:
        conn.close()


def _highlight_drifted(row, line: dict) -> bool:
    """True if any files-win-controlled column differs between the derived
    row and the file's line. `created_at` is treated as set-once-at-insert
    and never compared/overwritten by drift correction."""
    return (
        row["quote_text"] != line.get("quote")
        or row["prefix_context"] != line.get("prefix")
        or row["suffix_context"] != line.get("suffix")
        or row["text_position_start"] != line.get("position_start")
        or row["text_position_end"] != line.get("position_end")
        or row["content_hash"] != line.get("content_hash")
        or (row["color"] or "yellow") != (line.get("color") or "yellow")
    )


def _reconcile_highlight_note(conn, article_id: int, highlight_id: int, note_markdown, counts, now):
    existing_note = conn.execute(
        "SELECT * FROM notes WHERE highlight_id = ?", (highlight_id,)
    ).fetchone()
    if note_markdown is None:
        if existing_note is not None:
            conn.execute("DELETE FROM notes WHERE id = ?", (existing_note["id"],))
            counts["notes_deleted"] += 1
        return
    if existing_note is None:
        conn.execute(
            "INSERT INTO notes (uid, article_id, highlight_id, body_markdown, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (new_ulid(), article_id, highlight_id, note_markdown, now, now),
        )
        counts["notes_inserted"] += 1
    elif existing_note["body_markdown"] != note_markdown:
        conn.execute(
            "UPDATE notes SET body_markdown = ?, updated_at = ? WHERE id = ?",
            (note_markdown, now, existing_note["id"]),
        )
        counts["notes_updated"] += 1
    else:
        counts["notes_matched"] += 1


def _reconcile_highlight_rows_for_article(conn, article, lines: list[dict], counts) -> None:
    article_id = article["id"]
    existing = {
        r["uid"]: r
        for r in conn.execute(
            "SELECT * FROM highlights WHERE article_id = ?", (article_id,)
        ).fetchall()
    }
    seen_uids: set[str] = set()
    now = _now_iso()

    for line in lines:
        uid = line.get("uid")
        seen_uids.add(uid)
        line_article_uid = line.get("article_uid")
        if line_article_uid and line_article_uid != article["uid"]:
            # Stem wins (see module docstring): index under the
            # stem-resolved article regardless, just log + count the
            # disagreement. Never relocate the line or touch the file.
            logger.warning(
                "Annotation line uid=%s claims article_uid=%s but its stem "
                "resolves to article uid=%s -- indexing under the "
                "stem-resolved article (stem wins)",
                uid, line_article_uid, article["uid"],
            )
            counts["uid_mismatch_lines"] += 1
        note_markdown = line.get("note_markdown")
        row = existing.get(uid)
        if row is None:
            cur = conn.execute(
                """INSERT INTO highlights
                   (uid, article_id, quote_text, prefix_context, suffix_context,
                    text_position_start, text_position_end, content_hash, color,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    uid,
                    article_id,
                    line.get("quote"),
                    line.get("prefix"),
                    line.get("suffix"),
                    line.get("position_start"),
                    line.get("position_end"),
                    line.get("content_hash"),
                    line.get("color") or "yellow",
                    line.get("created_at") or now,
                    line.get("updated_at") or now,
                ),
            )
            highlight_id = cur.lastrowid
            counts["highlights_inserted"] += 1
        else:
            highlight_id = row["id"]
            if _highlight_drifted(row, line):
                conn.execute(
                    """UPDATE highlights
                       SET quote_text = ?, prefix_context = ?, suffix_context = ?,
                           text_position_start = ?, text_position_end = ?,
                           content_hash = ?, color = ?, updated_at = ?
                       WHERE id = ?""",
                    (
                        line.get("quote"),
                        line.get("prefix"),
                        line.get("suffix"),
                        line.get("position_start"),
                        line.get("position_end"),
                        line.get("content_hash"),
                        line.get("color") or "yellow",
                        line.get("updated_at") or now,
                        highlight_id,
                    ),
                )
                counts["highlights_updated"] += 1
            else:
                counts["highlights_matched"] += 1

        _reconcile_highlight_note(conn, article_id, highlight_id, note_markdown, counts, now)

    vanished = set(existing) - seen_uids
    for uid in vanished:
        row = existing[uid]
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM notes WHERE highlight_id = ?", (row["id"],)
        ).fetchone()["n"]
        conn.execute("DELETE FROM notes WHERE highlight_id = ?", (row["id"],))
        counts["notes_deleted"] += n
        conn.execute("DELETE FROM highlights WHERE id = ?", (row["id"],))
        counts["highlights_deleted"] += 1


def annotations_mass_delete_guard(
    dir_exists: bool, stems_with_rows: set[str], file_stems: set[str]
) -> bool:
    """Shared mass-deletion guard predicate, one sidecar kind at a time
    (highlights' `annotations/` dir or notes' `notes/` dir -- call it once
    per kind). Used by BOTH `reconcile_annotations()` (the real repair) and
    `tiro/doctor.py scan()`'s cheap pre-check, so the two can never drift
    apart on what counts as guard-worthy (a prior gap: scan() only checked
    the whole-directory-missing case, while reconcile also refused when the
    directory exists but is empty relative to every stem-with-rows).

    True when applying files-win literally would wipe out every row for
    this sidecar kind, in a way that looks like a directory mishap rather
    than a legitimate delete:
      - the directory is missing entirely while any stem has rows, or
      - the directory exists but contains NO matching file for MORE THAN
        ONE stem-with-rows (a present-but-effectively-empty directory,
        e.g. a botched restore, is just as dangerous as an absent one). A
        single-stem library is exempt -- deleting that one stem's rows
        when its lone sidecar vanished is the ordinary, legitimate
        files-win case, not a mishap.

    `file_stems` is only consulted when `dir_exists` is True."""
    if not stems_with_rows:
        return False
    if not dir_exists:
        return True
    missing = stems_with_rows - file_stems
    return bool(missing) and len(missing) == len(stems_with_rows) and len(stems_with_rows) > 1


def _resolve_missing_stems(rows_article_ids, id_to_article: dict, file_stems: set[str]) -> list:
    """Of `rows_article_ids` (articles with derived rows in the table under
    reconciliation), return the subset whose stem-resolved sidecar has no
    matching entry in `file_stems`. An id with no matching article row
    (`id_to_article.get()` -> None -- references a since-deleted article)
    is skipped, not this function's concern, same as before."""
    missing = []
    for article_id in rows_article_ids:
        article = id_to_article.get(article_id)
        if article is None:
            continue
        if sidecar_stem(article) not in file_stems:
            missing.append(article_id)
    return missing


def _reconcile_highlights(config: TiroConfig, conn, id_to_article: dict, counts: dict) -> None:
    ann_dir = annotations_dir(config)
    dir_exists = ann_dir.exists()
    file_stems: set[str] = set()
    stem_to_article = {sidecar_stem(a): a for a in id_to_article.values()}

    if dir_exists:
        for path in sorted(ann_dir.glob("*.jsonl")):
            stem = path.stem
            article = stem_to_article.get(stem)
            if article is None:
                _move_to_orphaned(config, path)
                counts["orphaned_files"] += 1
                continue
            try:
                lines, malformed = _parse_jsonl_lines(path)
            except (OSError, UnicodeDecodeError) as e:
                # Unreadable != absent: skip this article entirely (no row
                # deletions for it) rather than let one bad file abort the
                # whole reconcile or get treated as "the file is gone".
                logger.warning("Skipping unreadable annotation sidecar %s: %s", path, e)
                counts["unreadable_files"] += 1
                file_stems.add(stem)
                continue
            file_stems.add(stem)
            counts["malformed_lines"] += malformed
            _reconcile_highlight_rows_for_article(conn, article, lines, counts)

    highlight_article_ids = {
        r["article_id"]
        for r in conn.execute("SELECT DISTINCT article_id FROM highlights").fetchall()
    }
    stems_with_rows = {
        sidecar_stem(id_to_article[aid])
        for aid in highlight_article_ids
        if aid in id_to_article
    }

    # Mass-deletion guard (shared with `tiro/doctor.py scan()`'s cheap
    # pre-check via `annotations_mass_delete_guard` -- see its docstring):
    # covers both "the annotations/ directory is missing entirely" and "the
    # directory exists but is empty relative to every stem with rows".
    # Files-win would otherwise wipe every highlight row in the library on
    # what's really a directory mishap -- report instead, mirroring
    # `tiro/doctor.py fix()`'s all-articles-missing guard for markdown.
    if annotations_mass_delete_guard(dir_exists, stems_with_rows, file_stems):
        counts["guarded"] += 1
        return

    if not dir_exists:
        return

    missing_ids = _resolve_missing_stems(highlight_article_ids, id_to_article, file_stems)

    for article_id in missing_ids:
        highlight_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM highlights WHERE article_id = ?", (article_id,)
            ).fetchall()
        ]
        if not highlight_ids:
            continue
        placeholders = ",".join("?" for _ in highlight_ids)
        n = conn.execute(
            f"SELECT COUNT(*) AS n FROM notes WHERE highlight_id IN ({placeholders})",
            highlight_ids,
        ).fetchone()["n"]
        conn.execute(f"DELETE FROM notes WHERE highlight_id IN ({placeholders})", highlight_ids)
        counts["notes_deleted"] += n
        cur = conn.execute("DELETE FROM highlights WHERE article_id = ?", (article_id,))
        counts["highlights_deleted"] += cur.rowcount


def _reconcile_article_notes(config: TiroConfig, conn, id_to_article: dict, counts: dict) -> None:
    nt_dir = notes_dir(config)
    dir_exists = nt_dir.exists()
    file_stems: set[str] = set()
    stem_to_article = {sidecar_stem(a): a for a in id_to_article.values()}
    now = _now_iso()

    if dir_exists:
        for path in sorted(nt_dir.glob("*.md")):
            stem = path.stem
            article = stem_to_article.get(stem)
            if article is None:
                _move_to_orphaned(config, path)
                counts["orphaned_files"] += 1
                continue
            try:
                body = path.read_text()
            except (OSError, UnicodeDecodeError) as e:
                # Unreadable != absent: skip this article entirely (no row
                # deletions for it), same posture as the highlights side.
                logger.warning("Skipping unreadable note sidecar %s: %s", path, e)
                counts["unreadable_files"] += 1
                file_stems.add(stem)
                continue
            file_stems.add(stem)
            existing = conn.execute(
                "SELECT * FROM notes WHERE article_id = ? AND highlight_id IS NULL",
                (article["id"],),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO notes (uid, article_id, highlight_id, body_markdown,"
                    " created_at, updated_at) VALUES (?, ?, NULL, ?, ?, ?)",
                    (new_ulid(), article["id"], body, now, now),
                )
                counts["notes_inserted"] += 1
            elif existing["body_markdown"] != body:
                conn.execute(
                    "UPDATE notes SET body_markdown = ?, updated_at = ? WHERE id = ?",
                    (body, now, existing["id"]),
                )
                counts["notes_updated"] += 1
            else:
                counts["notes_matched"] += 1

    note_article_ids = {
        r["article_id"]
        for r in conn.execute(
            "SELECT DISTINCT article_id FROM notes WHERE highlight_id IS NULL"
        ).fetchall()
    }
    stems_with_rows = {
        sidecar_stem(id_to_article[aid]) for aid in note_article_ids if aid in id_to_article
    }

    # Same shared mass-deletion guard as `_reconcile_highlights`, for the
    # notes/ directory: report, don't delete every article-level note row.
    if annotations_mass_delete_guard(dir_exists, stems_with_rows, file_stems):
        counts["guarded"] += 1
        return

    if not dir_exists:
        return

    missing_ids = _resolve_missing_stems(note_article_ids, id_to_article, file_stems)

    for article_id in missing_ids:
        cur = conn.execute(
            "DELETE FROM notes WHERE article_id = ? AND highlight_id IS NULL", (article_id,)
        )
        counts["notes_deleted"] += cur.rowcount


def reconcile_annotations(config: TiroConfig) -> dict:
    """Rebuild `highlights`/`notes` rows from `annotations/*.jsonl` +
    `notes/*.md` on disk (files win) -- mirrors `tiro/wiki.py
    reconcile_wiki_index()`. NEVER writes or deletes sidecar file CONTENT;
    the only file-system mutation is moving true orphans (stem matches no
    article) into `{library}/.orphaned/`.

    Per stem, matched via the article's `markdown_path`:
      - highlight rows are matched to JSONL lines by `uid`: missing ->
        inserted, present-but-differing -> updated to the file's values,
        present-in-row-but-vanished-from-file -> deleted (along with any
        note anchored to that highlight).
      - the highlight-anchored note (the line's `note_markdown`) is synced
        the same way, keyed by the highlight's row id (not a separate uid
        -- see module docstring).
      - the article-level note (`notes/{stem}.md` existence + content) is
        synced against the one row with `highlight_id IS NULL` for that
        article.
      - malformed JSONL lines are skipped, counted, and logged -- never
        raised, and the file is never rewritten to "fix" them.
      - a line whose `article_uid` disagrees with the stem-resolved
        article's actual uid is still indexed under the stem-resolved
        article (stem wins, see module docstring) -- the disagreement is
        only logged and counted (`uid_mismatch_lines`), never used to
        relocate the line or touch the file.
      - a sidecar file that can't be read at all (permissions, decode
        error, ...) is skipped entirely -- unreadable is treated like
        malformed content, not like "the file is gone": no rows are
        deleted for that article, the failure is logged and counted
        (`unreadable_files`), and reconcile continues with every other
        article rather than letting one bad file abort the whole run.
      - a sidecar whose stem matches no known article is moved to
        `.orphaned/`, collision-safe.
      - if the `annotations/` (resp. `notes/`) directory itself does not
        exist AT ALL while highlight (resp. article-level note) rows exist,
        this is treated as a directory mishap, not "the user deleted every
        highlight" -- rows are left untouched and a guard is counted
        instead of a mass deletion (mirrors doctor's all-articles-missing
        guard for markdown). The SAME guard also fires when the directory
        DOES exist but contains zero matching files for MORE THAN ONE
        article with rows (e.g. `rm -rf annotations/*`, a botched restore)
        -- a present-but-effectively-empty directory is just as dangerous
        as an absent one. A single article whose lone sidecar vanished
        while the directory is otherwise present/populated is NOT guarded
        -- that's the ordinary, legitimate files-win delete.

    Returns counts:
      `highlights_matched`/`inserted`/`updated`/`deleted`,
      `notes_matched`/`inserted`/`updated`/`deleted` (covers both note
      kinds combined), `orphaned_files` (sidecars moved to `.orphaned/`,
      both directories combined), `malformed_lines` (JSONL lines skipped),
      `unreadable_files` (sidecars that raised on read, both directories
      combined), `uid_mismatch_lines` (JSONL lines whose `article_uid`
      disagreed with the stem-resolved article), `guarded` (mass-deletion
      guard events: 0, 1, or 2 -- one per directory that was either
      missing, or present-but-empty relative to >1 article's rows)."""
    counts = {
        "highlights_matched": 0,
        "highlights_inserted": 0,
        "highlights_updated": 0,
        "highlights_deleted": 0,
        "notes_matched": 0,
        "notes_inserted": 0,
        "notes_updated": 0,
        "notes_deleted": 0,
        "orphaned_files": 0,
        "malformed_lines": 0,
        "unreadable_files": 0,
        "uid_mismatch_lines": 0,
        "guarded": 0,
    }

    conn = get_connection(config.db_path)
    try:
        articles = conn.execute("SELECT id, uid, markdown_path FROM articles").fetchall()
        id_to_article = {row["id"]: row for row in articles}

        _reconcile_highlights(config, conn, id_to_article, counts)
        _reconcile_article_notes(config, conn, id_to_article, counts)

        conn.commit()
    finally:
        conn.close()

    return counts
