"""RSS core (Phase 4 M4.0): canonical URL, conditional GET, two-layer dedup,
per-feed backoff, hostile-feed isolation, sanitize invariant, audit-per-cycle.

Offline throughout: the HTTP fetch seam (`rss._fetch_feed`) and the full-page
extractor (`rss.fetch_and_extract_sync`) are monkeypatched, so no network.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tiro.database import get_connection
from tiro.ingestion import rss
from tiro.migrations import new_ulid

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"


def _read_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _subscribe_feed(config, url, *, folder=None, title="Feed", status="active",
                    interval=60, error_count=0, last_fetched_at=None,
                    last_etag=None, last_modified=None, source_type="rss") -> int:
    """Insert a feeds row + its source row directly (routes not needed here)."""
    conn = get_connection(config.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO sources (name, domain, source_type) VALUES (?, ?, ?)",
            (title, "example.com", source_type),
        )
        source_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO feeds (uid, url, title, folder, source_id, "
            "fetch_interval_minutes, status, error_count, last_fetched_at, "
            "last_etag, last_modified) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_ulid(), url, title, folder, source_id, interval, status,
             error_count, last_fetched_at, last_etag, last_modified),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _feed_row(config, feed_id):
    conn = get_connection(config.db_path)
    try:
        return conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    finally:
        conn.close()


def _articles(config):
    conn = get_connection(config.db_path)
    try:
        return conn.execute(
            "SELECT id, title, url, published_at, ingestion_method, source_id "
            "FROM articles"
        ).fetchall()
    finally:
        conn.close()


def _ledger(config):
    conn = get_connection(config.db_path)
    try:
        return conn.execute("SELECT feed_id, guid, article_id FROM feed_entries").fetchall()
    finally:
        conn.close()


class FakeFetch:
    """Stub for rss._fetch_feed: maps feed URL -> queue of (status, body, headers).

    Records the conditional-GET headers each call WOULD send (derived from the
    feed row's stored validators) so tests can assert the round-trip.
    """

    def __init__(self, responses: dict):
        # responses: {url: (status, body, headers)} or {url: [resp, resp, ...]}
        self._responses = responses
        self.sent_headers: list[dict] = []

    def __call__(self, client, feed_row):
        url = feed_row["url"]
        sent = {}
        if feed_row["last_etag"]:
            sent["If-None-Match"] = feed_row["last_etag"]
        if feed_row["last_modified"]:
            sent["If-Modified-Since"] = feed_row["last_modified"]
        self.sent_headers.append(sent)
        resp = self._responses[url]
        if isinstance(resp, list):
            resp = resp.pop(0)
        return resp


@pytest.fixture
def no_fullpage(monkeypatch):
    """Force the feed-provided-content fallback (offline): the full-page
    extractor always fails, so ingestion uses `entry.content`/`summary`."""
    def _boom(url):
        raise RuntimeError("offline: no full-page fetch")

    monkeypatch.setattr(rss, "fetch_and_extract_sync", _boom)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def test_canonical_url_strips_tracking_and_fragment():
    assert rss.canonical_url(
        "https://x.com/p?utm_source=rss&utm_campaign=feed&id=7#section"
    ) == "https://x.com/p?id=7"
    assert rss.canonical_url("https://x.com/p?fbclid=abc") == "https://x.com/p"
    # No tracking params -> unchanged (minus any fragment).
    assert rss.canonical_url("https://x.com/p?id=7") == "https://x.com/p?id=7"
    assert rss.canonical_url("https://x.com/p#top") == "https://x.com/p"


def test_is_due_backoff_window():
    now = datetime(2026, 7, 9, 12, 0, 0)

    class Row(dict):
        def __getitem__(self, k):
            return self.get(k)

    never = Row(last_fetched_at=None, fetch_interval_minutes=60, error_count=0)
    assert rss._is_due(never, now) is True

    # Fetched 30 min ago, 60-min interval, no errors -> not due.
    recent = Row(last_fetched_at=(now - timedelta(minutes=30)).isoformat(),
                 fetch_interval_minutes=60, error_count=0)
    assert rss._is_due(recent, now) is False

    # Fetched 90 min ago, 60-min interval, no errors -> due.
    stale = Row(last_fetched_at=(now - timedelta(minutes=90)).isoformat(),
                fetch_interval_minutes=60, error_count=0)
    assert rss._is_due(stale, now) is True

    # error_count=2 quadruples the window (60*4=240 min): 90 min ago -> NOT due.
    backed_off = Row(last_fetched_at=(now - timedelta(minutes=90)).isoformat(),
                     fetch_interval_minutes=60, error_count=2)
    assert rss._is_due(backed_off, now) is False


# --------------------------------------------------------------------------
# Conditional GET
# --------------------------------------------------------------------------

def test_200_stores_new_validators_and_ingests(initialized_library, no_fullpage, monkeypatch):
    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url)
    fetch = FakeFetch({url: (200, _read_fixture("valid_rss.xml"),
                             {"etag": '"abc123"', "last-modified": "Wed, 08 Jul 2026 00:00:00 GMT"})})
    monkeypatch.setattr(rss, "_fetch_feed", fetch)

    result = rss.check_feeds(config)

    assert result["feeds_checked"] == 1
    assert result["ingested"] == 2
    arts = _articles(config)
    assert len(arts) == 2
    row = _feed_row(config, 1)
    assert row["last_etag"] == '"abc123"'
    assert row["last_modified"] == "Wed, 08 Jul 2026 00:00:00 GMT"
    assert row["last_fetched_at"] is not None
    assert row["error_count"] == 0


def test_conditional_get_304_short_circuits(initialized_library, no_fullpage, monkeypatch):
    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url, last_fetched_at="2000-01-01T00:00:00",
                    last_etag='"stored-etag"', last_modified="Mon, 01 Jan 2001 00:00:00 GMT")
    fetch = FakeFetch({url: (304, b"", {})})
    monkeypatch.setattr(rss, "_fetch_feed", fetch)

    result = rss.check_feeds(config)

    # The conditional headers were sent from the stored validators.
    assert fetch.sent_headers[0]["If-None-Match"] == '"stored-etag"'
    assert fetch.sent_headers[0]["If-Modified-Since"] == "Mon, 01 Jan 2001 00:00:00 GMT"
    # 304 => no entries processed, no articles.
    assert result["entries_seen"] == 0
    assert result["ingested"] == 0
    assert _articles(config) == []
    # last_fetched_at advanced (so it's not perpetually "due").
    assert _feed_row(config, 1)["last_fetched_at"] != "2000-01-01T00:00:00"


# --------------------------------------------------------------------------
# Dedup — both layers
# --------------------------------------------------------------------------

def test_ledger_dedup_second_poll_ingests_nothing(initialized_library, no_fullpage, monkeypatch):
    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url)
    body = _read_fixture("valid_rss.xml")
    monkeypatch.setattr(rss, "_fetch_feed", FakeFetch({url: [(200, body, {}), (200, body, {})]}))

    first = rss.check_feeds(config, feed_id=1)
    assert first["ingested"] == 2
    # Second poll: same guids already ledgered -> nothing new.
    second = rss.check_feeds(config, feed_id=1)
    assert second["ingested"] == 0
    assert second["skipped"] == 2
    assert len(_articles(config)) == 2
    assert len(_ledger(config)) == 2


def test_ledger_survives_article_deletion_no_resurrection(initialized_library, no_fullpage, monkeypatch):
    """The dedup ledger row survives its article's deletion (article_id nulled
    by lifecycle.delete_article) so the next poll never resurrects it."""
    from tiro.lifecycle import delete_article

    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url)
    body = _read_fixture("valid_rss.xml")
    monkeypatch.setattr(rss, "_fetch_feed", FakeFetch({url: [(200, body, {}), (200, body, {})]}))

    rss.check_feeds(config, feed_id=1)
    arts = _articles(config)
    assert len(arts) == 2

    # Delete one article; its ledger row must survive with article_id NULL.
    delete_article(config, arts[0]["id"])
    led = _ledger(config)
    assert len(led) == 2  # ledger intact
    nulled = [r for r in led if r["article_id"] is None]
    assert len(nulled) == 1

    # Second poll: the deleted article's guid is still ledgered => not resurrected.
    result = rss.check_feeds(config, feed_id=1)
    assert result["ingested"] == 0
    assert len(_articles(config)) == 1  # still just the surviving article


def test_cross_method_canonical_url_dedup(initialized_library, no_fullpage, monkeypatch):
    config = initialized_library
    # Pre-save a manual article at the canonical form of feed item #2's link
    # (feed carries ?utm_source=rss&utm_campaign=feed).
    conn = get_connection(config.db_path)
    try:
        conn.execute("INSERT INTO sources (name, source_type) VALUES ('manual', 'web')")
        conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path, url, ingestion_method)"
            " VALUES (?, 1, 'Pre-saved Gadget', 'presaved', 'presaved.md', ?, 'manual')",
            (new_ulid(), "https://example.com/posts/second-gadget"),
        )
        conn.commit()
    finally:
        conn.close()

    url = "https://example.com/rss"
    feed_id = _subscribe_feed(config, url)
    monkeypatch.setattr(rss, "_fetch_feed",
                        FakeFetch({url: (200, _read_fixture("valid_rss.xml"), {})}))

    result = rss.check_feeds(config, feed_id=feed_id)

    # Item 1 ingested; item 2 skipped as an existing (canonical) URL.
    assert result["ingested"] == 1
    assert result["skipped"] == 1
    # A ledger row was written for the skipped item pointing at the existing article.
    led = _ledger(config)
    skip_rows = [r for r in led if r["guid"] == "example-guid-0002"]
    assert len(skip_rows) == 1
    pre = get_connection(config.db_path)
    try:
        existing_id = pre.execute(
            "SELECT id FROM articles WHERE url = ?", ("https://example.com/posts/second-gadget",)
        ).fetchone()["id"]
    finally:
        pre.close()
    assert skip_rows[0]["article_id"] == existing_id


def test_failed_entry_writes_no_ledger_row(initialized_library, monkeypatch):
    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url)

    # Full-page fetch fails AND the feed body has no usable content -> failed
    # entry, no ledger row (retries next poll). Use a feed whose items have
    # empty descriptions so the fallback yields nothing.
    empty_feed = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
        b'<item><title>No body</title><link>https://example.com/x</link>'
        b'<guid>g-empty</guid></item></channel></rss>'
    )
    monkeypatch.setattr(rss, "fetch_and_extract_sync",
                        lambda u: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(rss, "_fetch_feed", FakeFetch({url: (200, empty_feed, {})}))

    result = rss.check_feeds(config, feed_id=1)
    assert result["failed"] == 1
    assert result["ingested"] == 0
    assert _ledger(config) == []  # nothing ledgered => retried next time


# --------------------------------------------------------------------------
# Per-feed backoff + status
# --------------------------------------------------------------------------

def test_not_due_feed_skipped_in_full_cycle(initialized_library, no_fullpage, monkeypatch):
    config = initialized_library
    url = "https://example.com/rss"
    # Fetched 5 min ago, 60-min interval -> not due.
    _subscribe_feed(config, url, last_fetched_at=datetime.now().isoformat())
    called = {"n": 0}

    def fetch(client, feed_row):
        called["n"] += 1
        return 200, _read_fixture("valid_rss.xml"), {}

    monkeypatch.setattr(rss, "_fetch_feed", fetch)
    result = rss.check_feeds(config)  # full cycle
    assert result["feeds_skipped"] == 1
    assert result["feeds_checked"] == 0
    assert called["n"] == 0  # never fetched


def test_error_status_feed_skipped_but_manual_check_runs(initialized_library, no_fullpage, monkeypatch):
    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url, status="error", error_count=5)
    monkeypatch.setattr(rss, "_fetch_feed",
                        FakeFetch({url: (200, _read_fixture("valid_rss.xml"), {})}))

    # Full cycle skips a non-active feed.
    cycle = rss.check_feeds(config)
    assert cycle["feeds_skipped"] == 1
    assert cycle["feeds_checked"] == 0

    # Manual check ignores status; a success resets error_count and status.
    manual = rss.check_feeds(config, feed_id=1)
    assert manual["feeds_checked"] == 1
    assert manual["ingested"] == 2
    row = _feed_row(config, 1)
    assert row["error_count"] == 0
    assert row["status"] == "active"


def test_fetch_failure_records_feed_error(initialized_library, monkeypatch):
    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url, error_count=4)

    def boom(client, feed_row):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(rss, "_fetch_feed", boom)
    result = rss.check_feeds(config, feed_id=1)
    assert result["failed_feeds"] == 1
    row = _feed_row(config, 1)
    assert row["error_count"] == 5
    assert row["status"] == "error"  # crossed the threshold
    assert "connection refused" in row["last_error"]
    assert row["last_fetched_at"] is not None  # advanced so backoff applies


# --------------------------------------------------------------------------
# Hostile feeds
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fixture", ["billion_laughs.xml", "truncated_junk.xml"])
def test_hostile_feed_recorded_error_no_crash_isolation(initialized_library, no_fullpage,
                                                        monkeypatch, fixture):
    """A hostile feed must never crash the cycle or the server, and must not
    stop other feeds in the same cycle from processing. feedparser is robust
    (it may parse a malformed feed into some entries rather than raising — the
    invariant is no crash + no injection + isolation, not a specific ingest
    count for the bad feed)."""
    config = initialized_library
    bad_url = "https://evil.example.com/rss"
    good_url = "https://example.com/rss"
    _subscribe_feed(config, bad_url, title="Evil")
    good_id = _subscribe_feed(config, good_url, title="Good")

    responses = {
        bad_url: (200, _read_fixture(fixture), {}),
        good_url: (200, _read_fixture("valid_rss.xml"), {}),
    }
    monkeypatch.setattr(rss, "_fetch_feed", FakeFetch(responses))

    # Must not raise; the good feed in the same cycle still processes fully.
    rss.check_feeds(config)
    good_row = _feed_row(config, good_id)
    assert good_row["error_count"] == 0
    good_articles = [a for a in _articles(config) if a["source_id"] == good_row["source_id"]]
    assert len(good_articles) == 2


def test_wrong_encoding_bytes_no_crash(initialized_library, no_fullpage, monkeypatch):
    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url)
    # Latin-1 declared but with a stray high byte; feedparser must degrade.
    bad = b'<?xml version="1.0" encoding="utf-8"?><rss><channel><title>\xff\xfe junk</title></channel></rss>'
    monkeypatch.setattr(rss, "_fetch_feed", FakeFetch({url: (200, bad, {})}))
    result = rss.check_feeds(config, feed_id=1)  # no exception
    assert result["ingested"] == 0


def test_oversized_body_is_a_feed_error(initialized_library, no_fullpage, monkeypatch):
    """_fetch_feed enforces the 10 MB cap; simulate it raising FeedTooLarge."""
    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url)

    def toobig(client, feed_row):
        raise rss.FeedTooLarge("too big")

    monkeypatch.setattr(rss, "_fetch_feed", toobig)
    result = rss.check_feeds(config, feed_id=1)
    assert result["failed_feeds"] == 1
    assert "too big" in _feed_row(config, 1)["last_error"]


# --------------------------------------------------------------------------
# Sanitize invariant + metadata mapping
# --------------------------------------------------------------------------

def test_feed_fallback_content_is_sanitized(initialized_library, no_fullpage, monkeypatch):
    """A <script> in the feed body must never reach the stored markdown."""
    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url)
    monkeypatch.setattr(rss, "_fetch_feed",
                        FakeFetch({url: (200, _read_fixture("valid_rss.xml"), {})}))

    rss.check_feeds(config, feed_id=1)

    # Read the stored markdown for the first item.
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT markdown_path FROM articles WHERE title = 'First Post About Widgets'"
        ).fetchone()
    finally:
        conn.close()
    body = (config.articles_dir / row["markdown_path"]).read_text()
    assert "xss-in-feed-body" not in body
    assert "<script" not in body
    assert "Widgets are wonderful" in body


def test_published_parsed_maps_to_published_at(initialized_library, no_fullpage, monkeypatch):
    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url)
    monkeypatch.setattr(rss, "_fetch_feed",
                        FakeFetch({url: (200, _read_fixture("valid_rss.xml"), {})}))
    rss.check_feeds(config, feed_id=1)

    conn = get_connection(config.db_path)
    try:
        pub = conn.execute(
            "SELECT published_at FROM articles WHERE title = 'First Post About Widgets'"
        ).fetchone()["published_at"]
    finally:
        conn.close()
    assert pub is not None
    assert pub.startswith("2026-07-06")  # from the fixture's pubDate


def test_folder_becomes_deterministic_tag(initialized_library, no_fullpage, monkeypatch):
    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url, folder="Tech News")
    monkeypatch.setattr(rss, "_fetch_feed",
                        FakeFetch({url: (200, _read_fixture("valid_rss.xml"), {})}))
    rss.check_feeds(config, feed_id=1)

    conn = get_connection(config.db_path)
    try:
        tags = {r["name"] for r in conn.execute(
            "SELECT t.name FROM tags t JOIN article_tags at ON t.id = at.tag_id"
        ).fetchall()}
    finally:
        conn.close()
    assert "tech news" in tags  # lowercased, deterministic


def test_source_forced_to_feed_source(initialized_library, no_fullpage, monkeypatch):
    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url, title="My Feed")
    monkeypatch.setattr(rss, "_fetch_feed",
                        FakeFetch({url: (200, _read_fixture("valid_rss.xml"), {})}))
    rss.check_feeds(config, feed_id=1)

    feed_source_id = _feed_row(config, 1)["source_id"]
    for art in _articles(config):
        assert art["source_id"] == feed_source_id
        assert art["ingestion_method"] == "rss"


def test_atom_feed_ingests(initialized_library, no_fullpage, monkeypatch):
    config = initialized_library
    url = "https://atom.example.com/feed"
    _subscribe_feed(config, url)
    monkeypatch.setattr(rss, "_fetch_feed",
                        FakeFetch({url: (200, _read_fixture("valid_atom.xml"), {})}))
    result = rss.check_feeds(config, feed_id=1)
    assert result["ingested"] == 1
    assert _articles(config)[0]["title"] == "Atom Entry One"


# --------------------------------------------------------------------------
# Audit — one line per cycle
# --------------------------------------------------------------------------

def _audit_lines(config, service=None):
    from datetime import date
    path = config.library / "audit" / f"{date.today().isoformat()}.jsonl"
    if not path.exists():
        return []
    lines = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    if service:
        lines = [ln for ln in lines if ln.get("service") == service]
    return lines


def test_exactly_one_audit_line_per_poll_cycle(initialized_library, no_fullpage, monkeypatch):
    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url)
    monkeypatch.setattr(rss, "_fetch_feed",
                        FakeFetch({url: (200, _read_fixture("valid_rss.xml"), {})}))
    rss.check_feeds(config)

    rss_lines = _audit_lines(config, service="rss")
    assert len(rss_lines) == 1
    entry = rss_lines[0]
    assert entry["endpoint"] == "poll"
    assert entry["count"] == 2
    assert entry["success"] is True


def test_manual_check_audits_endpoint_check(initialized_library, no_fullpage, monkeypatch):
    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url)
    monkeypatch.setattr(rss, "_fetch_feed",
                        FakeFetch({url: (200, _read_fixture("valid_rss.xml"), {})}))
    rss.check_feeds(config, feed_id=1)
    rss_lines = _audit_lines(config, service="rss")
    assert len(rss_lines) == 1
    assert rss_lines[0]["endpoint"] == "check"


# --------------------------------------------------------------------------
# Integration: local fixture feed -> poll -> article appears
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Fold-in 1b: MAX_ENTRIES_PER_CYCLE cap (newest-first)
# --------------------------------------------------------------------------

def _many_item_feed(n: int) -> bytes:
    items = []
    for i in range(n):
        # Ascending day so item i is newer than item i-1; the cap must keep the
        # NEWEST entries (highest i).
        day = 1 + i
        items.append(
            f"<item><title>Item {i:03d}</title>"
            f"<link>https://example.com/p/{i}</link>"
            f"<guid>guid-{i:03d}</guid>"
            f"<pubDate>{day:02d} Jul 2026 00:00:00 +0000</pubDate>"
            f"<description><![CDATA[<p>Body number {i} with enough words to ingest.</p>]]></description>"
            "</item>"
        )
    return (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>Many</title>'
        + "".join(items).encode()
        + b"</channel></rss>"
    )


def test_max_entries_per_cycle_keeps_newest(initialized_library, no_fullpage, monkeypatch):
    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url)
    monkeypatch.setattr(rss, "MAX_ENTRIES_PER_CYCLE", 2)
    monkeypatch.setattr(rss, "_fetch_feed", FakeFetch({url: (200, _many_item_feed(5), {})}))

    result = rss.check_feeds(config, feed_id=1)
    # Only the cap's worth of entries ingested (the two newest: item 3 & 4).
    assert result["ingested"] == 2
    titles = {a["title"] for a in _articles(config)}
    assert titles == {"Item 003", "Item 004"}


# --------------------------------------------------------------------------
# Fold-in 2: real _fetch_feed via httpx.MockTransport (closes the FakeFetch
# seam-fidelity gap — exercises header construction, 304, and the stream cap).
# --------------------------------------------------------------------------

def test_real_fetch_feed_conditional_headers_and_304():
    import httpx

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["inm"] = request.headers.get("if-none-match")
        seen["ims"] = request.headers.get("if-modified-since")
        return httpx.Response(304, headers={"ETag": '"new"'})

    row = {
        "url": "https://example.com/rss",
        "last_etag": '"stored"',
        "last_modified": "Mon, 01 Jan 2001 00:00:00 GMT",
    }
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        status, body, headers = rss._fetch_feed(client, row)

    assert status == 304
    assert body == b""
    assert seen["inm"] == '"stored"'
    assert seen["ims"] == "Mon, 01 Jan 2001 00:00:00 GMT"


def test_real_fetch_feed_streams_200_body():
    import httpx

    payload = b"<rss>ok</rss>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"ETag": '"e"'})

    row = {"url": "https://example.com/rss", "last_etag": None, "last_modified": None}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        status, body, headers = rss._fetch_feed(client, row)
    assert status == 200
    assert body == payload
    assert headers["etag"] == '"e"'


def test_real_fetch_feed_enforces_size_cap(monkeypatch):
    import httpx

    monkeypatch.setattr(rss, "MAX_FEED_BYTES", 16)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 4096)

    row = {"url": "https://example.com/rss", "last_etag": None, "last_modified": None}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(rss.FeedTooLarge):
            rss._fetch_feed(client, row)


# --------------------------------------------------------------------------
# Fold-in 4: no audit line when there are zero feeds (a scheduler cycle with
# an empty feeds table must not write ~24 no-op JSONL lines/day).
# --------------------------------------------------------------------------

def test_no_audit_line_when_feeds_table_empty(initialized_library):
    config = initialized_library
    result = rss.check_feeds(config)  # zero feeds subscribed
    assert result["feeds_checked"] == 0
    assert _audit_lines(config, service="rss") == []


def test_integration_poll_produces_article(initialized_library, no_fullpage, monkeypatch):
    config = initialized_library
    url = "https://example.com/rss"
    _subscribe_feed(config, url, title="Integration Feed")
    monkeypatch.setattr(rss, "_fetch_feed",
                        FakeFetch({url: (200, _read_fixture("valid_rss.xml"), {})}))

    result = rss.check_feeds(config)
    assert result["ingested"] == 2
    arts = _articles(config)
    titles = {a["title"] for a in arts}
    assert "First Post About Widgets" in titles
    assert "Second Post About Gadgets" in titles
    # Correct source + publish date wired through.
    for a in arts:
        assert a["ingestion_method"] == "rss"
        assert a["published_at"] is not None
