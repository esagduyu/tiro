"""Library export — generates a zip bundle of the user's Tiro library."""

import json
import logging
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from tiro import __version__
from tiro.annotations import annotations_dir, notes_dir
from tiro.config import TiroConfig
from tiro.database import get_connection

logger = logging.getLogger(__name__)


def export_library(
    config: TiroConfig,
    *,
    tag: str | None = None,
    source_id: int | None = None,
    rating_min: int | None = None,
    date_from: str | None = None,
) -> Path:
    """Export the library as a zip file.

    Returns the path to a temporary zip file (caller is responsible for cleanup).
    """
    conn = get_connection(config.db_path)
    try:
        # Build filtered article query
        article_ids = _get_filtered_article_ids(
            conn, tag=tag, source_id=source_id, rating_min=rating_min, date_from=date_from
        )

        # Gather all data
        articles = _get_articles(conn, article_ids)
        sources = _get_sources(conn)
        tags = _get_tags(conn, article_ids)
        entities = _get_entities(conn, article_ids)
        relations = _get_relations(conn, article_ids)
        article_tags = _get_article_tags(conn, article_ids)
        article_entities = _get_article_entities(conn, article_ids)
        # digests and reading_stats are intentionally whole-library, not
        # filtered by article_ids: a digest can span articles outside the
        # current filter and daily stats aren't per-article at all, so
        # scoping them to the filtered set would silently drop or corrupt
        # data a THIRD-PARTY importer might rely on to reconstruct history
        # faithfully. Tiro's own importer (tiro/importer.py) deliberately
        # ignores both fields — they're regenerable caches / this-library
        # activity, not article content — so this scoping decision is about
        # keeping the bundle complete for other consumers, not about
        # round-tripping through `tiro import-bundle`.
        digests = _get_digests(conn)
        reading_stats = _get_reading_stats(conn)
        audio = _get_audio_metadata(conn, article_ids)
        highlights = _get_highlights(conn, article_ids)
        notes = _get_notes(conn, article_ids)
        # feeds are whole-library subscriptions (not article-filtered) — same
        # rationale as digests/reading_stats: a feed is a library-level object,
        # and a filtered export scoping them away would silently drop the
        # user's subscription list. Transient fetch state (etag/last_modified/
        # error_count/last_error/last_fetched_at) is excluded — regenerable on
        # the next poll — and `feed_entries` is excluded entirely (regenerable
        # dedup ledger, same bucket as reading_sessions). See spec D5.
        feeds = _get_feeds(conn)

    finally:
        conn.close()

    # Build the metadata payload
    metadata = {
        "exported_at": datetime.now().isoformat(),
        "tiro_version": __version__,
        "filters": {
            "tag": tag,
            "source_id": source_id,
            "rating_min": rating_min,
            "date_from": date_from,
        },
        "articles": articles,
        "sources": sources,
        "tags": tags,
        "entities": entities,
        "relations": relations,
        "article_tags": article_tags,
        "article_entities": article_entities,
        "digests": digests,
        "reading_stats": reading_stats,
        "audio": audio,
        "highlights": highlights,
        "notes": notes,
        "feeds": feeds,
    }

    # Create the zip
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip", prefix="tiro-export-")
    tmp.close()
    zip_path = Path(tmp.name)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add markdown files
        for article in articles:
            md_path = config.articles_dir / article["markdown_path"]
            if md_path.exists():
                arcname = f"articles/{md_path.name}"
                zf.write(md_path, arcname)

        # Wiki pages (Phase 1b): LLM-maintained synthesis pages are part of
        # the user's library — include them when present.
        wiki_dir = config.wiki_dir
        if wiki_dir.exists():
            for page in sorted(wiki_dir.rglob("*.md")):
                rel = page.relative_to(wiki_dir)
                zf.write(page, f"wiki/{rel.as_posix()}")

        # Highlights + notes sidecars (Phase 2 M2.1): files-as-truth, same
        # rglob-and-ride-along pattern as wiki/ above -- but scoped to ONLY
        # the exported articles' stems, since (unlike wiki/, which has no
        # per-article filter concept) a sidecar belongs to exactly one
        # article and a filtered export must not leak sidecars for articles
        # it excluded.
        exported_stems = {Path(a["markdown_path"]).stem for a in articles}
        ann_dir = annotations_dir(config)
        if ann_dir.exists():
            for f in sorted(ann_dir.glob("*.jsonl")):
                if f.stem in exported_stems:
                    zf.write(f, f"annotations/{f.name}")
        nt_dir = notes_dir(config)
        if nt_dir.exists():
            for f in sorted(nt_dir.glob("*.md")):
                if f.stem in exported_stems:
                    zf.write(f, f"notes/{f.name}")

        # Add metadata.json
        zf.writestr("metadata.json", json.dumps(metadata, indent=2, default=str))

        # Add sources.opml
        zf.writestr("sources.opml", export_opml(config))

        # Add README.md
        zf.writestr("README.md", _bundle_readme(len(articles)))

    logger.info("Exported %d articles to %s", len(articles), zip_path)
    return zip_path


def _get_filtered_article_ids(
    conn,
    *,
    tag: str | None = None,
    source_id: int | None = None,
    rating_min: int | None = None,
    date_from: str | None = None,
) -> list[int]:
    """Return article IDs matching the given filters."""
    query = "SELECT DISTINCT a.id FROM articles a"
    joins = []
    conditions = []
    params: list = []

    if tag:
        joins.append("JOIN article_tags at_ ON a.id = at_.article_id")
        joins.append("JOIN tags t ON at_.tag_id = t.id")
        conditions.append("LOWER(t.name) = LOWER(?)")
        params.append(tag)

    if source_id is not None:
        conditions.append("a.source_id = ?")
        params.append(source_id)

    if rating_min is not None:
        conditions.append("a.rating >= ?")
        params.append(rating_min)

    if date_from:
        conditions.append("a.ingested_at >= ?")
        params.append(date_from)

    if joins:
        query += " " + " ".join(joins)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    rows = conn.execute(query, params).fetchall()
    return [row["id"] for row in rows]


def _get_articles(conn, article_ids: list[int]) -> list[dict]:
    """Fetch full article records for the given IDs."""
    if not article_ids:
        return []

    placeholders = ",".join("?" * len(article_ids))
    rows = conn.execute(
        f"""
        SELECT a.*, s.name as source_name, s.source_type, s.is_vip as source_is_vip
        FROM articles a
        LEFT JOIN sources s ON a.source_id = s.id
        WHERE a.id IN ({placeholders})
        ORDER BY a.ingested_at DESC
        """,
        article_ids,
    ).fetchall()

    return [dict(row) for row in rows]


def _get_sources(conn) -> list[dict]:
    """Fetch all sources."""
    rows = conn.execute("SELECT * FROM sources ORDER BY name").fetchall()
    return [dict(row) for row in rows]


def _get_feeds(conn) -> list[dict]:
    """Fetch the whole-library `feeds` subscription rows, spec D5's exact
    column subset — durable subscription state only. Transient fetch validators
    (last_etag/last_modified/error_count/last_error/last_fetched_at) are
    deliberately excluded (regenerable on the next poll), and `feed_entries` is
    not exported at all (regenerable dedup ledger)."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT uid, url, title, site_url, folder, source_id, "
            "fetch_interval_minutes, status, created_at FROM feeds ORDER BY id"
        ).fetchall()
    ]


def export_opml(config: TiroConfig) -> str:
    """OPML 2.0 of all sources. A source backed by a Phase 4 RSS feed carries
    `type="rss"` + the feed's `xmlUrl` (and `htmlUrl` from the feed's site_url
    or the source domain); web sources carry htmlUrl only; email sources are
    name-only."""
    import xml.etree.ElementTree as ET

    conn = get_connection(config.db_path)
    try:
        sources = conn.execute(
            "SELECT id, name, domain, source_type FROM sources ORDER BY name"
        ).fetchall()
        # source_id -> (feed url, site_url) for feed-backed sources. A source
        # can only back one feed in practice; take the first if somehow more.
        feed_by_source: dict[int, tuple[str, str | None]] = {}
        for f in conn.execute(
            "SELECT source_id, url, site_url FROM feeds WHERE source_id IS NOT NULL"
        ).fetchall():
            feed_by_source.setdefault(f["source_id"], (f["url"], f["site_url"]))
    finally:
        conn.close()

    opml = ET.Element("opml", version="2.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = "Tiro sources"
    body = ET.SubElement(opml, "body")
    for s in sources:
        attrs = {"text": s["name"], "title": s["name"]}
        feed = feed_by_source.get(s["id"])
        if feed is not None:
            feed_url, site_url = feed
            attrs["type"] = "rss"
            attrs["xmlUrl"] = feed_url
            if site_url:
                attrs["htmlUrl"] = site_url
            elif s["domain"]:
                attrs["htmlUrl"] = f"https://{s['domain']}"
        elif s["domain"]:
            attrs["htmlUrl"] = f"https://{s['domain']}"
        ET.SubElement(body, "outline", attrs)
    return ET.tostring(opml, encoding="unicode", xml_declaration=True)


def _get_tags(conn, article_ids: list[int]) -> list[dict]:
    """Fetch tags referenced by the filtered articles."""
    if not article_ids:
        return []

    placeholders = ",".join("?" * len(article_ids))
    rows = conn.execute(
        f"""
        SELECT DISTINCT t.* FROM tags t
        JOIN article_tags at_ ON t.id = at_.tag_id
        WHERE at_.article_id IN ({placeholders})
        ORDER BY t.name
        """,
        article_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def _get_entities(conn, article_ids: list[int]) -> list[dict]:
    """Fetch entities referenced by the filtered articles."""
    if not article_ids:
        return []

    placeholders = ",".join("?" * len(article_ids))
    rows = conn.execute(
        f"""
        SELECT DISTINCT e.* FROM entities e
        JOIN article_entities ae ON e.id = ae.entity_id
        WHERE ae.article_id IN ({placeholders})
        ORDER BY e.name
        """,
        article_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def _get_relations(conn, article_ids: list[int]) -> list[dict]:
    """Fetch article relations where both sides are in the filtered set."""
    if not article_ids:
        return []

    placeholders = ",".join("?" * len(article_ids))
    rows = conn.execute(
        f"""
        SELECT * FROM article_relations
        WHERE article_id IN ({placeholders})
          AND related_article_id IN ({placeholders})
        """,
        article_ids + article_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def _get_article_tags(conn, article_ids: list[int]) -> list[dict]:
    """Fetch article-tag junction rows for the filtered articles."""
    if not article_ids:
        return []

    placeholders = ",".join("?" * len(article_ids))
    rows = conn.execute(
        f"SELECT * FROM article_tags WHERE article_id IN ({placeholders})",
        article_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def _get_article_entities(conn, article_ids: list[int]) -> list[dict]:
    """Fetch article-entity junction rows for the filtered articles."""
    if not article_ids:
        return []

    placeholders = ",".join("?" * len(article_ids))
    rows = conn.execute(
        f"SELECT * FROM article_entities WHERE article_id IN ({placeholders})",
        article_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def _get_digests(conn) -> list[dict]:
    """Fetch all digest rows (whole-library — not article-filtered)."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT date, digest_type, content, article_ids, created_at FROM digests"
            " ORDER BY date, digest_type"
        ).fetchall()
    ]


def _get_reading_stats(conn) -> list[dict]:
    """Fetch all reading_stats rows (whole-library — not article-filtered)."""
    return [dict(r) for r in conn.execute("SELECT * FROM reading_stats ORDER BY date").fetchall()]


def _get_highlights(conn, article_ids: list[int]) -> list[dict]:
    """Fetch highlight rows for the filtered articles, plus the owning
    article's `uid` (so an importer can re-key without a numeric-id lookup
    across databases -- same rationale as the `source_*` join in
    `_get_articles`)."""
    if not article_ids:
        return []

    placeholders = ",".join("?" * len(article_ids))
    rows = conn.execute(
        f"""SELECT h.*, a.uid AS article_uid
            FROM highlights h
            JOIN articles a ON h.article_id = a.id
            WHERE h.article_id IN ({placeholders})
            ORDER BY h.article_id, h.id""",
        article_ids,
    ).fetchall()
    return [dict(r) for r in rows]


def _get_notes(conn, article_ids: list[int]) -> list[dict]:
    """Fetch note rows (both kinds -- article-level has `highlight_id IS
    NULL`, highlight-anchored does not) for the filtered articles, plus the
    owning article's `uid`."""
    if not article_ids:
        return []

    placeholders = ",".join("?" * len(article_ids))
    rows = conn.execute(
        f"""SELECT n.*, a.uid AS article_uid
            FROM notes n
            JOIN articles a ON n.article_id = a.id
            WHERE n.article_id IN ({placeholders})
            ORDER BY n.article_id, n.id""",
        article_ids,
    ).fetchall()
    return [dict(r) for r in rows]


def _get_audio_metadata(conn, article_ids: list[int]) -> list[dict]:
    """Fetch audio metadata for the filtered articles, excluding file_path."""
    if not article_ids:
        return []

    placeholders = ",".join("?" * len(article_ids))
    return [
        dict(r)
        for r in conn.execute(
            f"""SELECT article_id, voice, model, duration_seconds, file_size_bytes,
                       generated_at
                FROM audio WHERE article_id IN ({placeholders})""",
            article_ids,
        ).fetchall()
    ]


def _bundle_readme(article_count: int) -> str:
    """Generate a README.md for the export bundle."""
    return f"""# Tiro Library Export

This bundle was exported from [Tiro](https://github.com/esagduyu/tiro), a local-first reading OS.

## Contents

- **articles/**: {article_count} markdown files with YAML frontmatter (title, author, tags, entities, summary, etc.)
- **wiki/**: LLM-maintained synthesis pages (Phase 1b), present only if the library has any
- **annotations/**: `{{stem}}.jsonl` highlight sidecars (Phase 2 M2.1), one per article that has highlights, scoped to exported articles only
- **notes/**: `{{stem}}.md` article-level note sidecars (Phase 2 M2.1), one per article that has a note, scoped to exported articles only
- **metadata.json**: Full structured data including articles, sources, tags, entities, ratings, article relations, digests, reading stats, audio metadata, highlights, and notes
- **README.md**: This file

## Markdown File Format

Each article is a markdown file with YAML frontmatter:

```yaml
---
title: "Article Title"
author: "Author Name"
source: "source.com"
url: "https://..."
published: 2026-02-10
ingested: 2026-02-11T14:30:00
tags: ["ai", "technology"]
entities: ["Company A", "Person B"]
word_count: 2450
reading_time: 10 min
---

# Article Title

[Full article content in clean markdown...]
```

## metadata.json Schema

```json
{{
  "exported_at": "ISO 8601 timestamp",
  "tiro_version": "0.2.0",  // illustrative; actual value reflects the exporting Tiro version
  "filters": {{ "tag": null, "source_id": null, "rating_min": null, "date_from": null }},
  "articles": [ {{ "id": 1, "title": "...", "rating": 1, "ai_tier": "must-read", "ingenuity_analysis": "...", ... }} ],
  "sources": [ {{ "id": 1, "name": "...", "domain": "...", "is_vip": true, ... }} ],
  "tags": [ {{ "id": 1, "name": "ai" }} ],
  "entities": [ {{ "id": 1, "name": "Anthropic", "entity_type": "company" }} ],
  "relations": [ {{ "article_id": 1, "related_article_id": 2, "similarity_score": 0.85, "connection_note": "..." }} ],
  "article_tags": [ {{ "article_id": 1, "tag_id": 1 }} ],
  "article_entities": [ {{ "article_id": 1, "entity_id": 1 }} ],
  "digests": [ {{ "date": "2026-07-01", "digest_type": "ranked", "content": "## ...", "article_ids": "[1,2]", "created_at": "..." }} ],
  "reading_stats": [ {{ "date": "2026-07-01", "articles_saved": 3, "articles_read": 1, "articles_rated": 0, "total_reading_time_min": 12 }} ],
  "audio": [ {{ "article_id": 1, "voice": "nova", "model": "tts-1", "duration_seconds": 180.5, "file_size_bytes": 204800, "generated_at": "..." }} ],
  "highlights": [ {{ "id": 1, "uid": "...", "article_id": 1, "article_uid": "...", "quote_text": "...", "color": "yellow", "text_position_start": 0, "text_position_end": 11, ... }} ],
  "notes": [ {{ "id": 1, "uid": "...", "article_id": 1, "article_uid": "...", "highlight_id": null, "body_markdown": "...", ... }} ],
  "feeds": [ {{ "uid": "...", "url": "https://blog.example.com/feed.xml", "title": "Example", "site_url": "https://blog.example.com", "folder": "Tech", "source_id": 3, "fetch_interval_minutes": 60, "status": "active", "created_at": "..." }} ]
}}
```

Note: `ingenuity_analysis` is not a separate top-level key — it rides along inside each article record in `articles[*].ingenuity_analysis` (JSON string or null). `digests`, `reading_stats`, and `feeds` are whole-library (not scoped to the export's article filters); `audio`/`highlights`/`notes` are scoped to the filtered articles. `feeds` (Phase 4 RSS subscriptions) carries durable subscription state only — transient fetch validators (etag/last-modified/error counters) and the regenerable `feed_entries` dedup ledger are excluded. `audio` deliberately omits the internal `file_path`. `highlights`/`notes` are the derived-table fallback for an importer that can't read the `annotations/`/`notes/` sidecar files directly (see those directories above) — a `notes` row with `highlight_id` set is a highlight-anchored note, `highlight_id: null` is the one article-level note.

## Re-importing

These files are standard markdown with YAML frontmatter, readable by any tool that supports frontmatter (Obsidian, Hugo, Jekyll, python-frontmatter, etc.).

The metadata.json contains the full relational data if you need to reconstruct the database.

---

*Exported by Tiro — own your context.*
"""
