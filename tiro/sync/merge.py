"""Op application + conflict rules (sync S2 — pure merge core, spec §4).

apply_ops folds journal ops into the local library through the existing
coordinators: file writes are atomic (temp+rename), article rows refresh via
tiro/sync/reconcile.refresh_article_from_file, deletes go through
lifecycle.delete_article, sidecars are written file-first. ZERO network
(test-enforced: monkeypatched socket in tests/test_sync_properties.py).

Concurrency model (plan decision #8): sync_shadow holds the last-synced
hash+hlc per entry. Per file op: stale (hlc <= shadow hlc) -> skip;
object_hash == current -> no-op fast-forward; base_hash == current ->
clean fast-forward; else TRUE CONCURRENCY -> LWW by HLC (local side =
shadow hlc if unedited, else a fresh local tick), loser preserved as a
conflict file. Applied ops advance the shadow, so a losing op can never
win later (monotone convergence).

HASH SPACES (S2.3 review Major #1 — hydrate_bodies' docstring is the
contract): for ARTICLES, op.object_hash is the FULL-file blob address while
base_hash and everything in sync_shadow/manifest are BODY-space
(frontmatter-stripped). An article's current body hash is therefore never
compared against op.object_hash, and article shadow rows always store the
BODY-space hash. For notes/wiki/pathfiles the two spaces coincide.

Stats are NEVER touched (tiro import precedent); reading_sessions/audio/
Chroma are never written (vector_status='pending' + existing retry loop).
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import frontmatter

from tiro.anchors import content_hash
from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.migrations import new_ulid
from tiro.sync.journal import (
    HLC,
    Alias,
    FileDel,
    FilePut,
    HLCClock,
    LineDel,
    LinePut,
    Meta,
    RowDel,
    RowPut,
    canonical_json,
)
from tiro.sync.manifest import (
    _ROW_COLUMNS,
    LINK_TABLES,
    META_FIELDS,
    ROW_TABLES,
    _fields_hash,
    shadow_tombstone,
    shadow_upsert,
)
from tiro.sync.reconcile import refresh_article_from_file, write_conflict_file

logger = logging.getLogger(__name__)

MASS_DELETE_FLOOR = 10
MASS_DELETE_FRACTION = 0.2

_ALLOWED_ROOTS = ("articles", "notes", "wiki")

# Meta/row/link apply targets (Task 6): single-sourced from manifest.py's
# sync-set definitions (ROW_TABLES / LINK_TABLES / META_FIELDS /
# _ROW_COLUMNS) — duplicating the tuples here would let the manifest and
# apply sides drift. META_FIELDS doubles as the SQL-identifier allowlist
# (_apply_meta interpolates op.field only after membership passes).
_LINK_SIDES = {
    # table -> (a-side table, a fk col, b-side table, b fk col, extra cols)
    "article_tags": ("articles", "article_id", "tags", "tag_id", ()),
    "article_entities": ("articles", "article_id", "entities", "entity_id", ()),
    "article_authors": ("articles", "article_id", "authors", "author_id", ()),
    "article_relations": ("articles", "article_id", "articles",
                          "related_article_id",
                          ("similarity_score", "connection_note")),
}


@dataclass
class ApplyReport:
    """Per plan decision #11. emitted_ops carries ops the merge itself
    generated (dedupe aliases) for the engine to journal on push."""
    applied: int = 0
    skipped_stale: int = 0
    conflicts: int = 0
    resurrected: int = 0
    deferred: int = 0
    errors: int = 0
    tombstones: int = 0
    guard: str | None = None
    by_kind: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)
    emitted_ops: list = field(default_factory=list)

    def _count(self, op, action: str, **extra) -> None:
        kind = type(op).kind
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1
        self.details.setdefault(action, []).append(
            {"kind": kind, "uid": op.uid, **extra})

    def as_dict(self) -> dict:
        from dataclasses import asdict

        from tiro.sync.journal import op_to_wire

        d = asdict(self)
        d["emitted_ops"] = [op_to_wire(op)[0] for op in self.emitted_ops]
        return d


def _resolve_path(config: TiroConfig, path_hint: str) -> Path:
    """Library-relative path_hint -> absolute path, refusing escapes."""
    p = Path(path_hint)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValueError(f"bad path_hint: {path_hint!r}")
    if p.parts[0] not in _ALLOWED_ROOTS:
        raise ValueError(f"path_hint outside sync roots: {path_hint!r}")
    return config.library / p


def _atomic_write(path: Path, text: str) -> None:
    from tiro.annotations import _atomic_write_text
    _atomic_write_text(path, text)


def _shadow_get(conn, kind: str, uid: str):
    return conn.execute(
        "SELECT hash, hlc, deleted_at FROM sync_shadow WHERE kind = ? AND uid = ?",
        (kind, uid),
    ).fetchone()


def _entry_kind_for_file(path_hint: str, uid: str) -> str:
    if uid.startswith("path:"):
        return "pathfile"
    root = Path(path_hint).parts[0]
    return {"articles": "article", "notes": "note", "wiki": "wiki"}[root]


def apply_ops(config: TiroConfig, ops: list, *, guard: bool = True,
              clock: HLCClock | None = None) -> ApplyReport:
    """FROZEN signature (+ optional clock kw). Never raises on a bad op —
    per-op failures land in report.errors; only the mass-delete guard
    halts the whole batch (applying NOTHING, spec §4)."""
    report = ApplyReport()
    clock = clock or HLCClock("local")
    for op in ops:  # engine-parity: fold every remote stamp into the clock
        clock.observe(op.hlc)

    if guard:
        msg = _mass_delete_guard(config, ops)
        if msg:
            report.guard = msg
            logger.warning("Sync apply guarded: %s — nothing applied; "
                           "rerun via tiro sync --accept-mass-delete (S5)", msg)
            return report

    wiki_touched = False
    for op in _ordered(ops):
        try:
            if isinstance(op, FilePut):
                wiki_touched |= _apply_file_put(config, op, report, clock)
            elif isinstance(op, FileDel):
                wiki_touched |= _apply_file_del(config, op, report)
            elif isinstance(op, LinePut):
                _apply_line_put(config, op, report)      # Task 5
            elif isinstance(op, LineDel):
                _apply_line_del(config, op, report)      # Task 5
            elif isinstance(op, Meta):
                _apply_meta(config, op, report)          # Task 6
            elif isinstance(op, RowPut):
                _apply_row_put(config, op, report)       # Task 6
            elif isinstance(op, RowDel):
                _apply_row_del(config, op, report)       # Task 6
            elif isinstance(op, Alias):
                _apply_alias(config, op, report)         # Task 7
        except Exception as e:
            report.errors += 1
            report._count(op, "errors", error=str(e))
            logger.error("Sync apply: op %s (%s %s) failed: %s",
                         op.op_id, type(op).kind, op.uid, e)
    if wiki_touched:
        try:
            from tiro.wiki import reconcile_wiki_index
            reconcile_wiki_index(config)
        except Exception as e:
            logger.error("Sync apply: wiki index refresh failed: %s", e)
    return report


def _ordered(ops: list) -> list:
    """Deterministic apply order: rows first (referents before links),
    then files, meta, lines, links, then deletes, aliases last."""
    def rank(op) -> tuple:
        if isinstance(op, RowPut):
            k = 0 if op.table in ROW_TABLES else 4
        elif isinstance(op, FilePut):
            k = 1
        elif isinstance(op, Meta):
            k = 2
        elif isinstance(op, LinePut):
            k = 3
        elif isinstance(op, LineDel):
            k = 5
        elif isinstance(op, RowDel):
            k = 6 if op.table != "articles" else 7
        elif isinstance(op, FileDel):
            k = 6
        else:  # Alias
            k = 8
        return (k, op.hlc.to_str(), op.op_id)
    return sorted(ops, key=rank)


def _mass_delete_guard(config: TiroConfig, ops: list) -> str | None:
    """Spec §4: a pulled diff deleting > max(10, 20%) of local articles (or
    the annotations equivalent over highlights) halts the merge."""
    art_dels = sum(1 for o in ops
                   if isinstance(o, RowDel) and o.table == "articles")
    line_dels = sum(1 for o in ops if isinstance(o, LineDel))
    if not art_dels and not line_dels:
        return None
    conn = get_connection(config.db_path)
    try:
        n_art = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
        n_hl = conn.execute("SELECT COUNT(*) AS n FROM highlights").fetchone()["n"]
    finally:
        conn.close()
    if art_dels and art_dels > max(MASS_DELETE_FLOOR,
                                   math.ceil(MASS_DELETE_FRACTION * n_art)):
        return f"{art_dels} article deletions vs {n_art} local articles"
    if line_dels and line_dels > max(MASS_DELETE_FLOOR,
                                     math.ceil(MASS_DELETE_FRACTION * n_hl)):
        return f"{line_dels} highlight deletions vs {n_hl} local highlights"
    return None


# --- file ops -----------------------------------------------------------------


def _apply_file_put(config: TiroConfig, op: FilePut, report: ApplyReport,
                    clock: HLCClock) -> bool:
    """Returns True when a wiki file was touched (index refresh batching)."""
    if op.body is None:
        raise ValueError("unhydrated file_put (body missing)")
    path = _resolve_path(config, op.path_hint)
    kind = _entry_kind_for_file(op.path_hint, op.uid)
    is_article = kind == "article"

    conn = get_connection(config.db_path)
    try:
        shadow = _shadow_get(conn, kind, op.uid)
        if shadow and shadow["hlc"] and op.hlc.to_str() <= shadow["hlc"]:
            report.skipped_stale += 1
            report._count(op, "skipped_stale")
            return False

        current = None
        if path.exists():
            if is_article:
                current = frontmatter.load(str(path)).content  # BODY hash space
            else:
                current = path.read_text()
        # For articles op.object_hash is the FULL-file blob address (hash
        # spaces, module docstring): re-derive the BODY-space hash from the
        # hydrated body — this is the only hash ever compared to the current
        # body or stored into the shadow for an article.
        incoming_hash_space = (
            content_hash(frontmatter.loads(op.body).content)
            if is_article else op.object_hash
        )

        if is_article:
            row = conn.execute("SELECT * FROM articles WHERE uid = ?",
                               (op.uid,)).fetchone()
            if row is None and current is None:
                _materialize_article(config, conn, op, report)
                # Deliberate accepted noise: this shadow row carries only
                # path_hint in fields, so the next local diff before the
                # cycle-end save_shadow may re-emit Meta ops for non-default
                # meta fields (receivers skip them as stale — a bounded
                # one-cycle echo).
                shadow_upsert(conn, kind, op.uid, hash=incoming_hash_space,
                              fields={"path_hint": op.path_hint},
                              hlc=op.hlc.to_str())
                conn.commit()
                report.applied += 1
                return False

        cur_hash = content_hash(current) if current is not None else None
        if current is not None and cur_hash == incoming_hash_space:
            shadow_upsert(conn, kind, op.uid,
                          hash=cur_hash, fields={"path_hint": op.path_hint},
                          hlc=op.hlc.to_str())
            conn.commit()
            report.applied += 1
            report._count(op, "fast_forward_noop")
            return False

        concurrent = (current is not None and op.base_hash is not None
                      and op.base_hash != cur_hash) or (
                      current is not None and op.base_hash is None)
        if concurrent:
            local_unedited = shadow and shadow["hash"] == cur_hash
            local_hlc = (HLC.parse(shadow["hlc"]) if local_unedited and shadow["hlc"]
                         else clock.tick())
            if op.hlc < local_hlc:
                # LOCAL wins: remote body preserved as conflict file.
                loser_body = (frontmatter.loads(op.body).content
                              if is_article else op.body)
                dest = write_conflict_file(path.parent, path.stem, loser_body,
                                           device=op.device)
                report.conflicts += 1
                report._count(op, "conflict_local_won",
                              conflict_file=dest.name)
                # Shadow hlc does NOT advance past local (local will out-op).
                conn.commit()
                return False
            # REMOTE wins: local body preserved as conflict file.
            write_conflict_file(path.parent, path.stem, current, device="local")
            report.conflicts += 1
            report._count(op, "conflict_remote_won")

        _atomic_write(path, op.body)
        materialized = False
        if is_article:
            row = conn.execute("SELECT * FROM articles WHERE uid = ?",
                               (op.uid,)).fetchone()
            post = frontmatter.loads(op.body)
            if row is None:
                _materialize_article(config, conn, op, report)
                materialized = True
            else:
                refresh_article_from_file(config, conn, row, path, post.content,
                                          content_hash(post.content),
                                          meta=post.metadata or {})
        elif kind == "note":
            _upsert_note_row(conn, config, op)
        # Article shadow rows: BODY-space hash + path_hint only — see the
        # accepted one-cycle Meta-echo note on the materialize branch above.
        shadow_upsert(conn, kind, op.uid, hash=incoming_hash_space,
                      fields={"path_hint": op.path_hint}, hlc=op.hlc.to_str())
        conn.commit()
        report.applied += 1
        if not materialized:
            # _materialize_article already _count()ed the op ("materialized")
            # — one op, one by_kind increment.
            report._count(op, "applied")
        return kind in ("wiki", "pathfile") and op.path_hint.startswith("wiki/")
    finally:
        conn.close()


def _apply_file_del(config: TiroConfig, op: FileDel, report: ApplyReport) -> bool:
    path = _resolve_path(config, op.path_hint)
    kind = _entry_kind_for_file(op.path_hint, op.uid)
    if kind == "article":
        raise ValueError("article deletion must be row_del, never file_del")
    conn = get_connection(config.db_path)
    try:
        shadow = _shadow_get(conn, kind, op.uid)
        if shadow and shadow["hlc"] and op.hlc.to_str() <= shadow["hlc"]:
            report.skipped_stale += 1
            report._count(op, "skipped_stale")
            return False
        if not path.exists():
            shadow_tombstone(conn, kind, op.uid, hlc=op.hlc.to_str())
            conn.commit()
            report.tombstones += 1
            report._count(op, "already_gone")
            return False
        current = path.read_text()
        if op.base_hash is None or content_hash(current) != op.base_hash:
            # Edit-wins retention (plan decision #17). base_hash=None on an
            # EXISTING file is treated as concurrent-by-construction, same
            # posture as the put side — never blind-delete user text (our
            # own diff always sends base_hash=prev.hash, so None here means
            # a foreign/degraded op; retention bias wins).
            report.resurrected += 1
            report._count(op, "resurrected_edit_wins")
            return False
        path.unlink()
        if kind == "note":
            row = conn.execute("SELECT id FROM articles WHERE uid = ?",
                               (op.uid,)).fetchone()
            if row:
                conn.execute(
                    "DELETE FROM notes WHERE article_id = ? AND highlight_id IS NULL",
                    (row["id"],))
        shadow_tombstone(conn, kind, op.uid, hlc=op.hlc.to_str())
        conn.commit()
        report.applied += 1
        report.tombstones += 1
        report._count(op, "applied")
        # Mirror _apply_file_put's return: a pathfile under wiki/ (e.g. a
        # deleted wiki conflict file) refreshes the index too.
        return kind == "wiki" or (kind == "pathfile"
                                  and op.path_hint.startswith("wiki/"))
    finally:
        conn.close()


def _upsert_note_row(conn, config: TiroConfig, op: FilePut) -> None:
    row = conn.execute("SELECT id FROM articles WHERE uid = ?",
                       (op.uid,)).fetchone()
    if row is None:
        return  # article not here (yet); reconcile_annotations heals later
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing = conn.execute(
        "SELECT id FROM notes WHERE article_id = ? AND highlight_id IS NULL",
        (row["id"],)).fetchone()
    if existing:
        conn.execute("UPDATE notes SET body_markdown = ?, updated_at = ? "
                     "WHERE id = ?", (op.body, now, existing["id"]))
    else:
        conn.execute(
            "INSERT INTO notes (uid, article_id, highlight_id, body_markdown, "
            "created_at, updated_at) VALUES (?, ?, NULL, ?, ?, ?)",
            (new_ulid(), row["id"], op.body, now, now))


def _materialize_article(config: TiroConfig, conn, op: FilePut,
                         report: ApplyReport) -> None:
    """Create the article row for an unknown-uid file_put (plan decision #9).
    Frontmatter is the row source (the processor writes title/author/url/
    tags/summary/published); NO enrichment, NO LLM, NO stats. URL dedupe
    (decision #12) is wired in Task 7 — this function gains a dedupe
    pre-check there."""
    path = _resolve_path(config, op.path_hint)
    post = frontmatter.loads(op.body)
    body = post.content
    meta = post.metadata or {}
    _atomic_write(path, op.body)

    title = str(meta.get("title") or path.stem)
    url = str(meta.get("url") or "")
    source_id = _source_for(conn, meta)
    slug, n = path.stem, 2
    while conn.execute("SELECT 1 FROM articles WHERE slug = ?", (slug,)).fetchone():
        slug = f"{path.stem}-{n}"
        n += 1
    word_count = len(body.split())
    conn.execute(
        """INSERT INTO articles
           (uid, source_id, title, author, url, slug, markdown_path,
            summary, word_count, reading_time_min, published_at, ingested_at,
            ingestion_method, vector_status, body_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sync', 'pending', ?)""",
        (op.uid, source_id, title, meta.get("author"), url, slug, path.name,
         meta.get("summary"), word_count, max(1, math.ceil(word_count / 250)),
         str(meta["published"]) if meta.get("published") else None,
         datetime.now().isoformat(), content_hash(body)),
    )
    article_id = conn.execute("SELECT id FROM articles WHERE uid = ?",
                              (op.uid,)).fetchone()["id"]
    from tiro.sync.reconcile import _sync_tags_from_frontmatter
    if isinstance(meta.get("tags"), list):
        _sync_tags_from_frontmatter(conn, article_id,
                                    [str(t) for t in meta["tags"]])
    try:
        from tiro.authors import link_article_author
        link_article_author(conn, article_id, meta.get("author"))
    except Exception as e:
        logger.error("link_article_author failed for %s (non-fatal): %s",
                     op.uid, e)
    report._count(op, "materialized")


def _source_for(conn, meta: dict) -> int:
    """source_uid meta ops repoint later; at materialize time fall back to
    frontmatter source name / url domain (S1 _external_source_id pattern)."""
    from urllib.parse import urlparse
    url = str(meta.get("url") or "")
    domain = urlparse(url).netloc if url else ""
    if domain:
        from tiro.ingestion.processor import _get_or_create_source
        return _get_or_create_source(conn, domain)
    name = str(meta.get("source") or "Synced")
    row = conn.execute(
        "SELECT id FROM sources WHERE name = ? AND domain IS NULL "
        "AND email_sender IS NULL", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO sources (uid, name, source_type) VALUES (?, ?, 'web')",
        (new_ulid(), name))
    return cur.lastrowid


# --- JSONL per-uid merge (spec §4 row 4) ---------------------------------------


def _line_sort_key(line: dict) -> tuple:
    return (line.get("created_at") or "", line.get("uid") or "")


def _lww_pick(a: dict, b: dict) -> tuple[dict, dict]:
    """(winner, loser) — LWW on updated_at (missing loses), ties broken by
    canonical JSON of the whole line (arbitrary but symmetric)."""
    ka = (a.get("updated_at") or "", canonical_json(a))
    kb = (b.get("updated_at") or "", canonical_json(b))
    return (a, b) if ka >= kb else (b, a)


def _conflict_blockquote(text: str, when: str | None, label: str) -> str:
    day = (when or "")[:10] or "unknown-date"
    quoted = "\n".join("> " + ln for ln in text.splitlines() or [""])
    return f"> [conflict {day} {label}]\n{quoted}"


def merge_jsonl(lines_a: list[dict], lines_b: list[dict], *,
                label_a: str = "local", label_b: str = "remote") -> list[dict]:
    """FROZEN core signature. Per-uid set union; same-uid clash resolves
    LWW-whole-line on updated_at, and a losing note_markdown that differs is
    APPENDED to the winning note as a [conflict {date} {device}] blockquote —
    never silently dropped (spec §4). Pure, deterministic, commutative
    (labels swap with their sides)."""
    by_uid: dict[str, tuple[dict, str]] = {}
    for line, label in [(ln, label_a) for ln in lines_a] + \
                       [(ln, label_b) for ln in lines_b]:
        uid = line.get("uid")
        if not uid:
            continue
        if uid not in by_uid:
            by_uid[uid] = (dict(line), label)
            continue
        cur, cur_label = by_uid[uid]
        if canonical_json(cur) == canonical_json(line):
            continue  # identical twins — nothing to merge
        winner, loser = _lww_pick(cur, line)
        l_label = label if winner is cur else cur_label
        merged = dict(winner)
        w_note = winner.get("note_markdown")
        l_note = loser.get("note_markdown")
        quoted_l_note = ("\n".join("> " + ln for ln in l_note.splitlines() or [""])
                         if l_note else "")
        # Skip when the loser's note is already present RAW (substring
        # heuristic, plan decision #15 / D20) or already present as a
        # BLOCKQUOTE — without the second check a multi-line loser note
        # re-presented on a later merge (third device, re-delivered file)
        # appends the same conflict block again (S2.5 review F2; single-line
        # notes were only coincidentally protected by their "> " prefix).
        if (l_note and l_note != w_note and l_note not in (w_note or "")
                and quoted_l_note not in (w_note or "")):
            block = _conflict_blockquote(l_note, loser.get("updated_at"), l_label)
            merged["note_markdown"] = (w_note + "\n\n" + block) if w_note else block
        by_uid[uid] = (merged, cur_label if winner is cur else label)
    return sorted((line for line, _label in by_uid.values()), key=_line_sort_key)


# --- line ops -------------------------------------------------------------------


def _highlight_row_from_line(conn, article_id: int, line: dict) -> None:
    """Upsert the derived highlights row (+ anchored note row) from a
    sidecar line — the index half of the sidecar-first write."""
    existing = conn.execute("SELECT id FROM highlights WHERE uid = ?",
                            (line["uid"],)).fetchone()
    params = (
        article_id, line.get("quote"), line.get("prefix"), line.get("suffix"),
        line.get("position_start"), line.get("position_end"),
        line.get("content_hash"), line.get("color") or "yellow",
        line.get("created_at"), line.get("updated_at"),
    )
    if existing:
        conn.execute(
            "UPDATE highlights SET article_id = ?, quote_text = ?, "
            "prefix_context = ?, suffix_context = ?, text_position_start = ?, "
            "text_position_end = ?, content_hash = ?, color = ?, "
            "created_at = ?, updated_at = ? WHERE uid = ?",
            (*params, line["uid"]))
        hl_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO highlights (uid, article_id, quote_text, "
            "prefix_context, suffix_context, text_position_start, "
            "text_position_end, content_hash, color, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (line["uid"], *params))
        hl_id = cur.lastrowid
    note = line.get("note_markdown")
    nrow = conn.execute("SELECT id FROM notes WHERE highlight_id = ?",
                        (hl_id,)).fetchone()
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if note and note.strip():
        if nrow:
            conn.execute("UPDATE notes SET body_markdown = ?, updated_at = ? "
                         "WHERE id = ?", (note, now, nrow["id"]))
        else:
            conn.execute(
                "INSERT INTO notes (uid, article_id, highlight_id, "
                "body_markdown, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (new_ulid(), article_id, hl_id, note, now, now))
    elif nrow:
        conn.execute("DELETE FROM notes WHERE id = ?", (nrow["id"],))


def _apply_line_put(config: TiroConfig, op: LinePut, report: ApplyReport) -> None:
    from tiro.annotations import read_annotations, sidecar_stem, write_annotations

    # Validate BEFORE any file write (S2.5 review F1): an invalid line —
    # uid mismatch or missing/empty quote (mirroring _parse_jsonl_lines'
    # requirements and the NOT NULL highlights.quote_text column) — must
    # error WITHOUT mutating the sidecar. Writing it first would destroy
    # the existing good line for that uid in the whole-file rewrite, leave
    # a poison line the next read counts as malformed (the sidecar then
    # gets marked unreadable, permanently degrading that stem's sync), and
    # cascade into reconcile_annotations deleting the user's rows.
    line = op.line
    if (not isinstance(line, dict) or line.get("uid") != op.uid
            or not isinstance(line.get("quote"), str) or not line["quote"]):
        report.errors += 1
        report._count(op, "errors", error="invalid line payload (uid/quote)")
        return

    conn = get_connection(config.db_path)
    try:
        shadow = _shadow_get(conn, "highlight", op.uid)
        if shadow and shadow["hlc"] and op.hlc.to_str() <= shadow["hlc"]:
            report.skipped_stale += 1
            report._count(op, "skipped_stale")
            return
        arow = conn.execute(
            "SELECT id, uid, markdown_path FROM articles WHERE uid = ?",
            (op.article_uid,)).fetchone()
        if arow is None:
            report.deferred += 1
            report._count(op, "deferred_unknown_article")
            return
        stem = sidecar_stem(arow)
        # FILE FIRST (sidecar-first invariant): merge the incoming line into
        # the sidecar via the same per-uid rules a two-file merge uses.
        local_lines = read_annotations(config, stem)
        merged = merge_jsonl(local_lines, [op.line],
                             label_a="local", label_b=op.device)
        write_annotations(config, stem, merged)
        # ROW SECOND — from the RE-READ line, not the in-memory merge result:
        # write_annotations projects onto _FIELD_ORDER (unknown wire keys
        # dropped, missing keys -> None), so hashing/storing the in-memory
        # dict could diverge from what build_manifest's _add_highlights will
        # compute from disk next cycle (phantom LinePut echo). The shadow row
        # must byte-match the manifest entry: same hash space
        # (content_hash(canonical_json(disk line))), same fields shape
        # (article_uid/line/path_hint — path_hint keeps the unreadable-
        # protection guards in diff/save_shadow structurally sound).
        merged_line = next(ln for ln in read_annotations(config, stem)
                           if ln["uid"] == op.uid)
        _highlight_row_from_line(conn, arow["id"], merged_line)
        shadow_upsert(conn, "highlight", op.uid,
                      hash=content_hash(canonical_json(merged_line)),
                      fields={"article_uid": merged_line.get("article_uid"),
                              "line": merged_line,
                              "path_hint": f"annotations/{stem}.jsonl"},
                      hlc=op.hlc.to_str())
        conn.commit()
        report.applied += 1
        report._count(op, "applied")
    finally:
        conn.close()


def _apply_line_del(config: TiroConfig, op: LineDel, report: ApplyReport) -> None:
    from tiro.annotations import (
        notes_dir,
        read_annotations,
        sidecar_stem,
        write_annotations,
    )

    conn = get_connection(config.db_path)
    try:
        shadow = _shadow_get(conn, "highlight", op.uid)
        if shadow and shadow["hlc"] and op.hlc.to_str() <= shadow["hlc"]:
            report.skipped_stale += 1
            report._count(op, "skipped_stale")
            return
        arow = conn.execute(
            "SELECT id, uid, markdown_path FROM articles WHERE uid = ?",
            (op.article_uid,)).fetchone()
        if arow is None:
            # Nothing local to delete — tombstone so a late line_put stays dead.
            shadow_tombstone(conn, "highlight", op.uid, hlc=op.hlc.to_str())
            conn.commit()
            report.tombstones += 1
            report._count(op, "tombstone_no_local")
            return
        stem = sidecar_stem(arow)
        lines = read_annotations(config, stem)
        target = next((ln for ln in lines if ln.get("uid") == op.uid), None)
        if target is not None:
            # Spec §4: delete wins over concurrent edit EXCEPT note_markdown —
            # a note edited after the remover's observation is preserved as an
            # article-level conflict note (never destroyed by a race).
            note = target.get("note_markdown")
            edited_after = (
                note and note.strip()
                and (op.observed_updated_at is None
                     or (target.get("updated_at") or "") > op.observed_updated_at)
            )
            if edited_after:
                dest = write_conflict_file(notes_dir(config), stem, note,
                                           device=op.device)
                report.resurrected += 1
                report._count(op, "note_resurrected", conflict_file=dest.name)
            # FILE FIRST: drop the line. An emptied sidecar stays as an EMPTY
            # file (write_annotations never unlinks; reconcile parses it as
            # zero lines and the mass-delete guard counts its stem present).
            write_annotations(config, stem,
                              [ln for ln in lines if ln.get("uid") != op.uid])
        hrow = conn.execute("SELECT id FROM highlights WHERE uid = ?",
                            (op.uid,)).fetchone()
        if hrow:
            conn.execute("DELETE FROM notes WHERE highlight_id = ?", (hrow["id"],))
            conn.execute("DELETE FROM highlights WHERE id = ?", (hrow["id"],))
        shadow_tombstone(conn, "highlight", op.uid, hlc=op.hlc.to_str())
        conn.commit()
        report.applied += 1
        report.tombstones += 1
        report._count(op, "applied")
    finally:
        conn.close()


# --- meta / row / link / article-tombstone ops ----------------------------------


def _apply_meta(config: TiroConfig, op: Meta, report: ApplyReport) -> None:
    # META_FIELDS is the single injection barrier: op.field is interpolated
    # into SQL below ONLY after this allowlist check. Raising (not silently
    # counting) is deliberate — apply_ops' per-op try/except converts it to
    # report.errors, keeping the disallowed-field surface visible.
    if op.field not in META_FIELDS:
        raise ValueError(f"meta field not allowed: {op.field!r}")
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT id, meta_updated_at, opened_count FROM articles WHERE uid = ?",
            (op.uid,)).fetchone()
        if row is None:
            report.deferred += 1
            report._count(op, "deferred_unknown_article")
            return
        if op.field == "opened_count":
            # max()-merge (spec §4): ts irrelevant, monotone counter.
            conn.execute(
                "UPDATE articles SET opened_count = MAX(opened_count, ?) "
                "WHERE id = ?", (int(op.value or 0), row["id"]))
            conn.commit()
            report.applied += 1
            report._count(op, "applied")
            return
        local_ts = row["meta_updated_at"]
        if local_ts and not op.ts:
            # S2.6 review F7: a ts-less (None/"") meta op must never
            # overwrite a stamped field — it would bypass both LWW guards
            # AND null meta_updated_at, regressing the clock. Mirror of the
            # NULL-local-loses rule.
            report.skipped_stale += 1
            report._count(op, "skipped_stale")
            return
        if local_ts and op.ts and op.ts < local_ts:
            report.skipped_stale += 1
            report._count(op, "skipped_stale")
            return
        if op.ts == local_ts and op.ts is not None:
            # Deterministic symmetric tiebreak (plan decision #7):
            # keep whichever value serializes larger; equal values are a
            # no-op. source_uid must compare uid-vs-uid (S2.6 review F1):
            # comparing op.value (a uid STRING) against the local INTEGER
            # source_id is asymmetric — in canonical JSON a quoted string
            # always sorts below any integer, so every equal-ts repoint
            # (the NORMAL post-sync state, since apply stamps
            # meta_updated_at = op.ts) would be skipped forever.
            if op.field == "source_uid":
                cur = conn.execute(
                    "SELECT s.uid AS v FROM articles a "
                    "LEFT JOIN sources s ON s.id = a.source_id "
                    "WHERE a.id = ?", (row["id"],)).fetchone()
            else:
                cur = conn.execute(
                    f"SELECT {op.field} AS v FROM articles WHERE id = ?",
                    (row["id"],)).fetchone()
            if canonical_json(op.value) <= canonical_json(cur["v"]):
                report.skipped_stale += 1
                report._count(op, "skipped_stale_tie")
                return
        if op.field == "source_uid":
            src = conn.execute("SELECT id FROM sources WHERE uid = ?",
                               (op.value,)).fetchone()
            if src is None:
                report.deferred += 1
                report._count(op, "deferred_unknown_source")
                return
            conn.execute(
                "UPDATE articles SET source_id = ?, meta_updated_at = ? "
                "WHERE id = ?", (src["id"], op.ts, row["id"]))
        else:  # rating / is_read / snoozed_until — allowlisted identifiers only
            conn.execute(
                f"UPDATE articles SET {op.field} = ?, meta_updated_at = ? "
                "WHERE id = ?", (op.value, op.ts, row["id"]))
        conn.commit()
        report.applied += 1
        report._count(op, "applied")
    finally:
        conn.close()


def _apply_row_put(config: TiroConfig, op: RowPut, report: ApplyReport) -> None:
    if op.table in LINK_TABLES:
        _apply_link_put(config, op, report)
        return
    if op.table not in ROW_TABLES:
        raise ValueError(f"table not in the sync set: {op.table!r}")
    conn = get_connection(config.db_path)
    try:
        kind = f"row:{op.table}"
        shadow = _shadow_get(conn, kind, op.uid)
        # Digests SKIP the shadow-hlc stale gate (S2.6 review F6): prefer-
        # newer created_at alone decides, so the outcome is deterministic
        # regardless of arrival order — with the gate, a hlc-newer/created-
        # older op arriving first would shadow-block the created-newer one.
        if (op.table != "digests" and shadow and shadow["hlc"]
                and op.hlc.to_str() <= shadow["hlc"]):
            report.skipped_stale += 1
            report._count(op, "skipped_stale")
            return
        cols = _ROW_COLUMNS[op.table]
        # COLUMN PROJECTION, not the raw wire row: the shadow's hash AND
        # fields must byte-match what build_manifest's _add_rows computes
        # from SQLite next cycle — a wire row with extra keys stored raw
        # would diverge and echo a phantom RowPut.
        fields = {c: op.row.get(c) for c in cols}
        values = [fields[c] for c in cols]
        if op.table == "digests":
            # Identity guard (S2.6 review F8): the table write keys off the
            # PAYLOAD's date/digest_type while the shadow keys off op.uid —
            # a mismatched op would desync them. Per-op error via apply_ops.
            expected = f"{op.row.get('date')}:{op.row.get('digest_type')}"
            if op.uid != expected:
                raise ValueError(
                    f"digest uid/payload mismatch: {op.uid!r} != {expected!r}")
            # prefer-newer created_at (spec §4 digests rule), not plain LWW.
            cur = conn.execute(
                "SELECT created_at FROM digests WHERE date = ? AND digest_type = ?",
                (fields["date"], fields["digest_type"])).fetchone()
            if cur and (cur["created_at"] or "") >= (fields["created_at"] or ""):
                report.skipped_stale += 1
                report._count(op, "skipped_older_digest")
                return
            conn.execute(
                "INSERT INTO digests (date, digest_type, content, article_ids, "
                "created_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(date, digest_type) DO UPDATE SET "
                "content = excluded.content, article_ids = excluded.article_ids, "
                "created_at = excluded.created_at", values)
        else:
            placeholders = ", ".join("?" for _ in cols)
            updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "uid")
            conn.execute(
                f"INSERT INTO {op.table} ({', '.join(cols)}) "
                f"VALUES ({placeholders}) "
                f"ON CONFLICT(uid) DO UPDATE SET {updates}", values)
        shadow_upsert(conn, kind, op.uid, hash=_fields_hash(fields),
                      fields=fields, hlc=op.hlc.to_str())
        conn.commit()
        report.applied += 1
        report._count(op, "applied")
    finally:
        conn.close()


def _link_local_ids(conn, table: str, a_uid: str, b_uid: str):
    a_tab, _a_col, b_tab, _b_col, _extras = _LINK_SIDES[table]
    a = conn.execute(f"SELECT id FROM {a_tab} WHERE uid = ?", (a_uid,)).fetchone()
    b = conn.execute(f"SELECT id FROM {b_tab} WHERE uid = ?", (b_uid,)).fetchone()
    return (a["id"] if a else None), (b["id"] if b else None)


def _apply_link_put(config: TiroConfig, op: RowPut, report: ApplyReport) -> None:
    conn = get_connection(config.db_path)
    try:
        kind = f"link:{op.table}"
        shadow = _shadow_get(conn, kind, op.uid)
        if shadow and shadow["hlc"] and op.hlc.to_str() <= shadow["hlc"]:
            report.skipped_stale += 1
            report._count(op, "skipped_stale")
            return
        a_uid, b_uid = op.row.get("a_uid"), op.row.get("b_uid")
        a_id, b_id = _link_local_ids(conn, op.table, a_uid, b_uid)
        if a_id is None or b_id is None:
            report.deferred += 1
            report._count(op, "deferred_unresolved_link")
            return
        a_tab, a_col, b_tab, b_col, extras = _LINK_SIDES[op.table]
        extra_cols = "".join(f", {c}" for c in extras)
        extra_ph = "".join(", ?" for _ in extras)
        conn.execute(
            f"INSERT OR REPLACE INTO {op.table} ({a_col}, {b_col}{extra_cols}) "
            f"VALUES (?, ?{extra_ph})",
            (a_id, b_id, *[op.row.get(c) for c in extras]))
        # Same projection rule as rows: shadow fields/hash must match
        # _add_links' shape ({a_uid, b_uid} + extras), never the raw wire row.
        fields = {"a_uid": a_uid, "b_uid": b_uid}
        for c in extras:
            fields[c] = op.row.get(c)
        shadow_upsert(conn, kind, op.uid, hash=_fields_hash(fields),
                      fields=fields, hlc=op.hlc.to_str())
        conn.commit()
        report.applied += 1
        report._count(op, "applied")
    finally:
        conn.close()


def _apply_row_del(config: TiroConfig, op: RowDel, report: ApplyReport) -> None:
    if op.table == "articles":
        _apply_article_tombstone(config, op, report)
        return
    if op.table in LINK_TABLES:
        _apply_link_del(config, op, report)
        return
    if op.table not in ROW_TABLES:
        raise ValueError(f"table not in the sync set: {op.table!r}")
    conn = get_connection(config.db_path)
    try:
        kind = f"row:{op.table}"
        shadow = _shadow_get(conn, kind, op.uid)
        if shadow and shadow["hlc"] and op.hlc.to_str() <= shadow["hlc"]:
            report.skipped_stale += 1
            report._count(op, "skipped_stale")
            return
        if op.table == "digests":
            date, _sep, digest_type = op.uid.partition(":")
            conn.execute("DELETE FROM digests WHERE date = ? AND digest_type = ?",
                         (date, digest_type))
        else:
            # Referential safety (S2.6 review F5): FKs are ON, so deleting a
            # row still referenced locally raises IntegrityError — catch it
            # and DEFER (row kept, NO shadow tombstone) instead of erroring;
            # the local link re-ops the row on the next diff (retention bias).
            try:
                conn.execute(f"DELETE FROM {op.table} WHERE uid = ?", (op.uid,))
            except sqlite3.IntegrityError:
                conn.rollback()
                report.deferred += 1
                report._count(op, "deferred_still_referenced")
                return
        shadow_tombstone(conn, kind, op.uid, hlc=op.hlc.to_str())
        conn.commit()
        report.applied += 1
        report.tombstones += 1
        report._count(op, "applied")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _apply_link_del(config: TiroConfig, op: RowDel, report: ApplyReport) -> None:
    conn = get_connection(config.db_path)
    try:
        kind = f"link:{op.table}"
        shadow = _shadow_get(conn, kind, op.uid)
        # Add-wins (spec §4, plan decision #10): the remove only applies if
        # the local link's add-hlc is <= what the remover observed. A newer
        # local (re-)add survives; a never-synced local link survives too.
        if shadow and shadow["hlc"]:
            if op.observed is None or shadow["hlc"] > op.observed:
                report.skipped_stale += 1
                report._count(op, "add_wins_over_remove")
                return
        elif shadow is None or shadow["deleted_at"] is None:
            # A NEVER-SYNCED local link has no shadow hlc the remover could
            # possibly have observed — whatever op.observed cites is some
            # OTHER device's add, never this one, so the local add survives
            # regardless of observed (S2.6 review F3; plan decision #10's
            # retention clause).
            if _link_exists(conn, op):
                report.skipped_stale += 1
                report._count(op, "add_wins_over_remove")
                return
        a_uid, _sep, b_uid = op.uid.partition(":")
        a_id, b_id = _link_local_ids(conn, op.table, a_uid, b_uid)
        if a_id is not None and b_id is not None:
            _a_tab, a_col, _b_tab, b_col, _extras = _LINK_SIDES[op.table]
            conn.execute(f"DELETE FROM {op.table} WHERE {a_col} = ? AND {b_col} = ?",
                         (a_id, b_id))
        shadow_tombstone(conn, kind, op.uid, hlc=op.hlc.to_str())
        conn.commit()
        report.applied += 1
        report.tombstones += 1
        report._count(op, "applied")
    finally:
        conn.close()


def _link_exists(conn, op: RowDel) -> bool:
    a_uid, _sep, b_uid = op.uid.partition(":")
    a_id, b_id = _link_local_ids(conn, op.table, a_uid, b_uid)
    if a_id is None or b_id is None:
        return False
    _a_tab, a_col, _b_tab, b_col, _extras = _LINK_SIDES[op.table]
    return conn.execute(
        f"SELECT 1 FROM {op.table} WHERE {a_col} = ? AND {b_col} = ?",
        (a_id, b_id)).fetchone() is not None


def _apply_article_tombstone(config: TiroConfig, op: RowDel,
                             report: ApplyReport) -> None:
    conn = get_connection(config.db_path)
    try:
        shadow = _shadow_get(conn, "article", op.uid)
        if shadow and shadow["hlc"] and op.hlc.to_str() <= shadow["hlc"]:
            report.skipped_stale += 1
            report._count(op, "skipped_stale")
            return
        row = conn.execute(
            "SELECT id, body_hash FROM articles WHERE uid = ?",
            (op.uid,)).fetchone()
        if row is None:
            shadow_tombstone(conn, "article", op.uid, hlc=op.hlc.to_str())
            conn.commit()
            report.tombstones += 1
            report._count(op, "already_gone")
            return
        # Edit-wins comparison is BODY-space vs BODY-space (hash spaces,
        # module docstring): diff emits observed=prev.hash and article
        # shadow/manifest hashes are body_hash — never op.object_hash.
        # observed=None on an EXISTING article mirrors _apply_file_del's
        # posture (S2.6 review F4): our own diff always stamps
        # observed=prev.hash, so None here means a foreign/degraded op —
        # retention bias resurrects, never blind-deletes.
        if op.observed is None or row["body_hash"] != op.observed:
            # Spec §4: delete vs concurrent body edit -> edit wins, article
            # resurrects (retention bias). The local edit out-ops the delete
            # on the next diff.
            report.resurrected += 1
            report._count(op, "resurrected_edit_wins")
            return
        article_id = row["id"]
    finally:
        conn.close()
    from tiro.lifecycle import delete_article
    delete_article(config, article_id)  # seven-store coordinator, own conn
    conn = get_connection(config.db_path)
    try:
        shadow_tombstone(conn, "article", op.uid, hlc=op.hlc.to_str())
        conn.commit()
    finally:
        conn.close()
    report.applied += 1
    report.tombstones += 1
    report._count(op, "applied")


# Task 7 handler lands next; the stub keeps _ordered dispatch importable.
def _apply_alias(config, op, report):  # pragma: no cover - Task 7
    raise NotImplementedError
