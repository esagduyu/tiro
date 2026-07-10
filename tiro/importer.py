"""Import a Tiro export bundle (see EXPORT_SCHEMA.md).

Reverses export_library per-article with conflict resolution. NOT restore:
restore replaces the whole library from a snapshot; import merges bundle
articles into an existing library. digests/reading_stats/audio/relations
are not imported (regenerable caches, this-library activity, or fileless).
No stats increments, no AI calls; imported articles get
vector_status='pending' and the retry loop embeds them.

Highlights + notes sidecars (Phase 2 M2.1 Task 4) ARE imported, per-article,
keyed by each highlight's `uid` -- unlike the rest of this module, which is
SQL-only, sidecar merging writes FILES directly (files-win, same convention
as `tiro/api/routes_annotations.py`'s CRUD writes and `tiro/wiki.py`'s
reconcile). `conflicts` governs the merge the same way it governs the
article row: "skip" keeps every existing local sidecar line untouched and
only appends bundle lines whose uid isn't already present locally;
"overwrite" replaces both of the local article's sidecar files wholesale
with the bundle's; "keep-both" copies the bundle's lines under the new
duplicate article's own fresh stem, minting fresh uids so they can never
collide with the uids the ORIGINAL local article's own highlights already
use. A bundle article with neither sidecar file (and no `highlights`/`notes`
metadata rows for it either) is a no-op. After every article in the run has
been processed, `reconcile_annotations()` runs ONCE to rebuild the derived
`highlights`/`notes` SQLite rows from whatever ended up on disk.

Bundles CARRY a `wiki/` directory (Phase 1b) when the source library has
synthesis pages, but this module does NOT import it -- `wiki/` page files
are silently ignored the same way digests/reading_stats are. Wiki page
import/merge is out of scope for W1; snapshots/restore (whole-library,
not merge) round-trip `wiki/` faithfully since they copy the directory
wholesale rather than reversing it row-by-row. This module DOES,
however, stale-mark any EXISTING local wiki page whose node an imported
article newly links to (see the `mark_pages_stale` call below) -- import
is an ingest path like web/email ingestion, so a page's trust status must
degrade when new source material shows up under it, bundle-imported or not.

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

from tiro.annotations import (
    annotations_dir,
    delete_note,
    read_annotations,
    read_note,
    reconcile_annotations,
    sidecar_stem,
    write_annotations,
    write_note,
)
from tiro.authors import link_article_author
from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.migrations import canonical_key, new_ulid
from tiro.tags import ensure_tag
from tiro.wiki import mark_pages_stale

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

            # Highlights/notes metadata fallback (used only when the bundle
            # article has no annotations/notes sidecar FILE -- see
            # _bundle_sidecar_lines): bundle article_id -> its highlight
            # rows, bundle highlight id -> its anchored note body, bundle
            # article_id -> its one article-level note body.
            highlights_by_article: dict[int, list[dict]] = {}
            for h in meta.get("highlights", []):
                highlights_by_article.setdefault(h["article_id"], []).append(h)
            notes_by_highlight_id: dict[int, str] = {}
            article_notes_by_article: dict[int, str] = {}
            for n in meta.get("notes", []):
                if n.get("highlight_id") is not None:
                    notes_by_highlight_id[n["highlight_id"]] = n.get("body_markdown")
                else:
                    article_notes_by_article[n["article_id"]] = n.get("body_markdown")

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
                        # Article row itself is left untouched, but sidecars
                        # still merge additively (brief: "skip = keep
                        # existing lines, add new-uid lines") -- a skipped
                        # article isn't the same thing as a skipped
                        # highlight/note.
                        _merge_bundle_sidecars(
                            conn, config, zf, zip_names, art,
                            highlights_by_article, notes_by_highlight_id,
                            article_notes_by_article,
                            local_article_id=existing["id"],
                            local_uid=existing["uid"],
                            local_stem=sidecar_stem(existing),
                            mode="skip",
                        )
                        continue
                    elif conflicts == "overwrite":
                        _overwrite_article(conn, config, existing, art, body_md)
                        counts["overwritten"] += 1
                        local_article_id = existing["id"]
                        local_uid = existing["uid"]
                        local_stem = sidecar_stem(existing)  # overwrite keeps slug/markdown_path
                        sidecar_mode = "overwrite"
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
                        link_article_author(conn, local_article_id, art.get("author"))
                        counts["kept_both"] += 1
                        sidecar_mode = "keep_both"
                else:
                    src = _bundle_source_for(sources_by_id, art, source_name)
                    source_id = _ensure_source(conn, src)
                    local_article_id = _insert_article(
                        conn, config, art, source_id, body_md, keep_both=False
                    )
                    link_article_author(conn, local_article_id, art.get("author"))
                    counts["imported"] += 1
                    sidecar_mode = "fresh"

                if sidecar_mode in ("fresh", "keep_both"):
                    local_row = conn.execute(
                        "SELECT uid, markdown_path FROM articles WHERE id = ?",
                        (local_article_id,),
                    ).fetchone()
                    local_uid = local_row["uid"]
                    local_stem = sidecar_stem(local_row)

                _merge_bundle_sidecars(
                    conn, config, zf, zip_names, art,
                    highlights_by_article, notes_by_highlight_id, article_notes_by_article,
                    local_article_id=local_article_id, local_uid=local_uid,
                    local_stem=local_stem, mode=sidecar_mode,
                )

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

                # Mark stale any wiki pages for entities/tags this article
                # just linked to (Phase 1b) -- import is an ingest path like
                # web/email ingestion, same non-fatal pattern as
                # processor.py's hook: free SQL + frontmatter rewrite, no
                # LLM, but best-effort bookkeeping that must never fail the
                # import itself.
                try:
                    mark_pages_stale(config, conn, local_article_id)
                except Exception as e:
                    logger.error(
                        "mark_pages_stale failed for imported article %d (non-fatal): %s",
                        local_article_id, e,
                    )

        sources_after = {r["id"] for r in conn.execute("SELECT id FROM sources").fetchall()}
        counts["sources_created"] = len(sources_after - sources_before)

        conn.commit()
    finally:
        conn.close()

    # Sidecar files were written directly per-article above (files-win, see
    # module docstring); now that every article row this run needs exists
    # and is committed, rebuild the derived highlights/notes SQLite rows in
    # one pass from whatever ended up on disk.
    reconcile_annotations(config)

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
    # Thin wrapper over the shared helper (tiro/tags.py) — single home for the
    # ensure-tag pattern that processor/rss/importer all needed (M4.2).
    return ensure_tag(conn, name)


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
    (under its own existing slug/markdown_path), keep id/uid/slug.

    Only fields actually present in the bundle's article dict are
    overwritten (`if field in art`) — a bundle produced by an older schema
    that predates one of `_OVERWRITE_FIELDS` must not null that column out
    just because the key is absent, e.g. a pre-`is_read`-export shouldn't be
    able to un-read every existing article on overwrite-import."""
    updates = {field: art[field] for field in _OVERWRITE_FIELDS if field in art}
    if updates:
        set_clause = ", ".join(f"{field} = ?" for field in updates)
        conn.execute(
            f"UPDATE articles SET {set_clause}, vector_status = 'pending' WHERE id = ?",
            (*updates.values(), existing["id"]),
        )
    else:
        conn.execute(
            "UPDATE articles SET vector_status = 'pending' WHERE id = ?", (existing["id"],)
        )
    # article_authors junction follows the same absent-field guard as
    # _OVERWRITE_FIELDS above: only touch it when the bundle actually
    # carries an 'author' key. When it does, the bundle's value wins
    # outright — drop the existing link(s) and relink to the bundle's
    # author (article_authors has no UNIQUE(article_id), so old links must
    # be cleared explicitly rather than relying on INSERT OR IGNORE).
    if "author" in art:
        conn.execute("DELETE FROM article_authors WHERE article_id = ?", (existing["id"],))
        link_article_author(conn, existing["id"], art["author"])
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


# --- Highlights + notes sidecar merge (Phase 2 M2.1 Task 4) ------------------


def _bundle_sidecar_lines(
    zf: zipfile.ZipFile,
    zip_names: set,
    bundle_stem: str,
    bundle_article_id: int,
    highlights_by_article: dict[int, list[dict]],
    notes_by_highlight_id: dict[int, str],
    article_notes_by_article: dict[int, str],
) -> tuple[list[dict], str | None]:
    """Return `(lines, article_note_body)` for one bundle article, reading
    the bundle's own `annotations/<stem>.jsonl` / `notes/<stem>.md` sidecars
    when present, falling back to the `highlights`/`notes` metadata.json
    arrays otherwise (older bundles, or a hand-edited bundle missing the
    sidecar directories, have neither -- that's fine, both come back empty).
    Malformed JSONL lines are skipped, never raised, mirroring
    `tiro.annotations._parse_jsonl_lines`."""
    ann_name = f"annotations/{bundle_stem}.jsonl"
    lines: list[dict] = []
    if ann_name in zip_names:
        for raw in zf.read(ann_name).decode("utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("uid"):
                lines.append(obj)
    else:
        for h in highlights_by_article.get(bundle_article_id, []):
            lines.append({
                "uid": h.get("uid"),
                "article_uid": h.get("article_uid"),
                "quote": h.get("quote_text"),
                "prefix": h.get("prefix_context"),
                "suffix": h.get("suffix_context"),
                "position_start": h.get("text_position_start"),
                "position_end": h.get("text_position_end"),
                "content_hash": h.get("content_hash"),
                "color": h.get("color"),
                "note_markdown": notes_by_highlight_id.get(h.get("id")),
                "created_at": h.get("created_at"),
                "updated_at": h.get("updated_at"),
            })

    note_name = f"notes/{bundle_stem}.md"
    if note_name in zip_names:
        article_note = zf.read(note_name).decode("utf-8")
    else:
        article_note = article_notes_by_article.get(bundle_article_id)

    return lines, article_note


def _dedupe_uid(conn: sqlite3.Connection, uid: str | None, *, exclude_article_id: int) -> str:
    """If `uid` already belongs to a highlight on a DIFFERENT local article,
    mint a fresh one -- keeps `highlights.uid`'s global UNIQUE constraint
    safe when copying lines in from a bundle. A uid that belongs to the
    article being merged INTO is the same identity, not a real collision."""
    if not uid:
        return new_ulid()
    row = conn.execute("SELECT article_id FROM highlights WHERE uid = ?", (uid,)).fetchone()
    if row is None or row["article_id"] == exclude_article_id:
        return uid
    return new_ulid()


def _write_fresh_sidecars(
    config: TiroConfig,
    conn: sqlite3.Connection,
    *,
    local_stem: str,
    local_uid: str,
    lines: list[dict],
    note_body: str | None,
    mint_fresh_uids: bool,
    exclude_article_id: int,
) -> None:
    """No pre-existing local sidecar to merge against -- used for a plain
    new import (`mint_fresh_uids=False`, dedupe only guards against a freak
    cross-library uid collision) and for a keep-both copy
    (`mint_fresh_uids=True`, since the ORIGINAL conflicting local article
    may already use these exact uids under its own stem)."""
    if lines:
        out_lines = []
        for line in lines:
            uid = (
                new_ulid()
                if mint_fresh_uids
                else _dedupe_uid(conn, line.get("uid"), exclude_article_id=exclude_article_id)
            )
            out_lines.append({**line, "uid": uid, "article_uid": local_uid})
        write_annotations(config, local_stem, out_lines)
    if note_body:
        write_note(config, local_stem, note_body)


def _merge_skip_sidecars(
    config: TiroConfig,
    conn: sqlite3.Connection,
    *,
    local_stem: str,
    local_uid: str,
    lines: list[dict],
    note_body: str | None,
    exclude_article_id: int,
) -> None:
    """conflicts="skip": the article row is untouched, and existing local
    sidecar lines/note win outright -- only bundle lines whose uid isn't
    already present locally get appended; an existing local note is never
    replaced by the bundle's."""
    existing_lines = read_annotations(config, local_stem)
    existing_uids = {ln.get("uid") for ln in existing_lines}
    changed = False
    for line in lines:
        if line.get("uid") in existing_uids:
            continue
        uid = _dedupe_uid(conn, line.get("uid"), exclude_article_id=exclude_article_id)
        existing_lines.append({**line, "uid": uid, "article_uid": local_uid})
        changed = True
    if changed:
        write_annotations(config, local_stem, existing_lines)

    if note_body and read_note(config, local_stem) is None:
        write_note(config, local_stem, note_body)


def _merge_overwrite_sidecars(
    config: TiroConfig,
    conn: sqlite3.Connection,
    *,
    local_stem: str,
    local_uid: str,
    lines: list[dict],
    note_body: str | None,
    exclude_article_id: int,
) -> None:
    """conflicts="overwrite": the bundle's state wins outright, same
    posture as the tags/entities junction rebuild above -- both sidecar
    files are replaced wholesale (an absent bundle sidecar means the local
    one is removed, not left stale)."""
    if lines:
        out_lines = []
        for line in lines:
            uid = _dedupe_uid(conn, line.get("uid"), exclude_article_id=exclude_article_id)
            out_lines.append({**line, "uid": uid, "article_uid": local_uid})
        write_annotations(config, local_stem, out_lines)
    else:
        (annotations_dir(config) / f"{local_stem}.jsonl").unlink(missing_ok=True)

    if note_body:
        write_note(config, local_stem, note_body)
    else:
        delete_note(config, local_stem)


def _merge_bundle_sidecars(
    conn: sqlite3.Connection,
    config: TiroConfig,
    zf: zipfile.ZipFile,
    zip_names: set,
    art: dict,
    highlights_by_article: dict[int, list[dict]],
    notes_by_highlight_id: dict[int, str],
    article_notes_by_article: dict[int, str],
    *,
    local_article_id: int,
    local_uid: str,
    local_stem: str,
    mode: str,
) -> None:
    """Dispatch to the merge strategy matching `mode` ("fresh" -- a plain
    new import, "keep_both", "skip", or "overwrite" -- mirroring the
    `conflicts` modes 1:1 except "fresh", which has no article-row conflict
    to resolve in the first place). No-ops entirely when the bundle article
    has neither a sidecar file nor metadata fallback rows."""
    bundle_stem = Path(art["markdown_path"]).stem
    lines, note_body = _bundle_sidecar_lines(
        zf, zip_names, bundle_stem, art["id"],
        highlights_by_article, notes_by_highlight_id, article_notes_by_article,
    )
    if not lines and note_body is None:
        return

    if mode == "fresh":
        _write_fresh_sidecars(
            config, conn, local_stem=local_stem, local_uid=local_uid,
            lines=lines, note_body=note_body, mint_fresh_uids=False,
            exclude_article_id=local_article_id,
        )
    elif mode == "keep_both":
        _write_fresh_sidecars(
            config, conn, local_stem=local_stem, local_uid=local_uid,
            lines=lines, note_body=note_body, mint_fresh_uids=True,
            exclude_article_id=local_article_id,
        )
    elif mode == "skip":
        _merge_skip_sidecars(
            config, conn, local_stem=local_stem, local_uid=local_uid,
            lines=lines, note_body=note_body, exclude_article_id=local_article_id,
        )
    elif mode == "overwrite":
        _merge_overwrite_sidecars(
            config, conn, local_stem=local_stem, local_uid=local_uid,
            lines=lines, note_body=note_body, exclude_article_id=local_article_id,
        )
    else:
        raise ValueError(f"unknown sidecar merge mode {mode!r}")
