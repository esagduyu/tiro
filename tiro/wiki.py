"""Library Wiki page store (Phase 1b, wave W1): files-as-truth.

`{library}/wiki/**/*.md` markdown files (frontmatter contract below) are the
source of truth. The `wiki_pages` + `wiki_page_articles` tables (migration
008) are a *derived* index — a queryable cache reconciled files-win by
`reconcile_wiki_index()` (doctor's job) and kept current on every
`write_page()` call. Nothing in this module ever treats the derived tables
as authoritative for content; only for fast listing/joins.

Frontmatter contract (design doc §1):
    uid, kind ("entity"|"concept"), title, entity_type (entities only),
    status ("fresh"|"stale"|"conflicted"), article_uids (list, by uid not
    int id), source_count, generated_by, updated_at, user_pinned_note.

`read_page()` tolerates hand-edited files: any missing field falls back to
a sane default rather than raising.

This module owns generic wiki bookkeeping (page I/O, index.md, log.md,
_schema.md, stale-marking, reconciliation). Prompt composition and the LLM
call live in `tiro/wiki_gen.py` (later task) — this module never calls an
LLM and never reads another wiki page's content.
"""

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import frontmatter

from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.migrations import canonical_key, new_ulid

logger = logging.getLogger(__name__)

WIKI_KINDS = ("entity", "concept")

_RESERVED_FILENAMES = {"_schema.md", "index.md", "log.md"}

_KIND_HEADINGS = {"entity": "Entities", "concept": "Concepts"}


def wiki_slugify(name: str) -> str:
    """Lowercase, non-alphanumeric runs -> single hyphen, trimmed.

    "Context Engineering" -> "context-engineering"
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower())
    return slug.strip("-")


def page_path(config: TiroConfig, slug: str) -> Path:
    """Resolve a wiki slug (e.g. "entities/anthropic") to its markdown file
    path ({wiki}/entities/anthropic.md). Rejects path traversal / absolute
    slugs -- this ends up on a `{slug:path}` API route, so a malicious slug
    must never escape the wiki directory."""
    if not slug or Path(slug).is_absolute():
        raise ValueError(f"invalid wiki slug: {slug!r}")
    parts = Path(slug).parts
    if ".." in parts or any(p in ("", ".") for p in parts):
        raise ValueError(f"invalid wiki slug: {slug!r}")
    return config.wiki_dir / f"{slug}.md"


def read_page(config: TiroConfig, slug: str) -> dict | None:
    """Read a wiki page by slug. Returns None if the file doesn't exist.
    Tolerates hand-edited files missing frontmatter fields (defaults)."""
    path = page_path(config, slug)
    if not path.exists():
        return None
    post = frontmatter.load(str(path))
    meta = post.metadata
    article_uids = list(meta.get("article_uids") or [])
    return {
        "slug": slug,
        "uid": meta.get("uid") or "",
        "kind": meta.get("kind") or "",
        "title": meta.get("title") or "",
        "entity_type": meta.get("entity_type"),
        "status": meta.get("status") or "fresh",
        "article_uids": article_uids,
        "source_count": meta.get("source_count", len(article_uids)),
        "generated_by": meta.get("generated_by"),
        "updated_at": meta.get("updated_at"),
        "user_pinned_note": meta.get("user_pinned_note") or "",
        "body": post.content,
    }


def _upsert_wiki_page_row(
    conn,
    *,
    uid: str,
    slug: str,
    kind: str,
    title: str,
    entity_type: str | None,
    status: str,
    source_count: int,
    updated_at: str | None,
    article_ids: list[int],
) -> int:
    """Insert-or-update the derived `wiki_pages` row for `slug` and replace
    its `wiki_page_articles` links wholesale. Caller commits."""
    existing = conn.execute("SELECT id FROM wiki_pages WHERE slug = ?", (slug,)).fetchone()
    if existing:
        page_id = existing["id"]
        conn.execute(
            """UPDATE wiki_pages
               SET uid = ?, kind = ?, title = ?, entity_type = ?, status = ?,
                   source_count = ?, updated_at = ?
               WHERE id = ?""",
            (uid, kind, title, entity_type, status, source_count, updated_at, page_id),
        )
    else:
        cursor = conn.execute(
            """INSERT INTO wiki_pages
               (uid, slug, kind, title, entity_type, status, source_count, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, slug, kind, title, entity_type, status, source_count, updated_at),
        )
        page_id = cursor.lastrowid

    conn.execute("DELETE FROM wiki_page_articles WHERE page_id = ?", (page_id,))
    for article_id in article_ids:
        conn.execute(
            "INSERT OR IGNORE INTO wiki_page_articles (page_id, article_id) VALUES (?, ?)",
            (page_id, article_id),
        )
    return page_id


def write_page(
    config: TiroConfig,
    *,
    slug: str,
    kind: str,
    title: str,
    entity_type: str | None,
    article_uids: list[str],
    body: str,
    generated_by,
    user_pinned_note: str = "",
    status: str = "fresh",
    uid: str | None = None,
) -> dict:
    """Write a wiki page: file first, then the derived index row.

    Order matters for crash-safety: the page FILE is written before the
    SQLite upsert. If the process dies in between, the result is a file
    with no derived row -- exactly the state reconcile_wiki_index() (files
    win) is designed to heal on its next run, since it rebuilds the derived
    tables purely from what's on disk. The reverse order would risk a
    derived row pointing at a file that was never written, which is a much
    stranger state to recover from (there is no content to show) and isn't
    what reconcile is built to detect (it only scans files, so a
    file-less row would linger as a ghost until backup/rebuild time).

    `uid`: pass the prior page's uid to preserve identity across a
    regenerate; omit (None) to mint a new one for a brand-new page.
    `article_uids` entries that don't resolve to a known article are
    skipped (logged) -- bundle-imported pages may cite articles the current
    library doesn't have.
    """
    if kind not in WIKI_KINDS:
        raise ValueError(f"invalid wiki kind: {kind!r} (expected one of {WIKI_KINDS})")

    path = page_path(config, slug)
    is_update = path.exists()
    resolved_uid = uid or new_ulid()
    updated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    article_uids = list(article_uids or [])
    source_count = len(article_uids)

    metadata = {
        "uid": resolved_uid,
        "kind": kind,
        "title": title,
        "entity_type": entity_type,
        "status": status,
        "article_uids": article_uids,
        "source_count": source_count,
        "generated_by": generated_by,
        "updated_at": updated_at,
        "user_pinned_note": user_pinned_note,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(body)
    post.metadata = metadata
    path.write_text(frontmatter.dumps(post))

    conn = get_connection(config.db_path)
    try:
        article_ids = []
        for a_uid in article_uids:
            row = conn.execute("SELECT id FROM articles WHERE uid = ?", (a_uid,)).fetchone()
            if row:
                article_ids.append(row["id"])
            else:
                logger.warning("write_page(%s): unknown article uid %r skipped", slug, a_uid)
        _upsert_wiki_page_row(
            conn,
            uid=resolved_uid,
            slug=slug,
            kind=kind,
            title=title,
            entity_type=entity_type,
            status=status,
            source_count=source_count,
            updated_at=updated_at,
            article_ids=article_ids,
        )
        conn.commit()
    finally:
        conn.close()

    ensure_schema_file(config)
    regenerate_index(config)
    append_log(config, "update" if is_update else "create", slug)

    return {
        "slug": slug,
        "uid": resolved_uid,
        "kind": kind,
        "title": title,
        "entity_type": entity_type,
        "status": status,
        "article_uids": article_uids,
        "source_count": source_count,
        "generated_by": generated_by,
        "updated_at": updated_at,
        "user_pinned_note": user_pinned_note,
        "body": body,
    }


def _library_root_from_conn(conn) -> Path | None:
    """Best-effort recovery of the library root from an open connection's
    own backing file (PRAGMA database_list), so free-SQL call sites that
    only receive `conn` (no config) can still locate {library}/wiki/ for the
    file-side half of an update. Returns None for connections with no
    backing file (should not happen in practice -- get_connection always
    opens a real tiro.db path), in which case callers skip the file write
    and rely on the DB row alone (index/reconcile stays consistent)."""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except Exception:
        return None
    main = next((r for r in rows if r["name"] == "main"), None)
    if not main or not main["file"]:
        return None
    return Path(main["file"]).parent


def _mark_file_stale(path: Path) -> None:
    """Frontmatter-only rewrite: load, flip status, dump back unchanged body
    (post.content is never touched, so the body is preserved byte-exact)."""
    try:
        post = frontmatter.load(str(path))
        if post.metadata.get("status") == "stale":
            return
        post.metadata["status"] = "stale"
        path.write_text(frontmatter.dumps(post))
    except Exception as e:  # pragma: no cover - defensive, never crash ingest
        logger.warning("Failed to mark wiki page file stale (%s): %s", path, e)


def mark_pages_stale(conn, article_id: int) -> int:
    """Mark stale every wiki page whose underlying entity/tag node just
    gained a link to `article_id` (via the article_entities/article_tags
    junctions, matched to wiki_pages by kind + canonical title). Free SQL --
    no LLM, safe to call inside the ingest transaction. Updates the derived
    row AND (best-effort) the page file's frontmatter status, since files
    are truth and a later files-win reconcile would otherwise clobber the
    DB flag back to whatever the untouched file says. Returns the number of
    pages matched (whether or not they were already stale)."""
    entity_keys = {
        row["canonical_key"]
        for row in conn.execute(
            """SELECT e.canonical_key AS canonical_key
               FROM entities e
               JOIN article_entities ae ON ae.entity_id = e.id
               WHERE ae.article_id = ?""",
            (article_id,),
        ).fetchall()
        if row["canonical_key"]
    }
    tag_keys = {
        canonical_key(row["name"])
        for row in conn.execute(
            """SELECT t.name AS name
               FROM tags t
               JOIN article_tags at ON at.tag_id = t.id
               WHERE at.article_id = ?""",
            (article_id,),
        ).fetchall()
    }
    if not entity_keys and not tag_keys:
        return 0

    pages = conn.execute("SELECT id, slug, kind, title, status FROM wiki_pages").fetchall()
    matched = [
        p
        for p in pages
        if (p["kind"] == "entity" and canonical_key(p["title"]) in entity_keys)
        or (p["kind"] == "concept" and canonical_key(p["title"]) in tag_keys)
    ]
    if not matched:
        return 0

    library_root = _library_root_from_conn(conn)
    wiki_dir = library_root / "wiki" if library_root else None
    for p in matched:
        if p["status"] != "stale":
            conn.execute("UPDATE wiki_pages SET status = 'stale' WHERE id = ?", (p["id"],))
        if wiki_dir is not None:
            path = wiki_dir / f"{p['slug']}.md"
            if path.exists():
                _mark_file_stale(path)
    return len(matched)


def reconcile_wiki_index(config: TiroConfig) -> dict:
    """Rebuild wiki_pages/wiki_page_articles from wiki/**/*.md on disk
    (files win) -- excludes _schema.md/index.md/log.md. NEVER writes or
    deletes page files; only the derived SQLite tables are touched.
    Unparseable files are skipped (warned) and counted rather than raising,
    so one corrupt page can't block the whole reconcile.

    Returns {"pages": n rebuilt, "skipped": n unparseable files,
    "unresolved_articles": n cited article_uids that didn't resolve}."""
    files = []
    if config.wiki_dir.exists():
        files = sorted(
            p for p in config.wiki_dir.rglob("*.md") if p.name not in _RESERVED_FILENAMES
        )

    parsed = []
    skipped = 0
    for path in files:
        try:
            post = frontmatter.load(str(path))
        except Exception as e:
            logger.warning("Skipping unparseable wiki page %s: %s", path, e)
            skipped += 1
            continue
        rel = path.relative_to(config.wiki_dir)
        slug = rel.with_suffix("").as_posix()
        meta = post.metadata
        article_uids = list(meta.get("article_uids") or [])
        parsed.append(
            {
                "slug": slug,
                "uid": meta.get("uid") or new_ulid(),
                "kind": meta.get("kind") or "",
                "title": meta.get("title") or slug,
                "entity_type": meta.get("entity_type"),
                "status": meta.get("status") or "fresh",
                "article_uids": article_uids,
                "source_count": meta.get("source_count", len(article_uids)),
                "updated_at": meta.get("updated_at"),
            }
        )

    conn = get_connection(config.db_path)
    try:
        conn.execute("DELETE FROM wiki_page_articles")
        conn.execute("DELETE FROM wiki_pages")
        unresolved = 0
        for p in parsed:
            article_ids = []
            for a_uid in p["article_uids"]:
                row = conn.execute("SELECT id FROM articles WHERE uid = ?", (a_uid,)).fetchone()
                if row:
                    article_ids.append(row["id"])
                else:
                    unresolved += 1
                    logger.warning(
                        "reconcile: wiki page %s cites unknown article uid %r", p["slug"], a_uid
                    )
            _upsert_wiki_page_row(
                conn,
                uid=p["uid"],
                slug=p["slug"],
                kind=p["kind"],
                title=p["title"],
                entity_type=p["entity_type"],
                status=p["status"],
                source_count=p["source_count"],
                updated_at=p["updated_at"],
                article_ids=article_ids,
            )
        conn.commit()
    finally:
        conn.close()

    return {"pages": len(parsed), "skipped": skipped, "unresolved_articles": unresolved}


def ensure_schema_file(config: TiroConfig) -> Path:
    """Ensure {wiki}/_schema.md exists, copying the packaged default
    template on first use. Never overwrites an existing file -- once
    created it's user-owned maintenance instructions. The packaged default
    (tiro/intelligence/templates/wiki_schema_default.md) doesn't ship until
    a later task; until then a minimal placeholder is written so this
    module is testable standalone."""
    path = config.wiki_dir / "_schema.md"
    if path.exists():
        return path
    config.wiki_dir.mkdir(parents=True, exist_ok=True)
    packaged = Path(__file__).parent / "intelligence" / "templates" / "wiki_schema_default.md"
    if packaged.exists():
        path.write_text(packaged.read_text())
    else:
        path.write_text(
            "# Wiki Schema\n\n"
            "Default maintenance instructions placeholder.\n"
            "(Replaced by the packaged default template once it ships.)\n"
        )
    return path


def append_log(config: TiroConfig, op: str, slug: str) -> None:
    """Append one greppable, append-only line to {wiki}/log.md:
    `## [YYYY-MM-DD HH:MM] {op} | {slug}` (UTC)."""
    config.wiki_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    line = f"## [{timestamp}] {op} | {slug}\n"
    with (config.wiki_dir / "log.md").open("a") as f:
        f.write(line)


def regenerate_index(config: TiroConfig) -> None:
    """Regenerate {wiki}/index.md from the derived wiki_pages table
    (kind-grouped, one line per page: title, source_count, updated)."""
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute(
            "SELECT slug, kind, title, source_count, status, updated_at "
            "FROM wiki_pages ORDER BY kind, title COLLATE NOCASE"
        ).fetchall()
    finally:
        conn.close()

    by_kind: dict[str, list] = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row)

    lines = [
        "# Wiki Index",
        "",
        "_Regenerated automatically from the derived index -- do not hand-edit "
        "(edit the page files instead)._",
        "",
    ]

    ordered_kinds = list(WIKI_KINDS) + [k for k in by_kind if k not in WIKI_KINDS]
    for kind in ordered_kinds:
        pages = by_kind.get(kind, [])
        lines.append(f"## {_KIND_HEADINGS.get(kind, kind.title() if kind else 'Other')}")
        lines.append("")
        if not pages:
            lines.append("_None yet._")
        else:
            for row in pages:
                stale = " (stale)" if row["status"] == "stale" else ""
                lines.append(
                    f"- [{row['title']}]({row['slug']}.md) -- "
                    f"{row['source_count']} sources, updated {row['updated_at']}{stale}"
                )
        lines.append("")

    config.wiki_dir.mkdir(parents=True, exist_ok=True)
    (config.wiki_dir / "index.md").write_text("\n".join(lines).rstrip("\n") + "\n")
