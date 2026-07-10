"""Feed management API routes (Phase 4 M4.0).

CRUD over the `feeds` table plus URL autodiscovery and manual poll triggers.
All routes mount under the `protected` list in tiro/app.py's create_app
(Depends(auth.require_auth)) — the route-walk invariant auto-covers them, no
allowlist entry needed, and mutating POSTs get CSRF checking for free.

Autodiscovery SSRF posture (spec D9): `POST /api/feeds` fetches a user-typed
URL (feed or page) under a scheme allowlist (http/https only), a 30s timeout,
a 5-redirect cap, and a 10 MB size cap. There is deliberately NO IP-range
blocklist — the exact same narrow posture, and rationale, as
`routes_remote.py::post_remote_test`: the URL is typed by the authenticated
single user about their own subscriptions, and rejecting LAN/loopback targets
would break legitimately self-hosted feeds. OPML import/export is M4.1.
"""

import asyncio
import logging
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from lxml import etree
from lxml.html import fromstring
from pydantic import BaseModel

from tiro import __version__, opml
from tiro.database import get_connection
from tiro.ingestion.rss import MAX_FEED_BYTES, check_feeds
from tiro.migrations import new_ulid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/feeds", tags=["feeds"])

_DISCOVER_TIMEOUT = 30.0
_DISCOVER_MAX_REDIRECTS = 5
_FEED_ALTERNATE_TYPES = ("application/rss+xml", "application/atom+xml")
_MAX_OPML_BYTES = 5 * 1024 * 1024  # reject OPML uploads over 5 MB (spec D5)


def _insert_feed(conn, *, feed_url: str, title: str, site_url: str,
                 folder: str | None, interval: int) -> int:
    """Create the feed's `sources` row (source_type='rss') + the `feeds` row.

    Shared by the add-by-URL route and the OPML import route (import skips the
    network autodiscovery around it). Returns the new feed id. Caller commits.
    """
    domain = urlparse(site_url).netloc or urlparse(feed_url).netloc
    cur = conn.execute(
        "INSERT INTO sources (name, domain, source_type) VALUES (?, ?, ?)",
        (title, domain, "rss"),
    )
    source_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO feeds (uid, url, title, site_url, folder, source_id, "
        "fetch_interval_minutes) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (new_ulid(), feed_url, title, site_url, folder, source_id, interval),
    )
    return cur.lastrowid


def _looks_like_feed(parsed) -> bool:
    """feedparser found real feed content: entries, or a feed title/link."""
    return bool(parsed.get("entries") or parsed.get("feed", {}).get("title")
                or parsed.get("feed", {}).get("link"))


def _find_alternate_feed_href(html: str, base_url: str) -> str | None:
    """Scan an HTML page for the first <link rel="alternate"
    type="application/rss+xml|atom+xml">, resolving a relative href against the
    final URL."""
    try:
        tree = fromstring(html)
    except (etree.ParserError, ValueError):
        return None
    for link in tree.iter("link"):
        rel = (link.get("rel") or "").lower()
        typ = (link.get("type") or "").lower()
        if "alternate" in rel and typ in _FEED_ALTERNATE_TYPES:
            href = link.get("href")
            if href:
                return urljoin(base_url, href)
    return None


async def _fetch_capped(client: httpx.AsyncClient, url: str) -> tuple[str, bytes]:
    """GET with the 10 MB streamed cap. Returns (final_url, body_bytes)."""
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        final_url = str(response.url)
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_FEED_BYTES:
                raise HTTPException(status_code=422, detail="Feed body exceeded 10 MB")
            chunks.append(chunk)
        return final_url, b"".join(chunks)


async def _discover_feed(url: str) -> dict:
    """Resolve a user-supplied feed-or-page URL to a subscribable feed.

    Returns {feed_url, title, site_url}. Raises HTTPException(422) when the URL
    is neither a feed nor a page advertising one.
    """
    parsed_url = urlparse(url)
    if parsed_url.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="URL must be http or https")

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=_DISCOVER_TIMEOUT,
        max_redirects=_DISCOVER_MAX_REDIRECTS,
        headers={"User-Agent": f"Tiro/{__version__}"},
    ) as client:
        try:
            final_url, body = await _fetch_capped(client, url)
        except HTTPException:
            raise
        except httpx.HTTPError as e:
            raise HTTPException(status_code=422, detail=f"Could not fetch URL: {e}") from e

        parsed = feedparser.parse(body)
        if _looks_like_feed(parsed):
            return {
                "feed_url": final_url,
                "title": parsed.feed.get("title") or final_url,
                "site_url": parsed.feed.get("link") or "",
            }

        # Not a feed — scan the HTML for an alternate feed link.
        try:
            html = body.decode("utf-8", errors="replace")
        except Exception:
            html = ""
        href = _find_alternate_feed_href(html, final_url)
        if not href:
            raise HTTPException(
                status_code=422,
                detail="No RSS/Atom feed found at that URL",
            )
        try:
            feed_final_url, feed_body = await _fetch_capped(client, href)
        except HTTPException:
            raise
        except httpx.HTTPError as e:
            raise HTTPException(status_code=422, detail=f"Could not fetch feed: {e}") from e
        feed_parsed = feedparser.parse(feed_body)
        if not _looks_like_feed(feed_parsed):
            raise HTTPException(status_code=422, detail="Discovered link is not a valid feed")
        return {
            "feed_url": feed_final_url,
            "title": feed_parsed.feed.get("title") or feed_final_url,
            "site_url": feed_parsed.feed.get("link") or "",
        }


@router.get("")
async def list_feeds(request: Request):
    """List feeds with status, last-fetch, error info, and article count
    (feed_entries rows that still point at a live article)."""
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute("""
            SELECT f.id, f.uid, f.url, f.title, f.site_url, f.folder, f.source_id,
                   f.fetch_interval_minutes, f.status, f.error_count, f.last_error,
                   f.last_fetched_at, f.created_at,
                   (SELECT COUNT(*) FROM feed_entries fe
                    WHERE fe.feed_id = f.id AND fe.article_id IS NOT NULL) AS article_count
            FROM feeds f
            ORDER BY f.folder IS NULL, f.folder ASC, f.title ASC
        """).fetchall()
        return {"success": True, "data": [dict(r) for r in rows]}
    finally:
        conn.close()


class FeedCreate(BaseModel):
    url: str
    folder: str | None = None
    fetch_interval_minutes: int | None = None


@router.post("")
async def add_feed(body: FeedCreate, request: Request):
    """Subscribe to a feed by URL (with page autodiscovery, §D9). 409
    already_subscribed on a duplicate feed URL."""
    config = request.app.state.config

    discovered = await _discover_feed(body.url.strip())
    feed_url = discovered["feed_url"]

    conn = get_connection(config.db_path)
    try:
        existing = conn.execute(
            "SELECT id, title FROM feeds WHERE url = ?", (feed_url,)
        ).fetchone()
        if existing:
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "error": "already_subscribed",
                    "data": {"id": existing["id"], "title": existing["title"]},
                },
            )

        title = discovered["title"]
        site_url = discovered["site_url"]
        interval = body.fetch_interval_minutes or config.rss_default_interval_minutes
        folder = body.folder.strip() if body.folder and body.folder.strip() else None

        # The feed's source row (source_type='rss') created at subscribe time;
        # entry ingestion forces every article's source to this row.
        feed_id = _insert_feed(
            conn, feed_url=feed_url, title=title, site_url=site_url,
            folder=folder, interval=interval,
        )
        source_id = conn.execute(
            "SELECT source_id FROM feeds WHERE id = ?", (feed_id,)
        ).fetchone()["source_id"]
        conn.commit()
    finally:
        conn.close()

    return {
        "success": True,
        "data": {
            "id": feed_id, "url": feed_url, "title": title,
            "site_url": site_url, "folder": folder,
            "fetch_interval_minutes": interval, "source_id": source_id,
        },
    }


class FeedUpdate(BaseModel):
    title: str | None = None
    folder: str | None = None
    fetch_interval_minutes: int | None = None
    status: str | None = None


@router.patch("/{feed_id}")
async def update_feed(feed_id: int, body: FeedUpdate, request: Request):
    """Update title/folder/interval/status. Setting status to 'active' also
    resets error_count/last_error (the resume path)."""
    config = request.app.state.config
    updates = body.model_dump(exclude_unset=True)

    if "status" in updates and updates["status"] not in ("active", "paused"):
        raise HTTPException(status_code=400, detail="status must be 'active' or 'paused'")

    # Fold-in 3 (T2 fable review): a non-positive interval would make the feed
    # perpetually "due" (a busy-loop poll) — reject it rather than persist it.
    if "fetch_interval_minutes" in updates:
        interval = updates["fetch_interval_minutes"]
        if interval is None or interval <= 0:
            raise HTTPException(
                status_code=400,
                detail="fetch_interval_minutes must be a positive integer",
            )

    conn = get_connection(config.db_path)
    try:
        row = conn.execute("SELECT id FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Feed not found")

        set_parts = []
        values: list = []
        for field in ("title", "folder", "fetch_interval_minutes", "status"):
            if field in updates:
                set_parts.append(f"{field} = ?")
                values.append(updates[field])
        # Resume: PATCH status->active clears the error state.
        if updates.get("status") == "active":
            set_parts.append("error_count = 0")
            set_parts.append("last_error = NULL")

        if set_parts:
            conn.execute(
                f"UPDATE feeds SET {', '.join(set_parts)} WHERE id = ?",
                (*values, feed_id),
            )
            conn.commit()

        updated = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        return {"success": True, "data": dict(updated)}
    finally:
        conn.close()


def _delete_feed(config, feed_id: int, delete_articles: bool) -> int:
    """Sync helper (one asyncio.to_thread call). Returns deleted article count.

    Without delete_articles: drop the feed's ledger rows + the feed row only;
    articles and the source stay. With delete_articles: delete every article
    on the feed's source through the lifecycle coordinator (never raw cascade),
    then drop the ledger + feed row, then the source row if it has no remaining
    articles.
    """
    from tiro.lifecycle import delete_article

    conn = get_connection(config.db_path)
    try:
        feed = conn.execute(
            "SELECT source_id FROM feeds WHERE id = ?", (feed_id,)
        ).fetchone()
        source_id = feed["source_id"] if feed else None
        article_ids = []
        if delete_articles and source_id is not None:
            article_ids = [
                r["id"] for r in conn.execute(
                    "SELECT id FROM articles WHERE source_id = ?", (source_id,)
                ).fetchall()
            ]
    finally:
        conn.close()

    deleted = 0
    for aid in article_ids:
        if delete_article(config, aid):
            deleted += 1

    conn = get_connection(config.db_path)
    try:
        conn.execute("DELETE FROM feed_entries WHERE feed_id = ?", (feed_id,))
        conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
        if delete_articles and source_id is not None:
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM articles WHERE source_id = ?", (source_id,)
            ).fetchone()["n"]
            if remaining == 0:
                conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        conn.commit()
    finally:
        conn.close()

    return deleted


@router.delete("/{feed_id}")
async def delete_feed(feed_id: int, request: Request, delete_articles: bool = False):
    """Unsubscribe. `?delete_articles=true` cascades through the lifecycle
    coordinator (auto_backup first), otherwise keeps articles + source."""
    config = request.app.state.config

    conn = get_connection(config.db_path)
    try:
        exists = conn.execute("SELECT id FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    finally:
        conn.close()
    if not exists:
        raise HTTPException(status_code=404, detail="Feed not found")

    if delete_articles:
        from tiro.backup import auto_backup
        await asyncio.to_thread(auto_backup, config, "feed-delete")

    deleted = await asyncio.to_thread(_delete_feed, config, feed_id, delete_articles)
    return {"success": True, "data": {"deleted_articles": deleted}}


@router.post("/{feed_id}/check")
async def check_feed(feed_id: int, request: Request):
    """Fetch this feed now, ignoring due-ness/paused status (POST — it
    mutates)."""
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        exists = conn.execute("SELECT id FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    finally:
        conn.close()
    if not exists:
        raise HTTPException(status_code=404, detail="Feed not found")

    result = await asyncio.to_thread(check_feeds, config, feed_id)
    return {"success": True, "data": result}


@router.post("/check-all")
async def check_all_feeds(request: Request):
    """Run a full poll cycle now (every active, due feed)."""
    config = request.app.state.config
    result = await asyncio.to_thread(check_feeds, config)
    return {"success": True, "data": result}


@router.post("/import")
async def import_opml(file: UploadFile, request: Request):
    """Import an OPML upload of subscriptions (spec D5).

    Recursively flattens nested outlines into a `folder` label, dedupes by feed
    URL against existing rows, and creates a feed + its rss source row per new
    URL WITHOUT network autodiscovery (the OPML already carries the feed URL;
    the first poll validates it). Feed URLs whose scheme isn't http/https are
    rejected per-row (the SAME allowlist `POST /api/feeds` enforces) and
    reported in `errors`, never aborting the whole import. Returns
    `{added, skipped, errors}`. 400 on an upload over 5 MB or unparseable OPML.
    """
    config = request.app.state.config
    raw = await file.read()
    if len(raw) > _MAX_OPML_BYTES:
        raise HTTPException(status_code=400, detail="OPML upload exceeds 5 MB")

    try:
        parsed = opml.parse_opml(raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Could not parse OPML: {e}") from e

    added = 0
    skipped = 0
    errors: list[str] = []
    conn = get_connection(config.db_path)
    try:
        existing = {r["url"] for r in conn.execute("SELECT url FROM feeds").fetchall()}
        for entry in parsed:
            feed_url = (entry.get("url") or "").strip()
            if not feed_url:
                continue
            if urlparse(feed_url).scheme not in ("http", "https"):
                # Same scheme allowlist as POST /api/feeds — an OPML row can
                # carry a javascript:/file:/data: xmlUrl. Reject the row, keep
                # importing the rest.
                errors.append(f"{feed_url}: URL must be http or https")
                continue
            if feed_url in existing:
                skipped += 1
                continue
            folder = entry.get("folder")
            folder = folder.strip() if folder and folder.strip() else None
            try:
                _insert_feed(
                    conn,
                    feed_url=feed_url,
                    title=entry.get("title") or feed_url,
                    site_url=entry.get("site_url") or "",
                    folder=folder,
                    interval=config.rss_default_interval_minutes,
                )
                conn.commit()
                existing.add(feed_url)
                added += 1
            except Exception as e:  # noqa: BLE001 — one bad row must not abort the import
                conn.rollback()
                logger.warning("OPML import: failed to add %r: %s", feed_url, e)
                errors.append(f"{feed_url}: {e}")
    finally:
        conn.close()

    return {"success": True, "data": {"added": added, "skipped": skipped, "errors": errors}}


@router.get("/export")
async def export_feeds_opml(request: Request):
    """Export subscriptions as a standalone OPML 2.0 document (spec D5): one
    `type="rss"` outline per feed (xmlUrl + htmlUrl), nested one level by
    folder."""
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute(
            "SELECT url, title, site_url, folder FROM feeds "
            "ORDER BY folder IS NULL, folder ASC, title ASC"
        ).fetchall()
    finally:
        conn.close()

    feeds = [
        {"url": r["url"], "title": r["title"], "site_url": r["site_url"], "folder": r["folder"]}
        for r in rows
    ]
    document = opml.build_opml(feeds)
    return Response(
        content=document,
        media_type="text/x-opml+xml",
        headers={"Content-Disposition": 'attachment; filename="tiro-feeds.opml"'},
    )
