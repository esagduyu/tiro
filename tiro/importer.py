"""Import a Tiro export bundle (see EXPORT_SCHEMA.md).

Reverses export_library per-article with conflict resolution. NOT restore:
restore replaces the whole library from a snapshot; import merges bundle
articles into an existing library. digests/reading_stats/audio/relations
are not imported (regenerable caches, this-library activity, or fileless).
No stats increments, no AI calls; imported articles get
vector_status='pending' and the retry loop embeds them.

Failure semantics: all SQLite writes for a run happen in one transaction
(commit only at the end) — a mid-run crash leaves the DB untouched. Markdown
writes are NOT part of that transaction: `_overwrite_article` rewrites the
existing file on disk immediately. A crash on a later article therefore
rolls back the DB while an earlier overwritten article's FILE keeps the
bundle's content — invisible to `tiro doctor` since both row and file still
exist, just inconsistent with each other. Narrow window, accepted for M1.1;
re-running the same import with the same conflict mode converges.
"""

import json
import logging
import sqlite3
import zipfile
from pathlib import Path

from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.migrations import canonical_key, new_ulid

logger = logging.getLogger(__name__)

CONFLICT_MODES = ("skip", "overwrite", "keep-both")
_OVERWRITE_FIELDS = (
    "title", "author", "summary", "rating", "ai_tier", "is_read",
    "published_at", "ingenuity_analysis",
)


def import_bundle(config: TiroConfig, zip_path: Path, *, conflicts: str = "skip") -> dict:
    """Import a bundle produced by `export_library` into `config`'s library.

    `conflicts` controls what happens when an incoming article matches an
    existing row (see module docstring / EXPORT_SCHEMA.md for match order):
    "skip" (default) leaves the existing row untouched, "overwrite" updates
    its content fields and rewrites its markdown, "keep-both" inserts the
    bundle article as a new row with a fresh uid and a disambiguated slug.
    """
    if conflicts not in CONFLICT_MODES:
        raise ValueError(f"conflicts must be one of {CONFLICT_MODES}, got {conflicts!r}")

    counts = {"imported": 0, "skipped": 0, "overwritten": 0, "kept_both": 0, "sources_created": 0}

    conn = get_connection(config.db_path)
    try:
        sources_before = {r["id"] for r in conn.execute("SELECT id FROM sources").fetchall()}

        with zipfile.ZipFile(zip_path) as zf:
            meta = json.loads(zf.read("metadata.json"))
            zip_names = set(zf.namelist())

            sources_by_id = {s["id"]: s for s in meta.get("sources", [])}
            tags_by_id = {t["id"]: t for t in meta.get("tags", [])}
            entities_by_id = {e["id"]: e for e in meta.get("entities", [])}

            # bundle article_id -> {tag_id: [...], entity_id: [...]}
            tags_for_article: dict[int, list[int]] = {}
            for row in meta.get("article_tags", []):
                tags_for_article.setdefault(row["article_id"], []).append(row["tag_id"])
            entities_for_article: dict[int, list[int]] = {}
            for row in meta.get("article_entities", []):
                entities_for_article.setdefault(row["article_id"], []).append(row["entity_id"])

            for art in meta.get("articles", []):
                bundle_article_id = art["id"]
                arcname = f"articles/{Path(art['markdown_path']).name}"
                if arcname not in zip_names:
                    logger.warning(
                        "Skipping article %r (uid=%s): markdown file %s missing from bundle",
                        art.get("title"), art.get("uid"), arcname,
                    )
                    counts["skipped"] += 1
                    continue
                body_md = zf.read(arcname).decode("utf-8")

                source_name = art.get("source_name")
                existing = _find_existing(conn, art, source_name)

                if existing is not None:
                    if conflicts == "skip":
                        counts["skipped"] += 1
                        continue
                    elif conflicts == "overwrite":
                        _overwrite_article(conn, config, existing, art, body_md)
                        counts["overwritten"] += 1
                        local_article_id = existing["id"]
                        # Overwrite means the bundle's state wins: clear
                        # existing junction links so locally-added tags/
                        # entities not present in the bundle don't survive.
                        conn.execute(
                            "DELETE FROM article_tags WHERE article_id = ?", (local_article_id,)
                        )
                        conn.execute(
                            "DELETE FROM article_entities WHERE article_id = ?", (local_article_id,)
                        )
                    else:  # keep-both
                        src = _bundle_source_for(sources_by_id, art, source_name)
                        source_id = _ensure_source(conn, src)
                        local_article_id = _insert_article(
                            conn, config, art, source_id, body_md, keep_both=True
                        )
                        counts["kept_both"] += 1
                else:
                    src = _bundle_source_for(sources_by_id, art, source_name)
                    source_id = _ensure_source(conn, src)
                    local_article_id = _insert_article(
                        conn, config, art, source_id, body_md, keep_both=False
                    )
                    counts["imported"] += 1

                # Rebuild junctions from the bundle for this article.
                for tag_id in tags_for_article.get(bundle_article_id, []):
                    tag = tags_by_id.get(tag_id)
                    if tag is None:
                        continue
                    local_tag_id = _ensure_tag(conn, tag["name"])
                    conn.execute(
                        "INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (?, ?)",
                        (local_article_id, local_tag_id),
                    )
                for entity_id in entities_for_article.get(bundle_article_id, []):
                    entity = entities_by_id.get(entity_id)
                    if entity is None:
                        continue
                    local_entity_id = _ensure_entity(conn, entity["name"], entity["entity_type"])
                    conn.execute(
                        "INSERT OR IGNORE INTO article_entities (article_id, entity_id) VALUES (?, ?)",
                        (local_article_id, local_entity_id),
                    )

        sources_after = {r["id"] for r in conn.execute("SELECT id FROM sources").fetchall()}
        counts["sources_created"] = len(sources_after - sources_before)

        conn.commit()
    finally:
        conn.close()

    return counts


def _bundle_source_for(sources_by_id: dict, art: dict, source_name: str | None) -> dict:
    """The bundle's source row for `art`, falling back to a minimal
    name/type-only dict if the referenced source_id is absent from the
    bundle's (unfiltered) sources array — shouldn't happen with a
    well-formed export, but keeps import robust against partial bundles."""
    src = sources_by_id.get(art.get("source_id"))
    if src is not None:
        return src
    return {"name": source_name or "Unknown", "source_type": art.get("source_type") or "web"}


def _find_existing(conn: sqlite3.Connection, art: dict, source_name: str | None) -> sqlite3.Row | None:
    """Match order: uid -> url (non-null) -> (title, source name)."""
    uid = art.get("uid")
    if uid:
        row = conn.execute("SELECT * FROM articles WHERE uid = ?", (uid,)).fetchone()
        if row is not None:
            return row

    url = art.get("url")
    if url:
        row = conn.execute("SELECT * FROM articles WHERE url = ?", (url,)).fetchone()
        if row is not None:
            return row

    title = art.get("title")
    if title and source_name:
        row = conn.execute(
            "SELECT a.* FROM articles a JOIN sources s ON a.source_id = s.id"
            " WHERE a.title = ? AND s.name = ?",
            (title, source_name),
        ).fetchone()
        if row is not None:
            return row

    return None


def _ensure_source(conn: sqlite3.Connection, src: dict) -> int:
    """Find a local source by (name, source_type) or create it from the
    bundle's source record."""
    name = src.get("name") or "Unknown"
    source_type = src.get("source_type") or "web"

    row = conn.execute(
        "SELECT id FROM sources WHERE name = ? AND source_type = ?", (name, source_type)
    ).fetchone()
    if row is not None:
        return row["id"]

    domain = src.get("domain")
    email_sender = src.get("email_sender")
    is_vip = src.get("is_vip", False)
    cur = conn.execute(
        "INSERT INTO sources (name, domain, email_sender, source_type, is_vip) VALUES (?, ?, ?, ?, ?)",
        (name, domain, email_sender, source_type, bool(is_vip)),
    )
    return cur.lastrowid


def _ensure_tag(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute("INSERT INTO tags (uid, name) VALUES (?, ?)", (new_ulid(), name))
    return cur.lastrowid


def _ensure_entity(conn: sqlite3.Connection, name: str, entity_type: str) -> int:
    key = canonical_key(name)
    row = conn.execute(
        "SELECT id FROM entities WHERE entity_type = ? AND canonical_key = ?", (entity_type, key)
    ).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO entities (uid, name, entity_type, canonical_key) VALUES (?, ?, ?, ?)",
        (new_ulid(), name, entity_type, key),
    )
    return cur.lastrowid


def _unique_slug(conn: sqlite3.Connection, base: str) -> str:
    """`base` unmodified if free; otherwise disambiguated with -2, -3, ...
    Callers pass an already `-imported`-suffixed base for keep-both."""
    slug = base
    n = 2
    while conn.execute("SELECT 1 FROM articles WHERE slug = ?", (slug,)).fetchone() is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


def _overwrite_article(conn: sqlite3.Connection, config: TiroConfig, existing: sqlite3.Row, art: dict, body_md: str) -> None:
    """Update the existing row's content fields, rewrite its markdown file
    (under its own existing slug/markdown_path), keep id/uid/slug."""
    updates = {field: art.get(field) for field in _OVERWRITE_FIELDS}
    set_clause = ", ".join(f"{field} = ?" for field in _OVERWRITE_FIELDS)
    conn.execute(
        f"UPDATE articles SET {set_clause}, vector_status = 'pending' WHERE id = ?",
        (*updates.values(), existing["id"]),
    )
    md_path = config.articles_dir / existing["markdown_path"]
    md_path.write_text(body_md)


def _insert_article(
    conn: sqlite3.Connection,
    config: TiroConfig,
    art: dict,
    source_id: int,
    body_md: str,
    *,
    keep_both: bool,
) -> int:
    """Insert `art` as a new row. `keep_both=True` always mints a fresh uid
    and an `-imported`-suffixed (uniquified) slug; otherwise the bundle's
    uid is reused when present and not already taken, and the bundle's own
    slug is uniquified in place."""
    base_slug = Path(art["markdown_path"]).stem or art.get("slug") or "article"

    if keep_both:
        uid = new_ulid()
        slug = _unique_slug(conn, f"{base_slug}-imported")
    else:
        bundle_uid = art.get("uid")
        if bundle_uid and conn.execute(
            "SELECT 1 FROM articles WHERE uid = ?", (bundle_uid,)
        ).fetchone() is None:
            uid = bundle_uid
        else:
            uid = new_ulid()
        slug = _unique_slug(conn, base_slug)

    markdown_path = f"{slug}.md"
    cur = conn.execute(
        """
        INSERT INTO articles (
            uid, source_id, title, author, url, slug, markdown_path, summary,
            word_count, reading_time_min, published_at, is_read, rating,
            ai_tier, ingenuity_analysis, ingestion_method, vector_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        (
            uid,
            source_id,
            art.get("title"),
            art.get("author"),
            art.get("url"),
            slug,
            markdown_path,
            art.get("summary"),
            art.get("word_count"),
            art.get("reading_time_min"),
            art.get("published_at"),
            bool(art.get("is_read", False)),
            art.get("rating"),
            art.get("ai_tier"),
            art.get("ingenuity_analysis"),
            art.get("ingestion_method"),
        ),
    )
    (config.articles_dir / markdown_path).write_text(body_md)
    return cur.lastrowid
