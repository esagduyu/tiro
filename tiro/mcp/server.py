"""Tiro MCP server — exposes the reading library to Claude Desktop and Claude Code."""

import asyncio
import json
import logging
from datetime import date
from pathlib import Path

import frontmatter
from mcp.server.fastmcp import FastMCP

from tiro.config import TiroConfig, load_config
from tiro.database import get_connection, init_db, migrate_db
from tiro.queries import build_article_filters
from tiro.vectorstore import get_collection, init_vectorstore
from tiro.wiki import read_page

logger = logging.getLogger(__name__)

mcp = FastMCP("Tiro Reading Library")

# Module-level config, initialized in main()
_config: TiroConfig | None = None


def _config_path() -> str:
    """Config path for the MCP server. Claude Desktop spawns us with an
    arbitrary CWD, so ./config.yaml is usually wrong there — set TIRO_CONFIG
    in the MCP client's env block to the absolute path."""
    import os

    return os.environ.get("TIRO_CONFIG", "config.yaml")


def _require_token_gate(config: TiroConfig) -> None:
    """Single-user gating for the MCP server. When the Tiro instance has a
    password configured, the MCP process must present a valid API token via
    the TIRO_API_TOKEN env var (set it in the MCP client's "env" block).

    Called on EVERY _get_config() lookup (i.e. on every tool invocation),
    not just at process startup — this is one indexed DB lookup, and it's
    what lets revoking the token (tiro token revoke) actually cut off a
    long-running MCP server process on its next call, instead of only
    affecting future server restarts."""
    import os

    if not config.auth_password_hash:
        return

    token = os.environ.get("TIRO_API_TOKEN", "")
    if not token:
        raise RuntimeError(
            "Tiro has a password configured. Set TIRO_API_TOKEN to a valid "
            "API token (create one with: tiro token create mcp) in your MCP "
            "client config's env block."
        )
    import sqlite3

    from tiro import auth

    try:
        valid = auth.validate_api_token(config.db_path, token)
    except sqlite3.OperationalError as e:
        raise RuntimeError(
            "Tiro library is not initialized — run `uv run tiro init` (and "
            "start Tiro once) before connecting the MCP server."
        ) from e
    if not valid:
        raise RuntimeError(
            "TIRO_API_TOKEN is not a valid API token. Create one with: "
            "tiro token create mcp"
        )


def _get_config() -> TiroConfig:
    global _config
    if _config is None:
        _config = load_config(_config_path())
        # Mirror the app.py lifespan order: bring SQLite up to the latest
        # schema (both idempotent/user_version-gated) before any query runs,
        # since MCP SQL relies on columns added after the hackathon schema
        # (display_date, uid) and, on legacy DBs, the Phase-0 auth tables
        # that _require_token_gate() below queries via tiro.auth.
        init_db(_config.db_path)
        migrate_db(_config.db_path)
        # Initialize ChromaDB so get_collection() works
        init_vectorstore(_config.chroma_dir, _config.default_embedding_model)
    # Enforced on every call (not just first init) so token revocation takes
    # effect on the next tool invocation, not only on server restart.
    _require_token_gate(_config)
    return _config


def _format_articles(rows, tags_map, score_map=None) -> str:
    """Format article rows into readable text output."""
    lines = []
    for r in rows:
        r = dict(r) if not isinstance(r, dict) else r
        aid = r["id"]
        vip = " [VIP]" if r.get("is_vip") else ""
        tags = ", ".join(tags_map.get(aid, []))
        rating_label = {-1: "👎", 1: "👍", 2: "❤️"}.get(r.get("rating"), "")
        tier = f" [{r['ai_tier']}]" if r.get("ai_tier") else ""
        score_str = f", similarity: {score_map[aid]:.0%}" if score_map and aid in score_map else ""
        lines.append(
            f"- **{r['title']}** (ID: {aid}{score_str}){vip}{tier} {rating_label}\n"
            f"  Source: {r.get('source_name') or 'Unknown'} | "
            f"{r.get('reading_time_min') or '?'} min read | "
            f"Tags: {tags or 'none'}\n"
            f"  Summary: {r.get('summary') or 'No summary'}\n"
        )
    return "\n".join(lines)


def _batch_fetch_tags(conn, article_ids) -> dict[int, list[str]]:
    """Fetch tags for a list of article IDs."""
    if not article_ids:
        return {}
    placeholders = ",".join("?" * len(article_ids))
    tag_rows = conn.execute(
        f"""SELECT at.article_id, t.name
            FROM article_tags at
            JOIN tags t ON at.tag_id = t.id
            WHERE at.article_id IN ({placeholders})""",
        article_ids,
    ).fetchall()
    tags_map: dict[int, list[str]] = {}
    for tr in tag_rows:
        tags_map.setdefault(tr["article_id"], []).append(tr["name"])
    return tags_map


def _build_filter_sql(
    *,
    author: str = "",
    source: str = "",
    tag: str = "",
    ai_tier: str = "",
    is_unread: bool | None = None,
    is_vip: bool | None = None,
    rating_min: int | None = None,
    date_from: str = "",
    date_to: str = "",
    ingestion_method: str = "",
    article_ids: list[int] | None = None,
) -> tuple[str, list]:
    """Build WHERE clause and params from filter arguments.

    Delegates the facets shared with the API/inbox (read status, tier, tag,
    ingestion method, VIP, date range) to the single shared
    ``build_article_filters()``. Facets this MCP tool supports that the
    shared builder doesn't cover — fuzzy author/source matching, the
    rating_min threshold, and the semantic-search candidate-id restriction —
    are appended as MCP-specific clauses after the builder returns.

    Note: ``is_unread`` is the MCP tool's naming for the inverse of the
    builder's ``is_read`` (is_unread=True -> is_read=False)."""
    is_read = (not is_unread) if is_unread is not None else None

    where_sql, params = build_article_filters(
        is_read=is_read,
        is_vip=is_vip,
        ai_tier=ai_tier or None,
        tag=tag.lower().strip() if tag else None,
        ingestion_method=ingestion_method or None,
        date_from=date_from or None,
        date_to=date_to or None,
    )

    conditions = []
    if where_sql:
        conditions.append(where_sql.removeprefix(" WHERE "))

    if article_ids is not None:
        placeholders = ",".join("?" * len(article_ids))
        conditions.append(f"a.id IN ({placeholders})")
        params.extend(article_ids)

    if rating_min is not None:
        conditions.append("a.rating >= ?")
        params.append(rating_min)

    if source:
        conditions.append("(s.name LIKE ? OR s.domain LIKE ?)")
        params.extend([f"%{source}%", f"%{source}%"])

    if author:
        conditions.append("a.author LIKE ?")
        params.append(f"%{author}%")

    where = " AND ".join(conditions) if conditions else "1=1"
    return where, params


@mcp.tool()
def search_articles(
    query: str = "",
    author: str = "",
    source: str = "",
    tag: str = "",
    ai_tier: str = "",
    is_unread: bool | None = None,
    is_vip: bool | None = None,
    rating_min: int | None = None,
    date_from: str = "",
    date_to: str = "",
    ingestion_method: str = "",
    max_results: int = 20,
) -> str:
    """Search the reading library with optional filters. If query is provided, searches by semantic similarity then applies filters. If query is empty, does a SQL-only filtered search. Filters: author, source (name/domain), tag, ai_tier (must-read/summary-enough/discard), is_unread, is_vip, rating_min (-1/1/2), date_from/date_to (YYYY-MM-DD), ingestion_method (manual/extension/imap/api)."""
    config = _get_config()

    # Semantic search first if query provided
    candidate_ids = None
    score_map = {}
    if query:
        collection = get_collection()
        count = collection.count()
        if count == 0:
            return "No articles in the library yet."

        results = collection.query(
            query_texts=[query],
            n_results=min(max_results * 2, count),
            include=["metadatas", "distances"],
        )

        if not results["ids"] or not results["ids"][0]:
            return "No matching articles found."

        candidate_ids = []
        for chroma_id, distance in zip(results["ids"][0], results["distances"][0], strict=False):
            article_id = int(chroma_id.replace("article_", ""))
            similarity = round(1 - (distance / 2), 4)
            candidate_ids.append(article_id)
            score_map[article_id] = similarity

    where, params = _build_filter_sql(
        author=author, source=source, tag=tag, ai_tier=ai_tier,
        is_unread=is_unread, is_vip=is_vip, rating_min=rating_min,
        date_from=date_from, date_to=date_to, ingestion_method=ingestion_method,
        article_ids=candidate_ids,
    )

    conn = get_connection(config.db_path)
    try:
        sql = f"""SELECT a.id, a.title, a.author, a.summary, a.reading_time_min,
                         a.ingested_at, a.is_read, a.rating, a.url, a.ai_tier,
                         s.name AS source_name, s.is_vip, s.source_type
                  FROM articles a
                  LEFT JOIN sources s ON a.source_id = s.id
                  WHERE {where}
                  ORDER BY s.is_vip DESC, a.display_date DESC
                  LIMIT ?"""
        params.append(max_results)
        rows = conn.execute(sql, params).fetchall()

        if not rows:
            desc = f'matching "{query}"' if query else "matching filters"
            return f"No articles found {desc}."

        # If semantic search, sort by similarity
        if score_map:
            rows = sorted(rows, key=lambda r: score_map.get(r["id"], 0), reverse=True)

        article_ids = [r["id"] for r in rows]
        tags_map = _batch_fetch_tags(conn, article_ids)

        desc = f'matching "{query}"' if query else "matching filters"
        header = f"Found {len(rows)} articles {desc}:\n\n"
        return header + _format_articles(rows, tags_map, score_map if score_map else None)
    finally:
        conn.close()


@mcp.tool()
def list_filters() -> str:
    """List available filter values with counts. Use this to discover what authors, sources, tags, tiers, and ingestion methods exist in the library before searching."""
    config = _get_config()
    conn = get_connection(config.db_path)
    try:
        lines = ["## Available Filters\n"]

        # Tiers
        tier_rows = conn.execute(
            "SELECT ai_tier, COUNT(*) as count FROM articles WHERE ai_tier IS NOT NULL GROUP BY ai_tier ORDER BY count DESC"
        ).fetchall()
        unclassified = conn.execute("SELECT COUNT(*) as count FROM articles WHERE ai_tier IS NULL").fetchone()["count"]
        lines.append("### Tiers")
        for r in tier_rows:
            lines.append(f"- {r['ai_tier']}: {r['count']}")
        lines.append(f"- unclassified: {unclassified}\n")

        # Sources
        source_rows = conn.execute(
            """SELECT s.name, s.domain, COUNT(a.id) as count, s.is_vip, s.source_type
               FROM sources s LEFT JOIN articles a ON s.id = a.source_id
               GROUP BY s.id ORDER BY count DESC"""
        ).fetchall()
        lines.append("### Sources")
        for r in source_rows:
            vip = " [VIP]" if r["is_vip"] else ""
            lines.append(f"- {r['name']} ({r['source_type']}): {r['count']} articles{vip}")
        lines.append("")

        # Tags (top 30)
        tag_rows = conn.execute(
            """SELECT t.name, COUNT(at.article_id) as count
               FROM tags t JOIN article_tags at ON t.id = at.tag_id
               GROUP BY t.name ORDER BY count DESC LIMIT 30"""
        ).fetchall()
        lines.append("### Tags (top 30)")
        for r in tag_rows:
            lines.append(f"- {r['name']}: {r['count']}")
        lines.append("")

        # Ratings
        rating_rows = conn.execute(
            "SELECT rating, COUNT(*) as count FROM articles WHERE rating IS NOT NULL GROUP BY rating ORDER BY rating DESC"
        ).fetchall()
        unrated = conn.execute("SELECT COUNT(*) as count FROM articles WHERE rating IS NULL").fetchone()["count"]
        labels = {-1: "Disliked", 1: "Liked", 2: "Loved"}
        lines.append("### Ratings")
        for r in rating_rows:
            lines.append(f"- {labels.get(r['rating'], r['rating'])}: {r['count']}")
        lines.append(f"- Unrated: {unrated}\n")

        # Ingestion methods
        method_rows = conn.execute(
            "SELECT ingestion_method, COUNT(*) as count FROM articles GROUP BY ingestion_method ORDER BY count DESC"
        ).fetchall()
        lines.append("### Ingestion Methods")
        for r in method_rows:
            lines.append(f"- {r['ingestion_method'] or 'unknown'}: {r['count']}")
        lines.append("")

        # Read status
        read = conn.execute("SELECT COUNT(*) as count FROM articles WHERE is_read = 1").fetchone()["count"]
        unread = conn.execute("SELECT COUNT(*) as count FROM articles WHERE is_read = 0").fetchone()["count"]
        lines.append(f"### Read Status\n- Read: {read}\n- Unread: {unread}\n")

        total = conn.execute("SELECT COUNT(*) as count FROM articles").fetchone()["count"]
        lines.append(f"**Total articles: {total}**")

        return "\n".join(lines)
    finally:
        conn.close()


@mcp.tool()
def get_article(article_id: int) -> str:
    """Get the full content and metadata of a specific article by its ID."""
    config = _get_config()
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            """SELECT a.id, a.title, a.author, a.url, a.summary,
                      a.word_count, a.reading_time_min, a.published_at, a.ingested_at,
                      a.is_read, a.rating, a.markdown_path,
                      s.name AS source_name, s.is_vip, s.source_type
               FROM articles a
               LEFT JOIN sources s ON a.source_id = s.id
               WHERE a.id = ?""",
            (article_id,),
        ).fetchone()

        if not row:
            return f"Article with ID {article_id} not found."

        article = dict(row)

        # Fetch tags
        tag_rows = conn.execute(
            """SELECT t.name FROM article_tags at
               JOIN tags t ON at.tag_id = t.id
               WHERE at.article_id = ?""",
            (article_id,),
        ).fetchall()
        tags = [r["name"] for r in tag_rows]

        # Fetch entities
        entity_rows = conn.execute(
            """SELECT e.name, e.entity_type FROM article_entities ae
               JOIN entities e ON ae.entity_id = e.id
               WHERE ae.article_id = ?""",
            (article_id,),
        ).fetchall()
        entities = [f"{r['name']} ({r['entity_type']})" for r in entity_rows]

        # Read markdown content
        md_path = config.articles_dir / article["markdown_path"]
        content = ""
        if md_path.exists():
            post = frontmatter.load(str(md_path))
            content = post.content

        vip = " [VIP Source]" if article["is_vip"] else ""
        rating_label = {-1: "Disliked", 1: "Liked", 2: "Loved"}.get(article["rating"], "Unrated")

        header = (
            f"# {article['title']}\n\n"
            f"**Source:** {article['source_name'] or 'Unknown'}{vip}\n"
            f"**Author:** {article['author'] or 'Unknown'}\n"
            f"**Published:** {article['published_at'] or 'Unknown'}\n"
            f"**Reading time:** {article['reading_time_min'] or '?'} min "
            f"({article['word_count'] or '?'} words)\n"
            f"**Rating:** {rating_label}\n"
            f"**URL:** {article['url'] or 'N/A'}\n"
            f"**Tags:** {', '.join(tags) or 'none'}\n"
            f"**Entities:** {', '.join(entities) or 'none'}\n\n"
            f"## Summary\n{article['summary'] or 'No summary'}\n\n"
            f"## Full Content\n{content}"
        )
        return header
    finally:
        conn.close()


@mcp.tool()
def get_digest(digest_type: str = "ranked") -> str:
    """Get today's daily digest. Types: 'ranked' (by importance), 'by_topic' (grouped by theme), 'by_entity' (grouped by people/companies)."""
    config = _get_config()
    today = date.today().isoformat()

    conn = get_connection(config.db_path)
    try:
        # Try today first, then fall back to most recent
        if digest_type not in ("ranked", "by_topic", "by_entity"):
            return f"Invalid digest type '{digest_type}'. Use: ranked, by_topic, or by_entity."

        row = conn.execute(
            """SELECT content, article_ids, created_at, date FROM digests
               WHERE digest_type = ?
               ORDER BY CASE WHEN date = ? THEN 0 ELSE 1 END, date DESC
               LIMIT 1""",
            (digest_type, today),
        ).fetchone()

        if not row:
            return (
                "No digest found. Generate one first by visiting the Tiro web UI "
                "and clicking the Digest tab, or calling POST /api/digest/today on the running server."
            )

        digest_date = row["date"]
        created = row["created_at"]
        content = row["content"]
        article_ids = json.loads(row["article_ids"])

        header = (
            f"## Daily Digest — {digest_type.replace('_', ' ').title()}\n"
            f"*Generated: {created} | Date: {digest_date} | "
            f"Based on {len(article_ids)} articles*\n\n"
        )
        return header + content
    finally:
        conn.close()


@mcp.tool()
def get_articles_by_tag(tag: str) -> str:
    """Get all articles with a specific tag. Tags are lowercase topic keywords extracted from articles."""
    return search_articles(tag=tag, max_results=50)


@mcp.tool()
def get_articles_by_source(source: str) -> str:
    """Get all articles from a specific source. Matches by source name or domain."""
    return search_articles(source=source, max_results=50)


@mcp.tool()
def list_wiki_pages() -> str:
    """List the library's wiki pages -- AI-generated notes on entities (people, companies, orgs) and concepts (topics/tags) that recur across your articles. Each entry shows its slug (use with get_wiki_page), kind, title, status (fresh/stale/conflicted), and how many articles it cites."""
    config = _get_config()
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute(
            "SELECT slug, kind, title, status, source_count FROM wiki_pages "
            "ORDER BY kind, title COLLATE NOCASE"
        ).fetchall()
        if not rows:
            return (
                "No wiki pages yet. Wiki pages are generated from entities and "
                "tags that recur across your articles -- save more articles or "
                "generate one from the Tiro web UI."
            )
        lines = ["## Wiki Pages\n"]
        for r in rows:
            lines.append(
                f"- **{r['title']}** (slug: {r['slug']}) [{r['kind']}] -- "
                f"{r['status']}, {r['source_count']} sources"
            )
        return "\n".join(lines)
    finally:
        conn.close()


@mcp.tool()
def get_wiki_page(slug: str) -> str:
    """Get a wiki page's full content by slug (e.g. "entities/anthropic" or "concepts/context-engineering") -- use list_wiki_pages to discover available slugs. Returns a frontmatter summary (title, kind, status, source count, updated) followed by the page body."""
    config = _get_config()

    try:
        page = read_page(config, slug)
    except ValueError:
        page = None

    if page is None:
        conn = get_connection(config.db_path)
        try:
            rows = conn.execute(
                "SELECT slug FROM wiki_pages ORDER BY kind, title COLLATE NOCASE LIMIT 20"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return f"No wiki page found for '{slug}'. No wiki pages exist yet."
        available = ", ".join(r["slug"] for r in rows)
        return f"No wiki page found for '{slug}'. Available slugs: {available}"

    return (
        f"# {page['title']}\n\n"
        f"**Kind:** {page['kind']}\n"
        f"**Status:** {page['status']}\n"
        f"**Sources:** {page['source_count']}\n"
        f"**Updated:** {page['updated_at'] or 'Unknown'}\n\n"
        f"{page['body']}"
    )


@mcp.tool()
def get_highlights(article_id: int | None = None, color: str | None = None, limit: int = 50) -> str:
    """List saved highlights (with any anchored note), newest first. Pass article_id to see highlights on one specific article, or color (yellow/green/blue/pink) to filter by highlight color. Leave both blank to review recent highlights across the whole library."""
    config = _get_config()
    conn = get_connection(config.db_path)
    try:
        clauses = []
        params: list = []
        if article_id is not None:
            clauses.append("h.article_id = ?")
            params.append(article_id)
        if color is not None:
            clauses.append("h.color = ?")
            params.append(color)
        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        rows = conn.execute(
            f"""SELECT h.uid, h.quote_text, h.color, h.created_at,
                       a.id AS article_id, a.title AS article_title,
                       n.body_markdown AS note_markdown
                FROM highlights h
                JOIN articles a ON h.article_id = a.id
                LEFT JOIN notes n ON n.highlight_id = h.id
                {where_sql}
                ORDER BY h.created_at DESC
                LIMIT ?""",
            [*params, limit],
        ).fetchall()

        if not rows:
            return "No highlights found."

        lines = [f"## Highlights ({len(rows)})\n"]
        for r in rows:
            note = f"\n  Note: {r['note_markdown']}" if r["note_markdown"] else ""
            lines.append(
                f'- "{r["quote_text"]}" [{r["color"]}] -- **{r["article_title"]}** '
                f"(article ID: {r['article_id']})\n"
                f"  Highlighted: {r['created_at']}{note}\n"
            )
        return "\n".join(lines)
    finally:
        conn.close()


@mcp.tool()
async def save_url(url: str) -> str:
    """Save a web page to the Tiro reading library by URL. Fetches the page, extracts content, generates tags/summary with AI, and stores it."""
    config = _get_config()

    from tiro.ingestion.processor import process_article
    from tiro.ingestion.web import fetch_and_extract

    try:
        extracted = await fetch_and_extract(url)
    except Exception as e:
        return f"Failed to fetch URL: {e}"

    try:
        result = await asyncio.to_thread(process_article, **extracted, config=config)
    except Exception as e:
        return f"Failed to process article: {e}"

    tags = ", ".join(result.get("tags", []))
    return (
        f"Saved successfully!\n\n"
        f"**{result['title']}** (ID: {result['id']})\n"
        f"Source: {result['source']}\n"
        f"Words: {result['word_count']} | Reading time: {result['reading_time_min']} min\n"
        f"Tags: {tags or 'none'}\n"
        f"Summary: {result.get('summary', 'N/A')}"
    )


@mcp.tool()
def save_email(file_path: str) -> str:
    """Save an email newsletter (.eml file) to the Tiro reading library. Parses the email, extracts content, generates tags/summary with AI, and stores it."""
    config = _get_config()

    from tiro.ingestion.email import parse_eml
    from tiro.ingestion.processor import process_article

    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        return f"File not found: {path}"
    if not path.suffix.lower() == ".eml":
        return f"Expected a .eml file, got: {path.name}"

    try:
        extracted = parse_eml(path)
    except Exception as e:
        return f"Failed to parse email: {e}"

    try:
        result = process_article(**extracted, config=config)
    except Exception as e:
        return f"Failed to process email article: {e}"

    tags = ", ".join(result.get("tags", []))
    return (
        f"Saved successfully!\n\n"
        f"**{result['title']}** (ID: {result['id']})\n"
        f"Source: {result['source']}\n"
        f"Words: {result['word_count']} | Reading time: {result['reading_time_min']} min\n"
        f"Tags: {tags or 'none'}\n"
        f"Summary: {result.get('summary', 'N/A')}"
    )


def main():
    """Entry point for the MCP server."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="tiro-mcp",
        description="Tiro MCP server (stdio transport). Exposes the reading "
        "library to Claude Desktop/Code. Requires TIRO_API_TOKEN when the "
        "Tiro instance has a password. Config: ./config.yaml, or set "
        "TIRO_CONFIG to an absolute path (recommended for Claude Desktop, "
        "which spawns this process with an arbitrary CWD).",
    )
    parser.parse_args()  # handles --help/-h and exits before any heavy init

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    global _config
    _config = load_config(_config_path())
    _require_token_gate(_config)
    init_vectorstore(_config.chroma_dir, _config.default_embedding_model)
    logger.info("Tiro MCP server starting (library: %s)", _config.library)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
