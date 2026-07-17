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

import json
import logging
import math
import re
import sqlite3
from dataclasses import dataclass, field, replace
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
        "SELECT hash, hlc, fields_json, deleted_at FROM sync_shadow "
        "WHERE kind = ? AND uid = ?",
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
                if not _materialize_article(config, conn, op, report, clock):
                    # URL-deduped into an existing article: the recursive
                    # _apply_file_put under the surviving uid owned all
                    # shadow/report bookkeeping for this op.
                    return False
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
                if not _materialize_article(config, conn, op, report, clock):
                    # URL-deduped (see above). The body written to
                    # op.path_hint just above stays behind as a rowless
                    # orphan file — reconcile's external ingest URL-dedupes
                    # (skips it) and doctor censuses it; never resurrections.
                    # KNOWN ACCOUNTING NOISE (S2.7 review Minor 2): if this
                    # fallthrough already counted conflict_remote_won above,
                    # the recursive re-routed apply counts the same wire op
                    # again — a narrow rowless-orphan-at-exact-hint corner;
                    # counters are report-only, correctness unaffected.
                    return False
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
                         report: ApplyReport, clock: HLCClock) -> bool:
    """Create the article row for an unknown-uid file_put (plan decision #9).
    Frontmatter is the row source (the processor writes title/author/url/
    tags/summary/published); NO enrichment, NO LLM, NO stats.

    Returns True when a new row was materialized. Returns False when the op
    was URL-DEDUPED into an existing article (decision #12) and re-routed
    through _apply_file_put under the surviving uid — the caller must then
    skip its own shadow/applied bookkeeping (the recursive apply did it)."""
    path = _resolve_path(config, op.path_hint)
    post = frontmatter.loads(op.body)  # ONE parse: dedupe + row source share it
    body = post.content
    meta = post.metadata or {}

    # URL dedupe (spec §4, plan decision #12 FROZEN): the same canonical URL
    # arriving under a DIFFERENT uid keeps the ULID-OLDER (lexicographically
    # smaller) uid and emits an Alias op for the journal. The incoming body
    # is re-routed through the normal file-merge path against the survivor,
    # so a concurrent local edit still resolves via decision #8's rules.
    url = str(meta.get("url") or "")
    if url:
        from tiro.ingestion.rss import _find_existing_article_by_url
        existing_id = _find_existing_article_by_url(conn, url)
        if existing_id is not None:
            existing = conn.execute(
                "SELECT uid, markdown_path FROM articles WHERE id = ?",
                (existing_id,)).fetchone()
            if existing and existing["uid"] and existing["uid"] != op.uid:
                survivor = min(existing["uid"], op.uid)
                loser = max(existing["uid"], op.uid)
                if survivor == op.uid:
                    # Incoming uid is OLDER: it survives — the LOCAL article
                    # adopts it before the re-routed put targets it.
                    _rewrite_local_uid(config, conn, existing["uid"], op.uid)
                # Record the alias exactly ONCE, always old_uid -> surviving
                # uid; the sync_shadow 'alias' row is TTL-exempt (decision
                # #18) so late ops for the dead uid can re-target forever.
                _record_alias(conn, loser, survivor)
                # Emitted aliases are DEDUPLICATED (decision #11): a second
                # file_put for the same duplicate in one batch must not
                # journal the mapping twice. The op is LOCALLY generated by
                # the merge itself, so it is stamped from the local clock —
                # never with the triggering remote op's hlc/device (a journal
                # line claiming another device authored it, at a wall-clock
                # predating its creation, would confuse S3/S5 per-device
                # journal invariants). S5 may re-stamp the device label with
                # the real device id at push time (S2.7 review Minor 1).
                if not any(isinstance(o, Alias) and o.uid == loser
                           and o.new_uid == survivor
                           for o in report.emitted_ops):
                    report.emitted_ops.append(Alias(
                        op_id=new_ulid(), hlc=clock.tick(),
                        device=clock.device, uid=loser, new_uid=survivor))
                # details-only, deliberately NOT report._count: one by_kind
                # increment per wire op, and the recursive apply below owns
                # this op's single count (applied / conflict / noop).
                report.details.setdefault("deduped_into", []).append(
                    {"kind": type(op).kind, "uid": op.uid,
                     "survivor": survivor})
                # Commit before recursing — _apply_file_put opens its own
                # connection and must not contend with this write txn.
                conn.commit()
                merged_op = replace(
                    op, uid=survivor,
                    path_hint=f"articles/{existing['markdown_path']}")
                _apply_file_put(config, merged_op, report, clock)
                return False

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
    return True


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
#
# The note algebra below was rebuilt by the Task-8 property suite (THE 1.0
# hard gate): the original pairwise blockquote-append was order-DEPENDENT —
# folding three notes as F(F(a,b),c) vs F(F(b,c),a) nested the conflict
# blockquotes differently, and the positional local/remote device label made
# even the two-note case byte-diverge across arrival orders. Merged notes are
# now a CANONICAL form — winner head + a sorted SET of conflict blocks, each
# quoting a loser's head VERBATIM under a content-derived "[conflict {day}]"
# header — so any fold order over the same set of lines produces identical
# bytes (test_apply_order_independent_across_devices), re-delivery never
# grows the note (idempotence), and every non-empty note appears verbatim as
# a substring of the merged note (test_merge_jsonl_never_loses_a_note — the
# old "> "-per-line quoting broke verbatim substring for multi-line notes).


def _line_sort_key(line: dict) -> tuple:
    return (line.get("created_at") or "", line.get("uid") or "")


def _line_key(line: dict) -> tuple:
    """Total order for LWW: updated_at (missing loses), then canonical JSON
    of the line WITHOUT note_markdown, then the note itself. The core must
    outrank the note: notes mutate during folds (conflict blocks accrue), so
    a note-inclusive tiebreak would let the fold GROUPING flip which core
    wins — breaking associativity-on-line-sets. The note participates only
    as the final key, where either pick yields the same core."""
    core = {k: v for k, v in line.items() if k != "note_markdown"}
    return (line.get("updated_at") or "", canonical_json(core),
            canonical_json(line.get("note_markdown")))


def _lww_pick(a: dict, b: dict) -> tuple[dict, dict]:
    """(winner, loser) — see _line_key."""
    return (a, b) if _line_key(a) >= _line_key(b) else (b, a)


# A conflict block header is a FULL line: "> [conflict 2026-07-10]" (loser's
# updated_at day) or "> [conflict unknown-date]". The day is CONTENT-derived
# (never a positional local/remote device label) so the same set of merged
# lines produces byte-identical notes on every device regardless of which
# side each line arrived from. Markdown lazy continuation renders the raw
# note lines that follow as part of the same blockquote.
_CONFLICT_HEADER_RE = re.compile(
    r"(?m)^> \[conflict (?:\d{4}-\d{2}-\d{2}|unknown-date)\]$")


def _conflict_block(text: str, when: str | None) -> str:
    day = (when or "")[:10] or "unknown-date"
    return f"> [conflict {day}]\n{text}"


def _decompose_note(note: str | None) -> tuple[str, list[str]]:
    """Split a merged note into (head, conflict blocks). Inverse of the
    assembly in _merge_notes: blocks start at header lines and non-final
    segments carry exactly one trailing "\\n\\n" separator (stripped here, so
    block bodies with their own trailing newlines round-trip byte-exactly).
    A note with no header lines is all head. A RAW user note containing a
    literal header line decomposes deterministically (same bytes, same split
    on every device) — convergence holds; only its visual grouping shifts."""
    if not note:
        return "", []
    starts = [m.start() for m in _CONFLICT_HEADER_RE.finditer(note)]
    if not starts:
        return note, []

    def _strip_sep(segment: str) -> str:
        return segment[:-2] if segment.endswith("\n\n") else segment

    head = _strip_sep(note[:starts[0]])
    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(note)
        seg = note[start:end]
        if i + 1 < len(starts):
            seg = _strip_sep(seg)
        blocks.append(seg)
    return head, blocks


def _merge_notes(winner: dict, loser: dict) -> str | None:
    """Canonical note merge: winner's head stays the head; the loser's head
    (when non-blank and different) joins the conflict-block SET, and the
    union is reassembled sorted. Set semantics make the result independent
    of fold order and stable under re-delivery (a block already present is
    never appended twice)."""
    w_note = winner.get("note_markdown")
    wh, wb = _decompose_note(w_note)
    lh, lb = _decompose_note(loser.get("note_markdown"))
    blocks = set(wb) | set(lb)
    if lh.strip() and lh != wh:
        blocks.add(_conflict_block(lh, loser.get("updated_at")))
    if blocks == set(wb):
        return w_note  # nothing new — keep the winner's bytes untouched
    parts = ([wh] if wh else []) + sorted(blocks)
    return "\n\n".join(parts) if parts else w_note


def merge_jsonl(lines_a: list[dict], lines_b: list[dict], *,
                label_a: str = "local", label_b: str = "remote") -> list[dict]:
    """FROZEN core signature. Per-uid set union; same-uid clash resolves
    LWW-whole-line on updated_at (_line_key total order), and a losing
    note_markdown that differs is preserved in the winning note as a
    "[conflict {date}]" block — never silently dropped (spec §4). Pure,
    deterministic, commutative AND order-independent across arbitrary fold
    groupings (canonical head+sorted-block-set note form; see the section
    comment above). label_a/label_b are retained for signature stability but
    are no longer embedded in conflict blocks — a positional label ("local"
    vs a device id for the SAME line, depending on arrival order) is exactly
    what byte-level convergence cannot contain."""
    del label_a, label_b  # positional labels cannot appear in convergent output
    by_uid: dict[str, dict] = {}
    for line in list(lines_a) + list(lines_b):
        uid = line.get("uid")
        if not uid:
            continue
        if uid not in by_uid:
            by_uid[uid] = dict(line)
            continue
        cur = by_uid[uid]
        if canonical_json(cur) == canonical_json(line):
            continue  # identical twins — nothing to merge
        winner, loser = _lww_pick(cur, line)
        merged = dict(winner)
        merged["note_markdown"] = _merge_notes(winner, loser)
        by_uid[uid] = merged
    return sorted(by_uid.values(), key=_line_sort_key)


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


# Highlight convergence model (rebuilt by the Task-8 property suite): the
# per-uid state every device must agree on is a pure function of the op SET —
#   watermark  = max HLC over all line ops seen for the uid
#   liveness   = whether the watermark op is a put (dels kill, later puts
#                resurrect; equal-HLC ties are impossible — HLCs are unique
#                per (wall, counter, device))
#   content    = merge_jsonl-fold over ALL put lines ever seen (the canonical
#                note algebra makes the fold order-independent)
# The old code skipped any op with hlc <= watermark, which made content
# depend on arrival order (an updated_at-newer line arriving hlc-late was
# dropped on one device and folded on another). Now a stale put still FOLDS
# (content), it just never advances the watermark (liveness). While dead,
# the fold is carried in the tombstone row's fields ("line"), so a
# resurrecting put re-folds the full content instead of starting from
# scratch. Sidecar FILE existence is also convergence-relevant (a kill
# leaves an empty file behind — Mandate C — which other arrival orders would
# otherwise never create), so every processed line op ensures the sidecar
# file of every article it cites exists (_ensure_sidecar).


def _fold_line(local: dict | None, incoming: dict) -> dict:
    return dict(incoming) if local is None else merge_jsonl([local], [incoming])[0]


def _stem_for_article(conn, article_uid: str | None) -> str | None:
    from tiro.annotations import sidecar_stem

    if not article_uid:
        return None
    row = conn.execute("SELECT markdown_path FROM articles WHERE uid = ?",
                       (article_uid,)).fetchone()
    return sidecar_stem(row["markdown_path"]) if row else None


def _ensure_sidecar(config: TiroConfig, conn, article_uid: str | None) -> None:
    """Create an EMPTY annotations sidecar for `article_uid` if none exists.
    File existence is part of the convergent state (see the model comment
    above): which sidecar files exist must be a function of the set of ops
    processed, never of their arrival order. Empty sidecars are inert by
    Mandate C (reconcile parses zero lines; the mass-delete guard counts the
    stem present)."""
    from tiro.annotations import annotations_dir, write_annotations

    stem = _stem_for_article(conn, article_uid)
    if stem is None:
        return
    path = annotations_dir(config) / f"{stem}.jsonl"
    if not path.exists():
        write_annotations(config, stem, [])


def _apply_line_put(config: TiroConfig, op: LinePut, report: ApplyReport) -> None:
    from tiro.annotations import (
        _ordered_line,
        notes_dir,
        read_annotations,
        sidecar_stem,
        write_annotations,
    )

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
    # Project onto the on-disk field order BEFORE folding/comparing: the
    # merge algebra must run in ONE representation space — a raw wire dict
    # (unknown keys, missing keys) canonicalizes differently from its own
    # parsed disk form, which would make fold results arrival-order-shaped.
    incoming = _ordered_line(line)

    conn = get_connection(config.db_path)
    try:
        _ensure_sidecar(config, conn, op.article_uid)
        _ensure_sidecar(config, conn, incoming.get("article_uid"))

        shadow = _shadow_get(conn, "highlight", op.uid)
        wm = shadow["hlc"] if shadow and shadow["hlc"] else None
        stale = wm is not None and op.hlc.to_str() <= wm
        dead = bool(shadow and shadow["deleted_at"])
        new_wm = max(op.hlc.to_str(), wm or "")

        if dead and stale:
            # DEAD-FOLD: the uid is tombstoned by a higher-hlc delete, so
            # this put stays dead — but its content must still join the fold
            # (a later resurrecting put re-folds it) and its note must land
            # somewhere durable (apply-level no-note-loss).
            stored = json.loads(shadow["fields_json"] or "{}").get("line")
            folded = _fold_line(stored, incoming)
            if stored is not None and folded == stored:
                report.skipped_stale += 1
                report._count(op, "skipped_stale")
                return
            shadow_tombstone(conn, "highlight", op.uid, hlc=wm,
                             fields={"article_uid": folded.get("article_uid"),
                                     "line": folded})
            conn.commit()
            note = incoming.get("note_markdown")
            if note and note.strip():
                stem = _stem_for_article(conn, incoming.get("article_uid")) or op.uid
                write_conflict_file(notes_dir(config), stem, note,
                                    device=op.device)
            report.applied += 1
            report._count(op, "dead_fold")
            return

        local_line, cur_stem = None, None
        if dead:
            # Fresh hlc on a tombstoned uid: RESURRECT — re-fold with the
            # content the tombstone carried.
            local_line = json.loads(shadow["fields_json"] or "{}").get("line")
        else:
            # Locate the current line GLOBALLY by highlight uid (the row is
            # the index): a same-uid put citing a different article must
            # merge with — and possibly move — the existing line, never fork
            # a second copy in another sidecar.
            hrow = conn.execute(
                "SELECT h.id AS hid, a.markdown_path FROM highlights h "
                "JOIN articles a ON a.id = h.article_id WHERE h.uid = ?",
                (op.uid,)).fetchone()
            if hrow is not None:
                cur_stem = sidecar_stem(hrow["markdown_path"])
                local_line = next(
                    (ln for ln in read_annotations(config, cur_stem)
                     if ln.get("uid") == op.uid), None)

        folded = _fold_line(local_line, incoming)
        target = conn.execute(
            "SELECT id, markdown_path FROM articles WHERE uid = ?",
            (folded.get("article_uid"),)).fetchone()
        if target is None:
            report.deferred += 1
            report._count(op, "deferred_unknown_article")
            return
        target_stem = sidecar_stem(target["markdown_path"])
        moved = cur_stem is not None and cur_stem != target_stem

        if not dead and not moved and local_line is not None and folded == local_line:
            if stale:
                # True replay / already-superseded content: nothing changes.
                report.skipped_stale += 1
                report._count(op, "skipped_stale")
                return
            # Newer hlc, identical content: advance the watermark only.
            shadow_upsert(conn, "highlight", op.uid,
                          hash=content_hash(canonical_json(local_line)),
                          fields={"article_uid": local_line.get("article_uid"),
                                  "line": local_line,
                                  "path_hint": f"annotations/{target_stem}.jsonl"},
                          hlc=new_wm)
            conn.commit()
            report.applied += 1
            report._count(op, "fast_forward_noop")
            return

        # FILE FIRST (sidecar-first invariant): move out of the old sidecar
        # when the fold winner's article changed, then rewrite the target.
        if moved:
            old_lines = read_annotations(config, cur_stem)
            write_annotations(config, cur_stem,
                              [ln for ln in old_lines if ln.get("uid") != op.uid])
        tlines = read_annotations(config, target_stem)
        write_annotations(config, target_stem, merge_jsonl(
            [ln for ln in tlines if ln.get("uid") != op.uid] + [folded], []))
        # ROW SECOND — from the RE-READ line, not the in-memory merge result:
        # write_annotations projects onto _FIELD_ORDER (unknown wire keys
        # dropped, missing keys -> None), so hashing/storing the in-memory
        # dict could diverge from what build_manifest's _add_highlights will
        # compute from disk next cycle (phantom LinePut echo). The shadow row
        # must byte-match the manifest entry: same hash space
        # (content_hash(canonical_json(disk line))), same fields shape
        # (article_uid/line/path_hint — path_hint keeps the unreadable-
        # protection guards in diff/save_shadow structurally sound).
        merged_line = next(ln for ln in read_annotations(config, target_stem)
                           if ln["uid"] == op.uid)
        _highlight_row_from_line(conn, target["id"], merged_line)
        shadow_upsert(conn, "highlight", op.uid,
                      hash=content_hash(canonical_json(merged_line)),
                      fields={"article_uid": merged_line.get("article_uid"),
                              "line": merged_line,
                              "path_hint": f"annotations/{target_stem}.jsonl"},
                      hlc=new_wm)
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
        _ensure_sidecar(config, conn, op.article_uid)
        shadow = _shadow_get(conn, "highlight", op.uid)
        wm = shadow["hlc"] if shadow and shadow["hlc"] else None
        if wm is not None and op.hlc.to_str() <= wm:
            report.skipped_stale += 1
            report._count(op, "skipped_stale")
            return
        if shadow and shadow["deleted_at"]:
            # Already dead: advance the watermark, KEEP the carried fold
            # (resetting fields here would drop content a resurrecting put
            # is entitled to re-fold).
            stored_fields = json.loads(shadow["fields_json"] or "{}")
            shadow_tombstone(conn, "highlight", op.uid, hlc=op.hlc.to_str(),
                             fields=stored_fields)
            conn.commit()
            report.tombstones += 1
            report._count(op, "already_dead")
            return
        # Locate GLOBALLY by highlight uid first — the envelope's article_uid
        # may lag a fold-driven move/alias; falling back to it covers a
        # sidecar line the derived index lost.
        hrow = conn.execute(
            "SELECT h.id AS hid, a.markdown_path FROM highlights h "
            "JOIN articles a ON a.id = h.article_id WHERE h.uid = ?",
            (op.uid,)).fetchone()
        if hrow is not None:
            stem = sidecar_stem(hrow["markdown_path"])
        else:
            stem = _stem_for_article(conn, op.article_uid)
        if stem is None:
            # Nothing local to delete — tombstone so a late line_put stays dead.
            shadow_tombstone(conn, "highlight", op.uid, hlc=op.hlc.to_str())
            conn.commit()
            report.tombstones += 1
            report._count(op, "tombstone_no_local")
            return
        lines = read_annotations(config, stem)
        target = next((ln for ln in lines if ln.get("uid") == op.uid), None)
        if target is not None:
            # A synced delete ALWAYS preserves a non-empty note as an
            # article-level conflict note (apply-level no-note-loss: a note
            # must never vanish silently, even when the remover had observed
            # it — retention bias over tidiness; observed_updated_at stays on
            # the wire for provenance but no longer gates preservation).
            note = target.get("note_markdown")
            if note and note.strip():
                dest = write_conflict_file(notes_dir(config), stem, note,
                                           device=op.device)
                report.resurrected += 1
                report._count(op, "note_resurrected", conflict_file=dest.name)
            # FILE FIRST: drop the line. An emptied sidecar stays as an EMPTY
            # file (write_annotations never unlinks; reconcile parses it as
            # zero lines and the mass-delete guard counts its stem present).
            write_annotations(config, stem,
                              [ln for ln in lines if ln.get("uid") != op.uid])
        if hrow:
            conn.execute("DELETE FROM notes WHERE highlight_id = ?",
                         (hrow["hid"],))
            conn.execute("DELETE FROM highlights WHERE id = ?", (hrow["hid"],))
        # The tombstone CARRIES the killed fold so a higher-hlc put can
        # resurrect the full content (convergence model above).
        fields = ({"article_uid": target.get("article_uid"), "line": target}
                  if target is not None else {})
        shadow_tombstone(conn, "highlight", op.uid, hlc=op.hlc.to_str(),
                         fields=fields)
        conn.commit()
        report.applied += 1
        report.tombstones += 1
        report._count(op, "applied")
    finally:
        conn.close()


# --- meta / row / link / article-tombstone ops ----------------------------------


def _metats_get(conn, article_uid: str, field: str) -> str | None:
    row = conn.execute(
        "SELECT fields_json FROM sync_shadow WHERE kind = 'metats' AND uid = ?",
        (f"{article_uid}:{field}",)).fetchone()
    if row is None:
        return None
    return json.loads(row["fields_json"] or "{}").get("ts")


def _metats_set(conn, article_uid: str, field: str, ts: str) -> None:
    conn.execute(
        "INSERT INTO sync_shadow (kind, uid, hash, fields_json, hlc, deleted_at) "
        "VALUES ('metats', ?, NULL, ?, NULL, NULL) "
        "ON CONFLICT(kind, uid) DO UPDATE SET fields_json = excluded.fields_json",
        (f"{article_uid}:{field}", canonical_json({"ts": ts})))


def _current_meta_value(conn, article_id: int, field: str):
    """Current local value of an allowlisted meta field (callers guard via
    META_FIELDS before interpolation). source_uid resolves through the
    sources join — comparisons must be uid-vs-uid (S2.6 review F1)."""
    if field == "source_uid":
        return conn.execute(
            "SELECT s.uid AS v FROM articles a "
            "LEFT JOIN sources s ON s.id = a.source_id "
            "WHERE a.id = ?", (article_id,)).fetchone()["v"]
    return conn.execute(
        f"SELECT {field} AS v FROM articles WHERE id = ?",
        (article_id,)).fetchone()["v"]


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
        # PER-FIELD LWW clock (Task-8 property fix): articles carry ONE
        # meta_updated_at across all meta fields, so gating on it coupled
        # the fields — Meta(rating, ts=5) arriving after Meta(is_read, ts=9)
        # was skipped as "stale" on one device and applied on another,
        # diverging on the rating. Applied per-field clocks live in
        # sync_shadow kind='metats' rows (uid "{article_uid}:{field}"),
        # written here only, skipped by load_shadow (never diffed, never
        # tombstoned by save_shadow). meta_updated_at itself is bumped
        # monotonically below purely as the diff-side emission stamp.
        local_ts = _metats_get(conn, op.uid, op.field)
        # UN-PUSHED LOCAL EDIT protection (S2.8 review Blocker): metats only
        # tracks the last-SYNCED per-field ts, while local route writes
        # (rate/read/snooze) bump only articles.meta_updated_at — gating on
        # metats alone let an OLDER remote op overwrite a NEWER un-pushed
        # local edit (LWW inversion, self-cementing via the next diff).
        # Mirror of decision #8's file rule (the local side counts as
        # "unchanged since last sync" only when it matches the shadow):
        # when the article's shadow entry STORES this field and the current
        # value differs — a local edit sync hasn't captured yet — the local
        # clock is max(metats, meta_updated_at). Apply-written shadow rows
        # carry only path_hint (no meta fields) until the next save_shadow,
        # so the protection engages only on properly-synced state; the
        # bootstrap window (un-pushed edit before the first full
        # save_shadow) remains unprotected — documented, bounded to one
        # cycle, and the local diff re-emits the local value either way.
        if op.field != "opened_count":
            srow = conn.execute(
                "SELECT fields_json FROM sync_shadow WHERE kind = 'article' "
                "AND uid = ? AND deleted_at IS NULL", (op.uid,)).fetchone()
            if srow:
                sfields = json.loads(srow["fields_json"] or "{}")
                if op.field in sfields:
                    cur_v = _current_meta_value(conn, row["id"], op.field)
                    if sfields.get(op.field) != cur_v:
                        mu = row["meta_updated_at"]
                        if mu and (local_ts is None or mu > local_ts):
                            local_ts = mu
        if not op.ts and (local_ts is not None or row["meta_updated_at"]):
            # S2.6 review F7: a ts-less (None/"") meta op must never
            # overwrite a stamped field — it would bypass both LWW guards
            # AND regress the clock. Mirror of the NULL-local-loses rule.
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
            if canonical_json(op.value) <= canonical_json(
                    _current_meta_value(conn, row["id"], op.field)):
                report.skipped_stale += 1
                report._count(op, "skipped_stale_tie")
                return
        # meta_updated_at is bumped MONOTONICALLY (never regressed): with
        # per-field clocks a legitimately-applied op can carry a ts older
        # than another field's — the column is the diff-side emission stamp,
        # not the LWW authority anymore.
        if op.field == "source_uid":
            src = conn.execute("SELECT id FROM sources WHERE uid = ?",
                               (op.value,)).fetchone()
            if src is None:
                report.deferred += 1
                report._count(op, "deferred_unknown_source")
                return
            conn.execute(
                "UPDATE articles SET source_id = ?, "
                "meta_updated_at = MAX(COALESCE(meta_updated_at, ''), ?) "
                "WHERE id = ?", (src["id"], op.ts, row["id"]))
        else:  # rating / is_read / snoozed_until — allowlisted identifiers only
            conn.execute(
                f"UPDATE articles SET {op.field} = ?, "
                "meta_updated_at = MAX(COALESCE(meta_updated_at, ''), ?) "
                "WHERE id = ?", (op.value, op.ts, row["id"]))
        _metats_set(conn, op.uid, op.field, op.ts or "")
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


# --- alias ops (Task 7: URL dedupe companions, spec §4 dedupe row) --------------


def _record_alias(conn, old_uid: str, new_uid: str) -> None:
    """Persist old_uid -> new_uid as a sync_shadow kind='alias' row —
    deleted_at stays NULL and expire_tombstones exempts the kind anyway
    (decision #18: alias mappings never age out)."""
    conn.execute(
        "INSERT INTO sync_shadow (kind, uid, hash, fields_json, hlc, deleted_at) "
        "VALUES ('alias', ?, NULL, ?, NULL, NULL) "
        "ON CONFLICT(kind, uid) DO UPDATE SET fields_json = excluded.fields_json",
        (old_uid, canonical_json({"new_uid": new_uid})))


def _rewrite_local_uid(config: TiroConfig, conn, old_uid: str,
                       new_uid: str) -> None:
    """The local article loses the uid contest: adopt the surviving uid.
    Rows keyed by article_id are untouched; only articles.uid and the
    sidecar lines' article_uid field need rewriting."""
    from tiro.annotations import read_annotations, sidecar_stem, write_annotations

    row = conn.execute("SELECT id, markdown_path FROM articles WHERE uid = ?",
                       (old_uid,)).fetchone()
    if row is None:
        return
    conn.execute("UPDATE articles SET uid = ? WHERE id = ?",
                 (new_uid, row["id"]))
    stem = sidecar_stem(row)
    lines = read_annotations(config, stem)
    if lines:
        for ln in lines:
            if ln.get("article_uid") == old_uid:
                ln["article_uid"] = new_uid
        write_annotations(config, stem, lines)
    # Shadow entries for the old uid are now dead weight; tombstone them.
    # UPDATE (not shadow_tombstone) on purpose: only rows that EXIST get
    # tombstoned — never fabricate a tombstone for a never-synced entry.
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for kind in ("article", "note"):
        conn.execute(
            "UPDATE sync_shadow SET deleted_at = ? WHERE kind = ? AND uid = ?",
            (now, kind, old_uid))
    # Highlight shadow rows are DELIBERATELY left alone: they key by the
    # highlight's own uid, and the old article_uid only lives inside their
    # fields' `line` dict. The next build_manifest reads the rewritten
    # sidecar lines from disk, so the fields/hash comparison in diff/
    # save_shadow re-stamps them (a bounded one-cycle LinePut echo, same
    # accepted posture as the materialize branch's Meta echo) — NOT an
    # oversight for the whole-branch review to flag.


def _apply_alias(config: TiroConfig, op: Alias, report: ApplyReport) -> None:
    """alias(old_uid, new_uid): repoint everything from old to new, then
    remove the duplicate old article via delete_article (its sidecar lines
    are moved to the survivor FIRST — delete_article would destroy them)."""
    from tiro.annotations import (
        notes_dir,
        read_annotations,
        read_note,
        sidecar_stem,
        write_annotations,
        write_note,
    )

    if op.uid == op.new_uid:
        # A self-alias would repoint rows onto themselves and then hand the
        # SURVIVOR to delete_article — refuse (per-op error via apply_ops).
        raise ValueError(f"self-referential alias for uid {op.uid!r}")

    conn = get_connection(config.db_path)
    try:
        _record_alias(conn, op.uid, op.new_uid)
        old = conn.execute("SELECT * FROM articles WHERE uid = ?",
                           (op.uid,)).fetchone()
        new = conn.execute("SELECT * FROM articles WHERE uid = ?",
                           (op.new_uid,)).fetchone()
        if old is None:
            # Nothing local under the dead uid (already applied, or the
            # duplicate never reached this device): keep the mapping only.
            conn.commit()
            report.applied += 1
            report._count(op, "applied_mapping_only")
            return
        if new is None:
            # Survivor not here yet — mapping persisted so later ops (and a
            # re-delivered alias after the survivor's file_put) re-target.
            conn.commit()
            report.deferred += 1
            report._count(op, "deferred_survivor_absent")
            return

        # 1. Sidecar lines old-stem -> new-stem, FILE FIRST (M2.1 invariant:
        #    a crash leaves truth ahead of the derived rows). Lines travel
        #    VERBATIM except article_uid (the Task-5 validation contract).
        old_stem, new_stem = sidecar_stem(old), sidecar_stem(new)
        old_lines = read_annotations(config, old_stem)
        if old_lines:
            for ln in old_lines:
                ln["article_uid"] = op.new_uid
            merged = merge_jsonl(read_annotations(config, new_stem), old_lines,
                                 label_a="local", label_b=op.device)
            write_annotations(config, new_stem, merged)
            # Emptied, not unlinked (write primitive is not a policy) — the
            # file itself is removed by delete_article below.
            write_annotations(config, old_stem, [])

        # 2. Article-level note: survivor keeps its own; a differing loser
        #    note is preserved as a conflict file (never merged silently) —
        #    device-label sanitization happens inside write_conflict_file.
        note_moved = False
        old_note = read_note(config, old_stem)
        if old_note is not None:
            new_note = read_note(config, new_stem)
            if new_note is None:
                write_note(config, new_stem, old_note)
                note_moved = True
            elif old_note != new_note:
                write_conflict_file(notes_dir(config), new_stem, old_note,
                                    device=op.device)
                report.conflicts += 1

        # 3. Repoint SQLite rows keyed by article_id. UNIQUE collisions on
        #    the junction PKs (same tag on both) leave stale rows on the old
        #    id under OR IGNORE; delete_article clears them below (verified:
        #    lifecycle.py still deletes all junctions by article_id).
        for table in ("article_tags", "article_entities", "article_authors",
                      "highlights"):
            conn.execute(
                f"UPDATE OR IGNORE {table} SET article_id = ? WHERE article_id = ?",
                (new["id"], old["id"]))
        # Highlight-anchored note rows travel with their highlights; the
        # loser's ARTICLE-LEVEL note row moves only when its FILE moved —
        # a wholesale repoint would hand the survivor a SECOND
        # highlight_id-IS-NULL row (the singleton every note route/
        # reconcile pass fetchone()s on). When it stays, delete_article
        # cleans it (its body already preserved as a conflict file above,
        # or identical to the survivor's).
        conn.execute(
            "UPDATE OR IGNORE notes SET article_id = ? "
            "WHERE article_id = ? AND highlight_id IS NOT NULL",
            (new["id"], old["id"]))
        if note_moved:
            conn.execute(
                "UPDATE OR IGNORE notes SET article_id = ? "
                "WHERE article_id = ? AND highlight_id IS NULL",
                (new["id"], old["id"]))
        conn.execute(
            "UPDATE OR IGNORE article_relations SET article_id = ? "
            "WHERE article_id = ?", (new["id"], old["id"]))
        conn.execute(
            "UPDATE OR IGNORE article_relations SET related_article_id = ? "
            "WHERE related_article_id = ?", (new["id"], old["id"]))
        conn.execute(
            "DELETE FROM article_relations WHERE article_id = related_article_id")
        # Tombstone the dead uid's article/note shadow rows at op.hlc so a
        # late, stale file_put for the old uid gates as skipped_stale instead
        # of resurrecting the duplicate as a fresh materialize.
        shadow_tombstone(conn, "article", op.uid, hlc=op.hlc.to_str())
        shadow_tombstone(conn, "note", op.uid, hlc=op.hlc.to_str())
        conn.commit()
        old_id = old["id"]
    finally:
        conn.close()
    # The repointed highlights keep their uids, but their shadow rows'
    # fields still cite the OLD article_uid inside `line`/`article_uid` and
    # the old stem in path_hint. The next build_manifest/save_shadow cycle
    # heals this from the rewritten on-disk sidecar (fields comparison ->
    # fresh hlc stamp) — proven end-to-end by
    # tests/test_sync_alias.py::test_alias_heals_highlight_manifest_entries.
    from tiro.lifecycle import delete_article
    delete_article(config, old_id)  # clears the duplicate row/file/leftovers
    report.applied += 1
    report._count(op, "applied")
