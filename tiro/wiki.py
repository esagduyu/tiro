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

W1 page identity: one page per slugified node name per kind. Two entities
sharing a name across entity_types (e.g. "Washington" person vs place) map
to the SAME entities/ page in W1 — the page covers both; stale marking
treats them as one. Revisit if lint (W3) surfaces real collisions.
"""

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import frontmatter

from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.intelligence.prompts import load_template
from tiro.migrations import new_ulid

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
    # A slug ending in "/" (e.g. "entities/" -- what a non-Latin name that
    # `wiki_slugify()` collapses to "" produces upstream, W3) has
    # Path(...).parts silently drop the trailing empty segment, passing the
    # check above while still resolving to a bare ".md" file
    # (wiki_dir/entities/.md). Guard the raw string's final segment
    # explicitly -- defense in depth alongside wiki_gen._generate's earlier,
    # more specific check.
    final_segment = slug.rsplit("/", 1)[-1]
    if not final_segment or final_segment == ".md":
        raise ValueError(f"invalid wiki slug (empty page name): {slug!r}")
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

    # frontmatter.dumps() strips leading/trailing whitespace from the body
    # when it writes the file, so a later read_page() would see a stripped
    # body regardless. Strip once, up front, so the dict this function
    # returns already matches what read_page() will return -- callers never
    # see a body that differs depending on whether they got it from the
    # write result or a subsequent read.
    body = body.strip()

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


def mark_page_ids_stale(config: TiroConfig, conn, page_ids: list[int]) -> int:
    """Mark stale every wiki page in `page_ids` -- both the derived
    `wiki_pages` row and, best-effort, the page file's frontmatter status
    (files are truth, so a later files-win reconcile would otherwise
    clobber the DB flag back to whatever the untouched file says).

    Shared plumbing: `mark_pages_stale()` resolves slugs from an article's
    entity/tag junctions and delegates here; `delete_article()`
    (tiro/lifecycle.py) resolves page ids from `wiki_page_articles` for the
    article being deleted and delegates here too, so a deleted article's
    citations always flip their page(s) stale rather than just vanishing
    from the junction. Returns the number of ids matched (an id that no
    longer exists is silently skipped, not an error)."""
    page_ids = list(dict.fromkeys(page_ids))  # de-dupe, preserve order
    if not page_ids:
        return 0

    placeholders = ",".join("?" for _ in page_ids)
    params = tuple(page_ids)
    matched = conn.execute(
        f"SELECT id, slug, status FROM wiki_pages WHERE id IN ({placeholders})",
        params,
    ).fetchall()
    if not matched:
        return 0

    conn.execute(
        f"UPDATE wiki_pages SET status = 'stale' "
        f"WHERE id IN ({placeholders}) AND status != 'stale'",
        params,
    )
    for p in matched:
        path = page_path(config, p["slug"])
        if path.exists():
            _mark_file_stale(path)
    return len(matched)


def mark_pages_stale(config: TiroConfig, conn, article_id: int) -> int:
    """Mark stale every wiki page whose slug matches an entity/tag node that
    just gained a link to `article_id` (via the article_entities/article_tags
    junctions). Matching is SLUG-based, not title-based: for each linked
    entity the expected slug is `entities/{wiki_slugify(entity.name)}`, and
    for each linked tag it's `concepts/{wiki_slugify(tag.name)}`. This is
    exact (immune to cosmetic title prettification, e.g. a page titled
    "Context Engineering" still has slug `concepts/context-engineering`) and
    type-safe by construction -- the `entities/`/`concepts/` prefix is what
    used to be a separate kind check. Free SQL -- no LLM, safe to call
    inside the ingest transaction. Returns the number of pages matched
    (whether or not they were already stale)."""
    entity_slugs = {
        f"entities/{wiki_slugify(row['name'])}"
        for row in conn.execute(
            """SELECT e.name AS name
               FROM entities e
               JOIN article_entities ae ON ae.entity_id = e.id
               WHERE ae.article_id = ?""",
            (article_id,),
        ).fetchall()
    }
    tag_slugs = {
        f"concepts/{wiki_slugify(row['name'])}"
        for row in conn.execute(
            """SELECT t.name AS name
               FROM tags t
               JOIN article_tags at ON at.tag_id = t.id
               WHERE at.article_id = ?""",
            (article_id,),
        ).fetchall()
    }
    expected_slugs = entity_slugs | tag_slugs
    if not expected_slugs:
        return 0

    placeholders = ",".join("?" for _ in expected_slugs)
    params = tuple(expected_slugs)
    matched_ids = [
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM wiki_pages WHERE slug IN ({placeholders})", params
        ).fetchall()
    ]
    return mark_page_ids_stale(config, conn, matched_ids)


def reconcile_wiki_index(config: TiroConfig) -> dict:
    """Rebuild wiki_pages/wiki_page_articles from wiki/**/*.md on disk
    (files win) -- excludes _schema.md/index.md/log.md AND sync conflict
    files (`{stem}.conflict-{device}-{yyyymmdd}.md` — spec §4: conflict
    files sync as ordinary files but are EXCLUDED from ingest/index; without
    this a wiki conflict file would be indexed as a page whose frontmatter
    uid collides with the real page's). NEVER writes or deletes page files;
    only the derived SQLite tables are touched. Unparseable files are
    skipped (warned) and counted rather than raising, so one corrupt page
    can't block the whole reconcile.

    Returns {"pages": n rebuilt, "skipped": n unparseable files,
    "unresolved_articles": n cited article_uids that didn't resolve,
    "duplicate_uids": n files whose frontmatter uid collided with an
    earlier-sorted file's this run}."""
    # Lazy import: tiro.sync.reconcile lazily imports tiro.wiki inside its
    # own functions; a module-level import here would risk a cycle.
    from tiro.sync.reconcile import is_conflict_file

    files = []
    if config.wiki_dir.exists():
        files = sorted(
            p for p in config.wiki_dir.rglob("*.md")
            if p.name not in _RESERVED_FILENAMES and not is_conflict_file(p.name)
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
        duplicate_uids = 0
        seen_uids: set[str] = set()
        for p in parsed:
            # Template-copying a page file (`cp entities/a.md entities/b.md`)
            # carries the source file's uid along, so two files can share a
            # uid -- idx_wiki_pages_uid is a UNIQUE index and would raise on
            # the second insert. `files` (and therefore `parsed`) is sorted,
            # so "later" is deterministic: the earlier-sorted file keeps its
            # uid, the later one's DERIVED ROW gets a fresh uid. The file
            # itself is never rewritten -- files-win means the row diverging
            # from its file's frontmatter uid is the accepted cost of a
            # collision, not something reconcile silently "fixes" on disk.
            uid = p["uid"]
            if uid in seen_uids:
                duplicate_uids += 1
                logger.warning(
                    "reconcile: wiki page %s has uid %r already used by an "
                    "earlier-sorted page this run; assigning a fresh row-only uid",
                    p["slug"], uid,
                )
                uid = new_ulid()
            else:
                seen_uids.add(uid)

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
                uid=uid,
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

    return {
        "pages": len(parsed),
        "skipped": skipped,
        "unresolved_articles": unresolved,
        "duplicate_uids": duplicate_uids,
    }


def ensure_schema_file(config: TiroConfig) -> Path:
    """Ensure {wiki}/_schema.md exists, copying the packaged default
    template on first use. Never overwrites an existing file -- once
    created it's user-owned maintenance instructions."""
    path = config.wiki_dir / "_schema.md"
    if path.exists():
        return path
    config.wiki_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(load_template("wiki_schema_default", ext="md"))
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
