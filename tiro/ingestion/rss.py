"""RSS/Atom feed polling (Phase 4 M4.0).

`check_feeds(config, feed_id=None)` is a SYNCHRONOUS poll cycle, consumed via
`asyncio.to_thread` from both the scheduler loop and the manual-check routes —
exactly how `check_imap_inbox` is consumed. It owns the HTTP fetch (headers,
timeout, size cap) and never lets feedparser fetch; feedparser is only the
parse boundary for bytes we already have.

Trust posture (spec §7):
- Feed content is untrusted HTML. The primary content path reuses
  `fetch_and_extract_sync` (which sanitizes via the shared web extraction
  core); the feed-provided fallback runs `sanitize_html` BEFORE markdownify
  here — rss.py is an extraction site, so the Phase-0 sanitize invariant
  applies to it directly.
- Hostile feeds (billion-laughs entity expansion, broken encodings, junk
  bytes, huge inline content) degrade to a recorded per-feed error, never a
  crash: every feed's processing is wrapped so one bad feed can't stop the
  cycle or take down the server. We fetch bytes ourselves under a 10 MB cap
  and a 30s timeout so feedparser never faces an unbounded body.

Audit (spec D4): exactly ONE `log_api_call(..., "rss", ...)` line per cycle
(`endpoint="poll"` for a full cycle, `"check"` for a single-feed manual
check) — never per entry or per feed.
"""

import logging
import re
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import httpx
from markdownify import markdownify as md

from tiro import __version__
from tiro.audit import log_api_call
from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.ingestion.processor import process_article
from tiro.ingestion.web import fetch_and_extract_sync
from tiro.migrations import new_ulid
from tiro.sanitize import sanitize_html

logger = logging.getLogger(__name__)

# We own the fetch, so feedparser never faces an unbounded body.
MAX_FEED_BYTES = 10 * 1024 * 1024  # 10 MB
FETCH_TIMEOUT = 30.0  # seconds
MAX_REDIRECTS = 5
BACKOFF_CAP = 5  # per-feed backoff exponent cap (2**5 = 32x the interval)
ERROR_STATUS_THRESHOLD = 5  # error_count at/above this flips status to 'error'
# Fold-in 1b (T2 fable review): each entry triggers a full-page re-fetch, so an
# unbounded entry list is a work-amplification vector. Process at most this many
# entries per feed per cycle, NEWEST-FIRST — a feed dumping thousands of items
# can't stall the cycle; the oldest overflow is picked up on later cycles once
# the newest are ledgered (or simply stays unfetched, which is acceptable for a
# firehose feed).
MAX_ENTRIES_PER_CYCLE = 200


class FeedTooLarge(Exception):
    """The feed body exceeded MAX_FEED_BYTES mid-stream."""


def canonical_url(url: str) -> str:
    """Canonicalize a URL for cross-method dedup: drop the fragment and every
    tracking query param (`utm_*`, `fbclid`) — the same tracking-param family
    the email pipeline strips. Order-preserving for the remaining params.

    Applied at READ time only (spec D3.4b): the incoming feed/import link is
    canonicalized and matched against the stored RAW `articles.url` (via
    `url = ? OR url = ?`), so an RSS item never duplicates an article the user
    already saved manually/via extension/email with a tracked variant of the
    same link. Stored urls are NEVER rewritten to canonical form on write —
    this is a read-time normalization, not a write-time invariant. Task 4/5
    (importers) reuse this same canonical form.
    """
    parsed = urlparse(url)
    kept = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not (k.lower().startswith("utm_") or k.lower() == "fbclid")
    ]
    return urlunparse(parsed._replace(query=urlencode(kept), fragment=""))


def _feed_backoff_interval(fetch_interval_minutes: int, error_count: int) -> int:
    """Effective minutes between polls for a feed, with per-feed exponential
    backoff (independent of the loop-level backoff): a broken feed's window
    grows 2**min(error_count, 5) so it never blocks the cycle or other feeds."""
    return fetch_interval_minutes * (2 ** min(error_count, BACKOFF_CAP))


def _is_due(feed_row, now: datetime) -> bool:
    """A feed is due when it has never been fetched, or `now` has passed its
    last fetch plus the (backoff-scaled) interval."""
    if not feed_row["last_fetched_at"]:
        return True
    try:
        last = datetime.fromisoformat(feed_row["last_fetched_at"])
    except (ValueError, TypeError):
        return True
    interval = _feed_backoff_interval(feed_row["fetch_interval_minutes"], feed_row["error_count"])
    return now >= last + timedelta(minutes=interval)


def _fetch_feed(client: httpx.Client, feed_row) -> tuple[int, bytes, dict]:
    """Conditional GET for one feed. Returns (status_code, body_bytes, headers).

    Sends `If-None-Match`/`If-Modified-Since` from the feed's stored validators
    so a `304 Not Modified` short-circuits the whole parse. Streams the body
    under MAX_FEED_BYTES so a hostile/huge feed can't exhaust memory. This is
    the one small, monkeypatchable HTTP seam the tests stub (offline).
    """
    headers = {}
    if feed_row["last_etag"]:
        headers["If-None-Match"] = feed_row["last_etag"]
    if feed_row["last_modified"]:
        headers["If-Modified-Since"] = feed_row["last_modified"]

    with client.stream("GET", feed_row["url"], headers=headers) as response:
        status = response.status_code
        resp_headers = {k.lower(): v for k, v in response.headers.items()}
        if status == 304:
            return 304, b"", resp_headers
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_FEED_BYTES:
                raise FeedTooLarge(f"feed body exceeded {MAX_FEED_BYTES} bytes")
            chunks.append(chunk)
        return status, b"".join(chunks), resp_headers


def _published_datetime(entry) -> datetime | None:
    """entry.published_parsed (a time.struct_time) → naive datetime, if present."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6])
    except (TypeError, ValueError):
        return None


def _extract_feed_fallback(entry) -> dict | None:
    """Feed-provided content path: `entry.content[0].value` or `entry.summary`,
    run through `sanitize_html` BEFORE markdownify (extraction-site sanitize
    invariant). Returns an extracted dict or None when there's no usable body."""
    html = None
    contents = entry.get("content")
    if contents:
        html = contents[0].get("value")
    if not html:
        html = entry.get("summary")
    if not html:
        return None
    clean = sanitize_html(html)
    content_md = md(clean, heading_style="ATX", bullets="-", wrap=False)
    content_md = re.sub(r"\n{3,}", "\n\n", content_md).strip()
    if not content_md:
        return None
    return {
        "title": entry.get("title") or "Untitled",
        "author": entry.get("author"),
        "content_md": content_md,
        "url": entry.get("link") or "",
    }


def _resolve_entry_content(entry) -> dict | None:
    """Full-page extraction first (feed bodies are routinely truncated), then
    the sanitized feed-provided fallback. None when both fail."""
    link = entry.get("link")
    if link:
        try:
            return fetch_and_extract_sync(link)
        except Exception as e:
            logger.debug("Full-page fetch failed for %s, falling back to feed content: %s", link, e)
    return _extract_feed_fallback(entry)


def _attach_folder_tag(conn, article_id: int, folder: str) -> None:
    """Attach the feed's (flattened OPML) folder as a deterministic lowercase
    tag on the new article — reuses the ensure-tag/link pattern from the
    processor. Best-effort: a tagging hiccup never fails an ingested entry."""
    tag_name = folder.strip().lower()
    if not tag_name:
        return
    conn.execute("INSERT OR IGNORE INTO tags (uid, name) VALUES (?, ?)", (new_ulid(), tag_name))
    tag_row = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()
    conn.execute(
        "INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (?, ?)",
        (article_id, tag_row["id"]),
    )
    conn.commit()


def _record_feed_success(conn, feed_id: int, etag: str | None, last_modified: str | None,
                         now: datetime) -> None:
    """Any successful fetch (200 or 304) resets the error state and updates the
    conditional-GET validators + last_fetched_at. Status returns to 'active'
    only if it was 'error' — a 'paused' feed stays paused (a manual check must
    not silently un-pause it)."""
    conn.execute(
        "UPDATE feeds SET last_fetched_at = ?, error_count = 0, last_error = NULL, "
        "last_etag = ?, last_modified = ?, "
        "status = CASE WHEN status = 'error' THEN 'active' ELSE status END "
        "WHERE id = ?",
        (now.isoformat(), etag, last_modified, feed_id),
    )
    conn.commit()


def _record_feed_error(conn, feed_id: int, error: str, now: datetime) -> None:
    """A per-feed failure: bump error_count, store last_error, advance
    last_fetched_at (so the backoff window actually takes effect), and flip
    status to 'error' once error_count crosses the threshold. A 'paused' feed
    stays paused."""
    conn.execute(
        "UPDATE feeds SET error_count = error_count + 1, last_error = ?, last_fetched_at = ?, "
        "status = CASE WHEN status != 'paused' AND error_count + 1 >= ? THEN 'error' "
        "ELSE status END "
        "WHERE id = ?",
        (error[:500], now.isoformat(), ERROR_STATUS_THRESHOLD, feed_id),
    )
    conn.commit()


def _find_existing_article_by_url(conn, link: str) -> int | None:
    """Cross-method URL dedup (spec D3.4b): match `articles.url` against the raw
    link OR its canonical form. Returns the existing article id, or None."""
    if not link:
        return None
    canon = canonical_url(link)
    row = conn.execute(
        "SELECT id FROM articles WHERE url = ? OR url = ? LIMIT 1", (link, canon)
    ).fetchone()
    return row["id"] if row else None


def _ledger_has(conn, feed_id: int, guid: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM feed_entries WHERE feed_id = ? AND guid = ?", (feed_id, guid)
    ).fetchone() is not None


def _ledger_insert(conn, feed_id: int, guid: str, article_id: int | None) -> None:
    """Insert the dedup-ledger row. A concurrent UNIQUE collision (the final
    guard) is treated as a skip, not an error."""
    import sqlite3

    try:
        conn.execute(
            "INSERT INTO feed_entries (feed_id, guid, article_id) VALUES (?, ?, ?)",
            (feed_id, guid, article_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()


def _process_entry(conn, config: TiroConfig, feed_row, entry, result: dict) -> None:
    """Dedup → content → ingest → ledger for a single entry. Mutates `result`.

    A FAILED entry (both content paths failed) writes NO ledger row so it
    retries next poll; only ingested and URL-skipped entries are ledgered.
    """
    feed_id = feed_row["id"]
    guid = entry.get("id") or entry.get("link")
    if not guid:
        # No stable identity at all — can't ledger it; skip without a row.
        result["skipped"] += 1
        return

    # (a) ledger dedup — already seen this (feed, guid).
    if _ledger_has(conn, feed_id, guid):
        result["skipped"] += 1
        return

    # (b) cross-method canonical-URL dedup — the user already has this article.
    existing_id = _find_existing_article_by_url(conn, entry.get("link"))
    if existing_id is not None:
        _ledger_insert(conn, feed_id, guid, existing_id)
        result["skipped"] += 1
        return

    extracted = _resolve_entry_content(entry)
    if not extracted:
        result["failed"] += 1
        return  # no ledger row — retry next poll

    try:
        article = process_article(
            title=extracted["title"],
            author=extracted.get("author"),
            content_md=extracted["content_md"],
            url=extracted.get("url") or entry.get("link") or "",
            config=config,
            published_at=_published_datetime(entry),
            ingestion_method="rss",
            source_id=feed_row["source_id"],
        )
    except Exception as e:
        logger.error("Failed to ingest RSS entry %r from feed %d: %s", guid, feed_id, e)
        result["failed"] += 1
        result["errors"].append(f"ingest error for {guid!r}: {e}")
        return  # no ledger row — retry next poll

    if feed_row["folder"]:
        try:
            _attach_folder_tag(conn, article["id"], feed_row["folder"])
        except Exception as e:
            logger.error("Folder-tag attach failed for article %d (non-fatal): %s", article["id"], e)

    _ledger_insert(conn, feed_id, guid, article["id"])
    result["ingested"] += 1
    result["articles"].append({"id": article["id"], "title": article["title"]})


def _process_feed(conn, config: TiroConfig, client: httpx.Client, feed_row, result: dict,
                  now: datetime) -> None:
    """Fetch + parse + ingest one feed. All failure modes (fetch error, size
    cap, feedparser blow-up, bozo-with-no-entries) become a recorded per-feed
    error — never a crash that stops the cycle."""
    feed_id = feed_row["id"]
    try:
        status, body, headers = _fetch_feed(client, feed_row)
    except Exception as e:
        logger.warning("Feed fetch failed for %r: %s", feed_row["url"], e)
        _record_feed_error(conn, feed_id, str(e), now)
        result["failed_feeds"] += 1
        result["errors"].append(f"fetch error for feed {feed_id}: {e}")
        return

    if status == 304:
        _record_feed_success(conn, feed_id, feed_row["last_etag"], feed_row["last_modified"], now)
        return

    if status >= 400:
        _record_feed_error(conn, feed_id, f"HTTP {status}", now)
        result["failed_feeds"] += 1
        result["errors"].append(f"HTTP {status} for feed {feed_id}")
        return

    try:
        parsed = feedparser.parse(body)
    except Exception as e:
        logger.warning("feedparser crashed on feed %r: %s", feed_row["url"], e)
        _record_feed_error(conn, feed_id, f"parse error: {e}", now)
        result["failed_feeds"] += 1
        result["errors"].append(f"parse error for feed {feed_id}: {e}")
        return

    entries = parsed.get("entries") or []
    if parsed.get("bozo") and not entries:
        detail = str(parsed.get("bozo_exception") or "malformed feed")
        _record_feed_error(conn, feed_id, detail, now)
        result["failed_feeds"] += 1
        result["errors"].append(f"malformed feed {feed_id}: {detail}")
        return

    # Success: store new validators up front so a mid-loop crash still records
    # the fetch (per-entry ingestion is individually guarded below anyway).
    _record_feed_success(conn, feed_id, headers.get("etag"), headers.get("last-modified"), now)

    # Cap the entries processed this cycle, newest-first (Fold-in 1b).
    if len(entries) > MAX_ENTRIES_PER_CYCLE:
        entries = sorted(
            entries,
            key=lambda e: _published_datetime(e) or datetime.min,
            reverse=True,
        )[:MAX_ENTRIES_PER_CYCLE]

    for entry in entries:
        result["entries_seen"] += 1
        try:
            _process_entry(conn, config, feed_row, entry, result)
        except Exception as e:
            logger.error("Unexpected error processing entry in feed %d: %s", feed_id, e)
            result["failed"] += 1
            result["errors"].append(f"entry error in feed {feed_id}: {e}")


def check_feeds(config: TiroConfig, feed_id: int | None = None) -> dict:
    """Poll due feeds (or one specific feed) and ingest new entries.

    `feed_id=None` polls every active, due feed (the scheduler cycle).
    A specific `feed_id` (manual check) ignores due-ness and paused status but
    still does the conditional GET, dedup, and backoff bookkeeping.

    Returns a summary dict:
        {feeds_checked, feeds_skipped, entries_seen, ingested, skipped, failed,
         failed_feeds, errors, articles}

    Emits exactly ONE audit line for the whole cycle (spec D4).
    """
    start = time.monotonic()
    now = datetime.now()
    result = {
        "feeds_checked": 0,
        "feeds_skipped": 0,
        "entries_seen": 0,
        "ingested": 0,
        "skipped": 0,
        "failed": 0,
        "failed_feeds": 0,
        "errors": [],
        "articles": [],
    }

    conn = get_connection(config.db_path)
    try:
        if feed_id is not None:
            feeds = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchall()
        else:
            feeds = conn.execute("SELECT * FROM feeds").fetchall()

        # Fold-in 4: an empty feeds table (the common case when RSS is enabled
        # but nothing is subscribed yet) must not write a no-op audit line every
        # scheduler cycle. Return before both the fetch client AND the audit
        # log below.
        if not feeds:
            return result

        client = httpx.Client(
            follow_redirects=True,
            timeout=FETCH_TIMEOUT,
            max_redirects=MAX_REDIRECTS,
            headers={"User-Agent": f"Tiro/{__version__}"},
        )
        try:
            for feed_row in feeds:
                # Manual single-feed check ignores due-ness/paused; the full
                # cycle skips inactive or not-yet-due feeds.
                if feed_id is None and (feed_row["status"] != "active" or not _is_due(feed_row, now)):
                    result["feeds_skipped"] += 1
                    continue
                result["feeds_checked"] += 1
                _process_feed(conn, config, client, feed_row, result, now)
        finally:
            client.close()
    finally:
        conn.close()

    endpoint = "check" if feed_id is not None else "poll"
    success = result["failed"] == 0 and result["failed_feeds"] == 0
    error = "; ".join(result["errors"])[:500] if result["errors"] else None
    log_api_call(
        config,
        "rss",
        endpoint=endpoint,
        count=result["ingested"],
        duration_ms=int((time.monotonic() - start) * 1000),
        success=success,
        error=error,
    )
    return result
