"""Common processing pipeline for all ingestion connectors."""

import logging
import math
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import frontmatter

from tiro.authors import link_article_author
from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.ingestion.extractors import extract_metadata
from tiro.migrations import canonical_key, new_ulid
from tiro.search.semantic import find_related_articles, generate_connection_notes, store_relations
from tiro.stats import update_stat
from tiro.vectorstore import get_collection
from tiro.wiki import mark_pages_stale

logger = logging.getLogger(__name__)


def generate_slug(title: str, dt: datetime) -> str:
    """Generate a filename-safe slug from title and date."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:80].rstrip("-")
    return f"{dt.strftime('%Y-%m-%d')}_{slug}"


def _ensure_unique_slug(slug: str, articles_dir: Path) -> str:
    """Append a numeric suffix if a file with this slug already exists."""
    if not (articles_dir / f"{slug}.md").exists():
        return slug
    n = 2
    while (articles_dir / f"{slug}-{n}.md").exists():
        n += 1
    return f"{slug}-{n}"


def _get_or_create_source(conn, domain: str) -> int:
    """Find existing source by domain or create a new one. Returns source_id."""
    row = conn.execute(
        "SELECT id FROM sources WHERE domain = ?", (domain,)
    ).fetchone()
    if row:
        return row["id"]

    source_name = domain.removeprefix("www.")
    cursor = conn.execute(
        "INSERT INTO sources (name, domain, source_type) VALUES (?, ?, ?)",
        (source_name, domain, "web"),
    )
    conn.commit()
    return cursor.lastrowid


def _related_wikilinks(conn, relations: list[dict]) -> list[str]:
    """Build Obsidian `[[stem]]` wikilink strings for an article's related
    articles, in relation order (most-similar first).

    Stems follow the same convention as wiki citations (tiro/wiki_gen.py):
    `markdown_path` with the trailing `.md` stripped. Relations whose
    article row can't be found (shouldn't happen -- `find_related_articles`
    already filters ChromaDB orphans) are silently skipped rather than
    producing a dead wikilink.
    """
    if not relations:
        return []
    ids = [r["related_article_id"] for r in relations]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, markdown_path FROM articles WHERE id IN ({placeholders})", ids
    ).fetchall()
    path_by_id = {row["id"]: row["markdown_path"] for row in rows}
    wikilinks = []
    for rid in ids:
        path = path_by_id.get(rid)
        if not path:
            continue
        stem = path[:-3] if path.endswith(".md") else path
        wikilinks.append(f"[[{stem}]]")
    return wikilinks


def _get_or_create_email_source(conn, sender_name: str, sender_email: str) -> int:
    """Find existing source by email sender or create a new one. Returns source_id."""
    row = conn.execute(
        "SELECT id FROM sources WHERE email_sender = ?", (sender_email,)
    ).fetchone()
    if row:
        return row["id"]

    cursor = conn.execute(
        "INSERT INTO sources (name, email_sender, source_type) VALUES (?, ?, ?)",
        (sender_name, sender_email, "email"),
    )
    conn.commit()
    return cursor.lastrowid


def process_article(
    *,
    title: str,
    author: str | None,
    content_md: str,
    url: str,
    config: TiroConfig,
    published_at: datetime | None = None,
    email_sender: str | None = None,
    ingestion_method: str = "manual",
    source_id: int | None = None,
) -> dict:
    """Run the full storage pipeline: save markdown, insert SQLite, embed in ChromaDB.

    Args:
        published_at: Override published date (used by email connector for Date header).
        email_sender: Sender email address (used by email connector for source matching).
        ingestion_method: How the article was saved (manual/extension/email/imap/rss).
        source_id: Pre-resolved source row id (Phase 4 M4.0, RSS). When given,
            skip domain/email source matching entirely and attribute the
            article to this exact source (the feed's `sources` row, created at
            subscribe time) — the source-forcing mechanism the RSS pipeline
            uses, parallel to how email matches on sender. `email_sender` is
            ignored when `source_id` is passed. Keyword-only, default None, so
            no existing caller changes.

    Returns a dict of the created article metadata.
    """
    now = datetime.now()
    pub_date = published_at or now

    # --- Word count & reading time ---
    word_count = len(content_md.split())
    reading_time_min = max(1, math.ceil(word_count / 250))

    # --- Slug & file path ---
    slug = generate_slug(title, pub_date)
    slug = _ensure_unique_slug(slug, config.articles_dir)
    md_filename = f"{slug}.md"
    md_path = config.articles_dir / md_filename

    # --- Source detection ---
    # A pre-resolved source_id (RSS, Phase 4) wins: skip domain/email matching
    # and attribute to that exact source. source_name is read back from the row
    # below for frontmatter/ChromaDB metadata.
    forced_source_id = source_id
    if forced_source_id is not None:
        source_name = None  # filled from the sources row below
        domain = None
    elif email_sender:
        source_name = author or email_sender.split("@")[0]
        domain = None
    else:
        domain = urlparse(url).netloc
        source_name = domain.removeprefix("www.")

    conn = get_connection(config.db_path)
    try:
        if forced_source_id is not None:
            source_id = forced_source_id
        elif email_sender:
            source_id = _get_or_create_email_source(conn, source_name, email_sender)
        else:
            source_id = _get_or_create_source(conn, domain)

        # Check VIP status for ChromaDB metadata (and, for a forced source_id,
        # the display name to stamp into frontmatter/vectors).
        source_row = conn.execute(
            "SELECT name, is_vip FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        is_vip = bool(source_row["is_vip"]) if source_row else False
        if forced_source_id is not None:
            source_name = source_row["name"] if source_row else (author or "")

        # --- Save markdown file with YAML frontmatter ---
        post = frontmatter.Post(content_md)
        post.metadata = {
            "title": title,
            "author": author,
            "source": source_name,
            "url": url or "",
            "published": pub_date.strftime("%Y-%m-%d"),
            "ingested": now.isoformat(timespec="seconds"),
            "tags": [],
            "entities": [],
            "word_count": word_count,
            "reading_time": f"{reading_time_min} min",
        }
        if config.obsidian_compatible_mode:
            # Obsidian-vault compatibility (PRODUCT_ROADMAP.md Phase 2,
            # format-only -- bidirectional sync is Phase 2b). `tags` above is
            # already a plain YAML list, which is exactly the format Obsidian
            # expects, so nothing to change there. `aliases`/`created` are
            # Obsidian conventions with no existing Tiro equivalent, added
            # here. `related` (wikilinks) is intentionally NOT set yet: it
            # depends on relation computation, which runs later in this same
            # function (after the ChromaDB add) -- see the targeted
            # frontmatter rewrite right after store_relations() below.
            post.metadata["aliases"] = []
            post.metadata["created"] = pub_date.isoformat(timespec="seconds")
        md_path.write_text(frontmatter.dumps(post))
        logger.info("Saved markdown to %s", md_path)

        # --- Insert into SQLite ---
        # The markdown file above already exists on disk but no DB row
        # references it yet. If the INSERT (or its commit) raises, unlink
        # the file so it doesn't become an orphan, then re-raise.
        try:
            cursor = conn.execute(
                """INSERT INTO articles
                   (uid, source_id, title, author, url, slug, markdown_path,
                    word_count, reading_time_min, published_at, ingested_at,
                    ingestion_method)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_ulid(),
                    source_id,
                    title,
                    author,
                    url or "",
                    slug,
                    md_filename,
                    word_count,
                    reading_time_min,
                    pub_date.isoformat() if published_at else None,
                    now.isoformat(),
                    ingestion_method,
                ),
            )
            article_id = cursor.lastrowid
            link_article_author(conn, article_id, author)
            conn.commit()
        except Exception:
            md_path.unlink(missing_ok=True)
            raise
        logger.info("Inserted article %d into SQLite", article_id)

        # --- Update reading stats ---
        try:
            update_stat(config, "articles_saved")
        except Exception as e:
            logger.error("Failed to update reading stats: %s", e)

        # Row + markdown file now exist and are committed. Everything below
        # this point is rollback-guarded: a failure in enrichment/frontmatter
        # unwinds via delete_article() and re-raises (no orphan row/file).
        try:
            # --- AI metadata extraction (Haiku) ---
            ai = extract_metadata(title, content_md, config)
            summary = ai["summary"]
            tag_names = ai["tags"]
            entity_list = ai["entities"]

            if summary:
                conn.execute(
                    "UPDATE articles SET summary = ? WHERE id = ?",
                    (summary, article_id),
                )

            for tag_name in tag_names:
                conn.execute(
                    "INSERT OR IGNORE INTO tags (uid, name) VALUES (?, ?)",
                    (new_ulid(), tag_name),
                )
                tag_row = conn.execute(
                    "SELECT id FROM tags WHERE name = ?", (tag_name,)
                ).fetchone()
                conn.execute(
                    "INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (?, ?)",
                    (article_id, tag_row["id"]),
                )

            for entity in entity_list:
                key = canonical_key(entity["name"])
                ent_row = conn.execute(
                    "SELECT id FROM entities WHERE entity_type = ? AND canonical_key = ?",
                    (entity["type"], key),
                ).fetchone()
                if ent_row:
                    entity_id = ent_row["id"]
                else:
                    cursor = conn.execute(
                        "INSERT INTO entities (uid, name, entity_type, canonical_key)"
                        " VALUES (?, ?, ?, ?)",
                        (new_ulid(), entity["name"], entity["type"], key),
                    )
                    entity_id = cursor.lastrowid
                conn.execute(
                    "INSERT OR IGNORE INTO article_entities (article_id, entity_id) VALUES (?, ?)",
                    (article_id, entity_id),
                )

            # --- Mark stale any wiki pages for entities/tags this article
            # just linked to (Phase 1b). Free SQL + frontmatter rewrite, no
            # LLM -- but best-effort: this is bookkeeping on top of an
            # already-successful ingest, not something worth unwinding the
            # whole article over. A failure here is logged and swallowed
            # rather than left to propagate into the enrichment stage's
            # rollback-via-delete_article() below (same non-fatal pattern as
            # the ChromaDB/related-articles steps further down).
            try:
                mark_pages_stale(config, conn, article_id)
            except Exception as e:
                logger.error("mark_pages_stale failed for article %d (non-fatal): %s", article_id, e)

            conn.commit()

            # Update frontmatter with AI-extracted metadata
            post.metadata["tags"] = tag_names
            post.metadata["entities"] = [e["name"] for e in entity_list]
            if summary:
                post.metadata["summary"] = summary
            md_path.write_text(frontmatter.dumps(post))

            # --- Store in ChromaDB (non-fatal: retry loop indexes failures) ---
            try:
                collection = get_collection()
                collection.upsert(
                    ids=[f"article_{article_id}"],
                    documents=[content_md],
                    metadatas=[
                        {
                            "title": title,
                            "source": source_name,
                            "is_vip": is_vip,
                            "tags": ",".join(tag_names),
                            "published_at": pub_date.strftime("%Y-%m-%d"),
                            "article_id": article_id,
                        }
                    ],
                )
                conn.execute(
                    "UPDATE articles SET vector_status = 'indexed' WHERE id = ?",
                    (article_id,),
                )
                conn.commit()
                logger.info("Added article %d to ChromaDB", article_id)
            except Exception as e:
                logger.error("ChromaDB add failed for %d (will retry): %s", article_id, e)
                conn.execute(
                    "UPDATE articles SET vector_status = 'pending' WHERE id = ?",
                    (article_id,),
                )
                conn.commit()

            # --- Find and store related articles (non-fatal) ---
            try:
                relations = find_related_articles(article_id, config, limit=5)
                if relations:
                    generate_connection_notes(summary or "", title, relations, config)
                    store_relations(article_id, relations, config)

                if config.obsidian_compatible_mode:
                    # Targeted frontmatter update, same ingest call, no
                    # background rewriter: relations are only known at this
                    # point (computed after the ChromaDB add, which is after
                    # the two frontmatter writes above), so `related:` gets
                    # its own best-effort write here. Non-fatal like the rest
                    # of this block -- a failure here never unwinds a
                    # successful ingest.
                    post.metadata["related"] = _related_wikilinks(conn, relations)
                    md_path.write_text(frontmatter.dumps(post))
            except Exception as e:
                logger.error("Related articles failed for %d: %s", article_id, e)
        except Exception:
            # Enrichment/frontmatter stage failed: unwind the row + file.
            # delete_article() opens its own connection, so release ours
            # first to avoid a SQLite write-lock deadlock.
            conn.close()
            from tiro.lifecycle import delete_article

            try:
                delete_article(config, article_id)
            except Exception as rollback_err:
                logger.error(
                    "Rollback failed for article %d — orphan row/file may remain: %s",
                    article_id, rollback_err,
                )
            raise

        return {
            "id": article_id,
            "title": title,
            "author": author,
            "url": url,
            "slug": slug,
            "source": source_name,
            "source_id": source_id,
            "word_count": word_count,
            "reading_time_min": reading_time_min,
            "markdown_path": md_filename,
            "summary": summary,
            "tags": tag_names,
        }
    finally:
        conn.close()
