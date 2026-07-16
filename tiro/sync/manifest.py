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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import frontmatter

from tiro.anchors import content_hash
from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.sync.journal import TOMBSTONE_TTL_DAYS, canonical_json
from tiro.sync.reconcile import is_conflict_file

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
    try:
        return path.read_text()
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
            path = config.articles_dir / name
            body = _read_text(path)
            h = content_hash(body) if body is not None else None
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
            continue
        m.add(ManifestEntry(
            kind="pathfile", uid=f"path:{rel}", hash=content_hash(body),
            fields={"path_hint": rel},
        ))


def _add_highlights(config: TiroConfig, m: Manifest) -> None:
    from tiro.annotations import annotations_dir, read_annotations

    an_dir = annotations_dir(config)
    if not an_dir.exists():
        return
    for path in sorted(an_dir.glob("*.jsonl")):
        for line in read_annotations(config, path.stem):
            uid = line.get("uid")
            if not uid:
                continue
            m.add(ManifestEntry(
                kind="highlight", uid=uid,
                hash=content_hash(canonical_json(line)),
                fields={"article_uid": line.get("article_uid"), "line": line},
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
                     now: datetime | None = None) -> None:
    conn.execute(
        "INSERT INTO sync_shadow (kind, uid, hash, fields_json, hlc, deleted_at) "
        "VALUES (?, ?, NULL, '{}', ?, ?) "
        "ON CONFLICT(kind, uid) DO UPDATE SET hash = NULL, fields_json = '{}', "
        "hlc = excluded.hlc, deleted_at = excluded.deleted_at",
        (kind, uid, hlc, _now_iso(now)),
    )


def save_shadow(config: TiroConfig, manifest: Manifest, *,
                clock=None, now: datetime | None = None) -> None:
    """Persist `manifest` as the new shadow. Previous live entries absent
    from `manifest` become tombstones (deleted locally / by an applied op).
    The engine (S5) calls this after a successful push; S2 tests call it
    directly. Alias rows are preserved untouched."""
    from tiro.sync.journal import HLCClock

    clock = clock or HLCClock("local")
    hlc_str = clock.tick().to_str()
    prev = load_shadow(config)
    conn = get_connection(config.db_path)
    try:
        for key, entry in manifest.entries.items():
            old = prev.entries.get(key)
            # Keep the existing hlc when nothing changed (monotone shadow).
            keep = old.hlc if (old and old.hash == entry.hash
                               and old.fields == entry.fields) else hlc_str
            shadow_upsert(conn, entry.kind, entry.uid,
                          hash=entry.hash, fields=entry.fields, hlc=keep)
        for key in prev.entries:
            if key not in manifest.entries:
                shadow_tombstone(conn, key[0], key[1], hlc=hlc_str, now=now)
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
