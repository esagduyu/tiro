"""Manifest build + shadow store + diff (sync S2 — pure merge core).

build_manifest snapshots the SYNC SET (spec §2) as (kind, uid)-keyed
entries; sync_shadow (migration 016) persists the same shape as of the last
sync; diff derives journal ops from the two (Task 3). PURE: reads files
only under config.library and SQLite only at config.db_path; zero network
(test-enforced).

Kinds and identity (plan decisions #4/#5):
  article    uid=articles.uid   hash=body_hash    fields=path_hint/url/meta
  note       uid=article uid    hash=sha256(body) fields=path_hint
  wiki       uid=page uid       hash=sha256(body) fields=path_hint
  pathfile   uid="path:{rel}"   hash=sha256(body) fields=path_hint
             (conflict files + wiki/_schema.md + uid-less wiki pages)
  highlight  uid=line uid       hash=sha256(canonical line) fields=article_uid/line
  row:<t>    uid=row uid (digests: "{date}:{type}")  fields=durable columns
  link:<t>   uid="{a_uid}:{b_uid}"                   fields=a_uid/b_uid(+extras)

NEVER in the manifest (spec §2): ChromaDB, audio, reading_sessions,
reading_stats, auth tables, feeds/feed_entries (device-local subscriptions
travel via export/backup, not sync — S5 may revisit), backups/, config.

Accepted limitation (S2.3 review nit, deliberate): frontmatter-ONLY article
edits (e.g. hand-fixing a title in the file) are invisible to change
detection — article hashes are BODY-space (frontmatter-stripped, the S1
body_hash decision) — and piggyback on the next body change instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import frontmatter

from tiro.anchors import content_hash
from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.migrations import new_ulid
from tiro.sync.journal import (
    TOMBSTONE_TTL_DAYS,
    FileDel,
    FilePut,
    HLCClock,
    LineDel,
    LinePut,
    Meta,
    Op,
    RowDel,
    RowPut,
    canonical_json,
)
from tiro.sync.reconcile import body_hash_of_file, is_conflict_file

logger = logging.getLogger(__name__)

ROW_TABLES = ("sources", "authors", "tags", "entities", "saved_views", "digests")
LINK_TABLES = ("article_tags", "article_entities", "article_authors", "article_relations")
# Article meta fields (spec §4 row 6/7 + repoint ops; is_vip is deliberately
# absent — VIP is source/author-level, synced via row:sources/row:authors).
META_FIELDS = ("rating", "is_read", "snoozed_until", "opened_count", "source_uid")

_ROW_COLUMNS = {
    "sources": ("uid", "name", "domain", "email_sender", "source_type", "is_vip", "created_at"),
    "authors": ("uid", "name", "canonical_key", "is_vip", "created_at"),
    "tags": ("uid", "name"),
    "entities": ("uid", "name", "entity_type", "canonical_key"),
    "saved_views": ("uid", "name", "filter_json", "sort_mode", "position", "created_at"),
    "digests": ("date", "digest_type", "content", "article_ids", "created_at"),
}

_LINK_SQL = {
    # (left table alias for a_uid, right for b_uid, extra columns)
    "article_tags": (
        "SELECT a.uid AS a_uid, t.uid AS b_uid FROM article_tags j "
        "JOIN articles a ON a.id = j.article_id JOIN tags t ON t.id = j.tag_id",
        (),
    ),
    "article_entities": (
        "SELECT a.uid AS a_uid, e.uid AS b_uid FROM article_entities j "
        "JOIN articles a ON a.id = j.article_id JOIN entities e ON e.id = j.entity_id",
        (),
    ),
    "article_authors": (
        "SELECT a.uid AS a_uid, au.uid AS b_uid FROM article_authors j "
        "JOIN articles a ON a.id = j.article_id JOIN authors au ON au.id = j.author_id",
        (),
    ),
    "article_relations": (
        "SELECT a.uid AS a_uid, b.uid AS b_uid, j.similarity_score, "
        "j.connection_note FROM article_relations j "
        "JOIN articles a ON a.id = j.article_id "
        "JOIN articles b ON b.id = j.related_article_id",
        ("similarity_score", "connection_note"),
    ),
}


@dataclass(frozen=True)
class ManifestEntry:
    kind: str
    uid: str
    hash: str | None
    fields: dict
    hlc: str | None = None  # populated on shadow entries only


@dataclass
class Manifest:
    entries: dict[tuple[str, str], ManifestEntry] = field(default_factory=dict)
    # Library-relative path_hints of files that EXIST but could not be read
    # (permission blip, non-UTF-8 bytes, iCloud lazy materialization — spec
    # §10 risk). Their entries are absent from `entries`, but consumers
    # (save_shadow, diff) must treat them as UNKNOWN, never as deleted —
    # tombstoning an unreadable file would propagate a delete to every
    # other device (S2.1+S2.2 review, Major #2).
    unreadable: set[str] = field(default_factory=set)

    def add(self, entry: ManifestEntry) -> None:
        self.entries[(entry.kind, entry.uid)] = entry


@dataclass
class Shadow:
    entries: dict[tuple[str, str], ManifestEntry] = field(default_factory=dict)
    tombstones: dict[tuple[str, str], str] = field(default_factory=dict)  # -> deleted_at
    aliases: dict[str, str] = field(default_factory=dict)  # old_uid -> new_uid


def _fields_hash(fields: dict) -> str:
    return content_hash(canonical_json(fields))


def _read_text(path: Path) -> str | None:
    # Explicit utf-8: sync hashes must be byte-stable across devices and
    # platforms — never locale-dependent.
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Manifest: unreadable file %s: %s", path, e)
        return None


def build_manifest(config: TiroConfig) -> Manifest:
    """FROZEN signature. Snapshot the sync set as of NOW."""
    m = Manifest()
    conn = get_connection(config.db_path)
    try:
        _add_articles(config, conn, m)
        _add_rows(conn, m)
        _add_links(conn, m)
    finally:
        conn.close()
    _add_notes(config, m)
    _add_wiki(config, m)
    _add_pathfiles(config, m)
    _add_highlights(config, m)
    return m


def _add_articles(config: TiroConfig, conn, m: Manifest) -> None:
    rows = conn.execute(
        "SELECT a.uid, a.markdown_path, a.url, a.rating, a.is_read, "
        "a.snoozed_until, a.opened_count, a.meta_updated_at, a.body_hash, "
        "s.uid AS source_uid "
        "FROM articles a LEFT JOIN sources s ON s.id = a.source_id"
    ).fetchall()
    for r in rows:
        if not r["uid"]:
            continue  # pre-uid legacy rows: migration 002 backfilled; belt+braces
        name = Path(r["markdown_path"]).name
        h = r["body_hash"]
        if h is None:
            # NULL = unhashed baseline (S1 decision #7): fall back to disk.
            # MUST be the frontmatter-stripped BODY hash (body_hash_of_file),
            # matching migration 015's backfill and ingest stamping — hashing
            # the raw file text would make this entry permanently unequal to
            # every stamped body_hash on every device (review Major #1).
            h = body_hash_of_file(config.articles_dir / name)
            if h is None:
                m.unreadable.add(f"articles/{name}")
        m.add(ManifestEntry(
            kind="article", uid=r["uid"], hash=h,
            fields={
                "path_hint": f"articles/{name}",
                "url": r["url"] or "",
                "rating": r["rating"],
                "is_read": int(bool(r["is_read"])),
                "snoozed_until": r["snoozed_until"],
                "opened_count": int(r["opened_count"] or 0),
                "source_uid": r["source_uid"],
                "meta_updated_at": r["meta_updated_at"],
            },
        ))


def _add_notes(config: TiroConfig, m: Manifest) -> None:
    from tiro.annotations import notes_dir

    nt_dir = notes_dir(config)
    if not nt_dir.exists():
        return
    stem_to_uid = {
        Path(e.fields["path_hint"]).stem: e.uid
        for (k, _u), e in m.entries.items() if k == "article"
    }
    for path in sorted(nt_dir.glob("*.md")):
        if is_conflict_file(path.name):
            continue  # conflict files are pathfile entries
        uid = stem_to_uid.get(path.stem)
        if uid is None:
            continue  # orphan note: doctor's concern, not sync's
        body = _read_text(path)
        if body is None:
            m.unreadable.add(f"notes/{path.name}")
            continue
        m.add(ManifestEntry(
            kind="note", uid=uid, hash=content_hash(body),
            fields={"path_hint": f"notes/{path.name}"},
        ))


def _add_wiki(config: TiroConfig, m: Manifest) -> None:
    if not config.wiki_dir.exists():
        return
    for path in sorted(config.wiki_dir.rglob("*.md")):
        rel = path.relative_to(config.wiki_dir).as_posix()
        if is_conflict_file(path.name) or path.name == "_schema.md":
            continue  # -> pathfile entries
        body = _read_text(path)
        if body is None:
            m.unreadable.add(f"wiki/{rel}")
            continue
        try:
            uid = (frontmatter.loads(body).metadata or {}).get("uid") or ""
        except Exception:
            uid = ""
        if not uid:
            # Hand-made/broken page without a uid: sync it path-keyed.
            m.add(ManifestEntry(
                kind="pathfile", uid=f"path:wiki/{rel}",
                hash=content_hash(body), fields={"path_hint": f"wiki/{rel}"},
            ))
            continue
        if ("wiki", str(uid)) in m.entries:
            logger.warning(
                "Manifest: duplicate wiki uid %s — %s overwrites %s in the "
                "sync set", uid, rel,
                m.entries[("wiki", str(uid))].fields.get("path_hint"))
        m.add(ManifestEntry(
            kind="wiki", uid=str(uid), hash=content_hash(body),
            fields={"path_hint": f"wiki/{rel}"},
        ))


def _add_pathfiles(config: TiroConfig, m: Manifest) -> None:
    from tiro.annotations import notes_dir

    candidates: list[tuple[str, Path]] = []
    if config.articles_dir.exists():
        candidates += [
            (f"articles/{p.name}", p)
            for p in sorted(config.articles_dir.glob("*.md"))
            if is_conflict_file(p.name)
        ]
    nt_dir = notes_dir(config)
    if nt_dir.exists():
        candidates += [
            (f"notes/{p.name}", p)
            for p in sorted(nt_dir.glob("*.md")) if is_conflict_file(p.name)
        ]
    if config.wiki_dir.exists():
        schema = config.wiki_dir / "_schema.md"
        if schema.exists():
            candidates.append(("wiki/_schema.md", schema))
        candidates += [
            (f"wiki/{p.relative_to(config.wiki_dir).as_posix()}", p)
            for p in sorted(config.wiki_dir.rglob("*.md"))
            if is_conflict_file(p.name)
        ]
    for rel, path in candidates:
        body = _read_text(path)
        if body is None:
            m.unreadable.add(rel)
            continue
        m.add(ManifestEntry(
            kind="pathfile", uid=f"path:{rel}", hash=content_hash(body),
            fields={"path_hint": rel},
        ))


def _add_highlights(config: TiroConfig, m: Manifest) -> None:
    # _parse_jsonl_lines is annotations' private parser, used here instead of
    # read_annotations because the MALFORMED COUNT is load-bearing for sync
    # (S2.3 review Major #2): read_annotations hides it, and a sidecar whose
    # lines are corrupt-but-readable (iCloud/Dropbox partial materialization,
    # spec §10) must mark the file unreadable so its invisible lines are
    # never diffed into LineDels — highlights join the same unreadable
    # contract notes/wiki/articles already have.
    from tiro.annotations import _parse_jsonl_lines, annotations_dir

    an_dir = annotations_dir(config)
    if not an_dir.exists():
        return
    for path in sorted(an_dir.glob("*.jsonl")):
        rel = f"annotations/{path.name}"
        try:
            lines, malformed = _parse_jsonl_lines(path)
        except (OSError, UnicodeDecodeError, ValueError) as e:
            logger.warning("Manifest: unreadable sidecar %s: %s", path, e)
            m.unreadable.add(rel)
            continue
        if malformed:
            # Sync the lines that ARE readable, but protect the rest: with
            # the file marked unreadable, diff/save_shadow never treat the
            # missing lines as deleted, and shadow rows for this sidecar
            # don't advance (conservative lag; re-diffed when clean).
            logger.warning(
                "Manifest: %d malformed line(s) in %s — sidecar marked "
                "unreadable, its absent highlights are protected from "
                "delete propagation", malformed, path)
            m.unreadable.add(rel)
        for line in lines:
            uid = line.get("uid")
            if not uid:
                continue
            m.add(ManifestEntry(
                kind="highlight", uid=uid,
                hash=content_hash(canonical_json(line)),
                fields={"article_uid": line.get("article_uid"), "line": line,
                        "path_hint": rel},
            ))


def _add_rows(conn, m: Manifest) -> None:
    for table in ROW_TABLES:
        cols = _ROW_COLUMNS[table]
        for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall():
            fields = {c: r[c] for c in cols}
            if table == "digests":
                uid = f"{r['date']}:{r['digest_type']}"
            else:
                uid = r["uid"]
                if not uid:
                    continue
            m.add(ManifestEntry(
                kind=f"row:{table}", uid=uid,
                hash=_fields_hash(fields), fields=fields,
            ))


def _add_links(conn, m: Manifest) -> None:
    for table in LINK_TABLES:
        sql, extras = _LINK_SQL[table]
        for r in conn.execute(sql).fetchall():
            if not r["a_uid"] or not r["b_uid"]:
                continue
            fields = {"a_uid": r["a_uid"], "b_uid": r["b_uid"]}
            for c in extras:
                fields[c] = r[c]
            m.add(ManifestEntry(
                kind=f"link:{table}", uid=f"{r['a_uid']}:{r['b_uid']}",
                hash=_fields_hash(fields), fields=fields,
            ))


# --- shadow store -------------------------------------------------------------


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_shadow(config: TiroConfig) -> Shadow:
    import json as _json

    s = Shadow()
    conn = get_connection(config.db_path)
    try:
        for r in conn.execute(
            "SELECT kind, uid, hash, fields_json, hlc, deleted_at FROM sync_shadow"
        ).fetchall():
            if r["kind"] == "alias":
                fields = _json.loads(r["fields_json"] or "{}")
                s.aliases[r["uid"]] = fields.get("new_uid", "")
                continue
            if r["kind"] == "metats":
                # Per-field meta LWW clocks (merge.py::_apply_meta) — apply-
                # side bookkeeping, not sync-set entries: skipping them here
                # keeps save_shadow from tombstoning them as "deleted" and
                # diff from ever seeing them.
                continue
            if r["deleted_at"]:
                s.tombstones[(r["kind"], r["uid"])] = r["deleted_at"]
                continue
            s.entries[(r["kind"], r["uid"])] = ManifestEntry(
                kind=r["kind"], uid=r["uid"], hash=r["hash"],
                fields=_json.loads(r["fields_json"] or "{}"), hlc=r["hlc"],
            )
    finally:
        conn.close()
    return s


def shadow_upsert(conn, kind: str, uid: str, *, hash: str | None,
                  fields: dict, hlc: str | None) -> None:
    conn.execute(
        "INSERT INTO sync_shadow (kind, uid, hash, fields_json, hlc, deleted_at) "
        "VALUES (?, ?, ?, ?, ?, NULL) "
        "ON CONFLICT(kind, uid) DO UPDATE SET hash = excluded.hash, "
        "fields_json = excluded.fields_json, hlc = excluded.hlc, deleted_at = NULL",
        (kind, uid, hash, canonical_json(fields), hlc),
    )


def shadow_tombstone(conn, kind: str, uid: str, *, hlc: str | None,
                     now: datetime | None = None,
                     fields: dict | None = None) -> None:
    """`fields` (default {}) lets a highlight tombstone CARRY the killed
    line's fold (merge.py's convergence model): a line_put with a newer hlc
    resurrects from it instead of losing every pre-delete contribution.
    load_shadow ignores tombstone fields, so other kinds are unaffected."""
    conn.execute(
        "INSERT INTO sync_shadow (kind, uid, hash, fields_json, hlc, deleted_at) "
        "VALUES (?, ?, NULL, ?, ?, ?) "
        "ON CONFLICT(kind, uid) DO UPDATE SET hash = NULL, "
        "fields_json = excluded.fields_json, "
        "hlc = excluded.hlc, deleted_at = excluded.deleted_at",
        (kind, uid, canonical_json(fields or {}), hlc, _now_iso(now)),
    )


def save_shadow(config: TiroConfig, manifest: Manifest, *,
                clock=None, now: datetime | None = None) -> None:
    """Persist `manifest` as the new shadow. Previous live entries absent
    from `manifest` become tombstones (deleted locally / by an applied op) —
    EXCEPT entries whose file was merely unreadable at build time
    (manifest.unreadable): those are carried forward unchanged, never
    tombstoned, so a transient read failure can never propagate as a
    delete to other devices. The engine (S5) calls this after a successful
    push; S2 tests call it directly. Alias rows are preserved untouched."""
    from tiro.sync.journal import HLCClock

    clock = clock or HLCClock("local")
    hlc_str = clock.tick().to_str()
    prev = load_shadow(config)
    conn = get_connection(config.db_path)
    try:
        for key, entry in manifest.entries.items():
            if entry.fields.get("path_hint") in manifest.unreadable:
                # Entry PRESENT but its file was unreadable at build time
                # (e.g. an article row whose NULL-body_hash disk fallback
                # failed, or a highlight from a partially-corrupt sidecar):
                # keep the previous shadow row untouched rather than
                # clobbering its hash with the failed read's result
                # (S2.3 review Minor #1).
                continue
            old = prev.entries.get(key)
            # Keep the existing hlc when nothing changed (monotone shadow).
            keep = old.hlc if (old and old.hash == entry.hash
                               and old.fields == entry.fields) else hlc_str
            shadow_upsert(conn, entry.kind, entry.uid,
                          hash=entry.hash, fields=entry.fields, hlc=keep)
        for key, old in prev.entries.items():
            if key in manifest.entries:
                continue
            if old.fields.get("path_hint") in manifest.unreadable:
                continue  # unreadable, not deleted — carry forward untouched
            shadow_tombstone(conn, key[0], key[1], hlc=hlc_str, now=now)
        conn.commit()
    finally:
        conn.close()


# --- diff ---------------------------------------------------------------------

_META_DEFAULTS = {"rating": None, "is_read": 0, "snoozed_until": None,
                  "opened_count": 0, "source_uid": None}


def _hlc_wall_iso(hlc) -> str:
    return datetime.fromtimestamp(hlc.wall_ms / 1000, UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _meta_ts(entry: ManifestEntry, hlc) -> str:
    return entry.fields.get("meta_updated_at") or _hlc_wall_iso(hlc)


def diff(manifest: Manifest, shadow: Shadow, *, clock=None) -> list[Op]:
    """FROZEN signature: derive journal ops from state vs shadow (spec §1's
    state-diff capture). Deterministic order: sorted by (kind, uid), puts
    before deletes. diff NEVER reads disk (FilePut.body stays None — see
    hydrate_bodies) and NEVER emits Alias ops (apply's dedupe owns those).

    manifest.unreadable is honored exactly like save_shadow does (S2.1+S2.2
    review, Major #2): an unreadable file is UNKNOWN, not deleted — a shadow
    entry whose path_hint is unreadable emits NO delete op, and a manifest
    entry whose path_hint is unreadable emits NO FilePut (its hash is a
    disk-read fallback that failed; article meta ops still flow — SQLite
    was readable)."""
    clock = clock or HLCClock("local")
    ops: list[Op] = []

    def _stamp():
        h = clock.tick()
        return {"op_id": new_ulid(), "hlc": h, "device": clock.device}, h

    for key in sorted(manifest.entries):
        entry = manifest.entries[key]
        prev = shadow.entries.get(key)
        changed_hash = prev is None or entry.hash != prev.hash
        unreadable = entry.fields.get("path_hint") in manifest.unreadable
        if entry.kind == "article":
            if changed_hash and not unreadable:
                base, h = _stamp()
                # object_hash here is the MANIFEST hash — BODY-space
                # (frontmatter-stripped) for articles — a placeholder until
                # hydrate_bodies replaces it with the full-file blob hash
                # (a DIFFERENT space; see hydrate_bodies' docstring). Apply
                # must never compare an article body hash to op.object_hash.
                ops.append(FilePut(**base, uid=entry.uid,
                                   path_hint=entry.fields["path_hint"],
                                   object_hash=entry.hash or "",
                                   base_hash=prev.hash if prev else None))
            prev_fields = prev.fields if prev else _META_DEFAULTS
            for f in META_FIELDS:
                cur_v = entry.fields.get(f, _META_DEFAULTS.get(f))
                old_v = prev_fields.get(f, _META_DEFAULTS.get(f))
                if cur_v != old_v:
                    base, h = _stamp()
                    ops.append(Meta(**base, uid=entry.uid, field=f,
                                    value=cur_v, ts=_meta_ts(entry, h)))
        elif entry.kind in ("note", "wiki", "pathfile"):
            if changed_hash and not unreadable:
                base, _h = _stamp()
                ops.append(FilePut(**base, uid=entry.uid,
                                   path_hint=entry.fields["path_hint"],
                                   object_hash=entry.hash or "",
                                   base_hash=prev.hash if prev else None))
        elif entry.kind == "highlight":
            if changed_hash or (prev and entry.fields != prev.fields):
                base, _h = _stamp()
                ops.append(LinePut(**base, uid=entry.uid,
                                   article_uid=entry.fields["article_uid"],
                                   line=entry.fields["line"]))
        elif entry.kind.startswith("row:"):
            if changed_hash or (prev and entry.fields != prev.fields):
                base, _h = _stamp()
                ops.append(RowPut(**base, uid=entry.uid,
                                  table=entry.kind.split(":", 1)[1],
                                  row=dict(entry.fields)))
        elif entry.kind.startswith("link:"):
            if prev is None:
                base, _h = _stamp()
                ops.append(RowPut(**base, uid=entry.uid,
                                  table=entry.kind.split(":", 1)[1],
                                  row=dict(entry.fields)))
            elif entry.fields != prev.fields:  # relations note/score changed
                base, _h = _stamp()
                ops.append(RowPut(**base, uid=entry.uid,
                                  table=entry.kind.split(":", 1)[1],
                                  row=dict(entry.fields)))

    for key in sorted(shadow.entries):
        if key in manifest.entries:
            continue
        prev = shadow.entries[key]
        if prev.fields.get("path_hint") in manifest.unreadable:
            continue  # unreadable, not deleted — mirrors save_shadow
        # _stamp() inside each branch: an unhandled kind must not burn a
        # clock tick + ULID without emitting an op (S2.3 review nit).
        if prev.kind == "article":
            base, _h = _stamp()
            ops.append(RowDel(**base, uid=prev.uid, table="articles",
                              observed=prev.hash))
        elif prev.kind in ("note", "wiki", "pathfile"):
            base, _h = _stamp()
            ops.append(FileDel(**base, uid=prev.uid,
                               path_hint=prev.fields.get("path_hint", ""),
                               base_hash=prev.hash))
        elif prev.kind == "highlight":
            line = prev.fields.get("line") or {}
            base, _h = _stamp()
            ops.append(LineDel(**base, uid=prev.uid,
                               article_uid=prev.fields.get("article_uid", ""),
                               observed_updated_at=line.get("updated_at")))
        elif prev.kind.startswith("row:"):
            base, _h = _stamp()
            ops.append(RowDel(**base, uid=prev.uid,
                              table=prev.kind.split(":", 1)[1], observed=None))
        elif prev.kind.startswith("link:"):
            base, _h = _stamp()
            ops.append(RowDel(**base, uid=prev.uid,
                              table=prev.kind.split(":", 1)[1],
                              observed=prev.hlc))
    return ops


def hydrate_bodies(config: TiroConfig, ops: list[Op]) -> list[Op]:
    """Fill FilePut.body from disk (push-path helper — spec §6.4 uploads
    objects first, and apply_ops requires hydrated bodies). Path hints are
    library-relative; a file that vanished since diff is dropped with a
    WARNING (it will re-diff next cycle). Non-file ops pass through.

    HASH SPACES (S2.3 review Major #1): for ARTICLES the hydrated
    object_hash — sha256 of the FULL file text, the content address S3's
    objects/ store keys blobs on — and base_hash — BODY-space,
    frontmatter-stripped, the edit-wins baseline — are DIFFERENT spaces,
    always (every article has frontmatter). Apply-side rules (Task 4 /
    decision #8) must therefore compare base_hash against
    body_hash_of_file(current file), treat object_hash purely as a blob
    address, and store BODY-space hashes into sync_shadow so the receiver's
    next build_manifest doesn't see a phantom change. For notes/wiki/
    pathfiles the two spaces coincide (whole file = body)."""
    out: list[Op] = []
    for op in ops:
        if not isinstance(op, FilePut) or op.body is not None:
            out.append(op)
            continue
        path = config.library / op.path_hint
        body = _read_text(path)
        if body is None:
            logger.warning("hydrate_bodies: %s vanished since diff — op dropped",
                           op.path_hint)
            continue
        out.append(replace(op, body=body, object_hash=content_hash(body)))
    return out


def clear_shadow(config: TiroConfig) -> None:
    """Repair-epoch reset (S5.5): wipe sync_shadow entries AND tombstones so
    the next diff re-emits the full local state as a fresh push. Two kinds
    survive by design: `alias` rows are permanent uid mappings (deleting
    them would let late ops resurrect deduped losers), and `metats` rows are
    per-field meta LWW clocks (deleting them would let older remote meta
    values overwrite newer local ones after the epoch reset)."""
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            "DELETE FROM sync_shadow WHERE kind NOT IN ('alias', 'metats')")
        conn.commit()
    finally:
        conn.close()


def expire_tombstones(config: TiroConfig, now: datetime | None = None) -> int:
    """Purge sync_shadow tombstones older than TOMBSTONE_TTL_DAYS (FROZEN
    90). Local half of spec §4's GC; the all-device-ack half is S5's.
    Alias rows are exempt (decision #18)."""
    cutoff = _now_iso((now or datetime.now(UTC)) - timedelta(days=TOMBSTONE_TTL_DAYS))
    conn = get_connection(config.db_path)
    try:
        cur = conn.execute(
            "DELETE FROM sync_shadow WHERE deleted_at IS NOT NULL "
            "AND deleted_at < ? AND kind != 'alias'",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
