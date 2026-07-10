"""OPML parse/build round-trip + import/export API (Phase 4 M4.1).

`tiro/opml.py` is pure (bytes<->list of feed dicts); the import/export routes
in `routes_feeds.py` wrap it. Import reuses the subscribe internals to create
the feed + its source row but WITHOUT network autodiscovery (OPML already
carries the feed URL — first poll validates it).
"""

from pathlib import Path

import pytest

from tiro import opml
from tiro.database import get_connection

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"


# --------------------------------------------------------------------------
# Pure parse / build
# --------------------------------------------------------------------------

def test_parse_nested_flattens_folders():
    feeds = opml.parse_opml((FIXTURES / "nested.opml").read_bytes())
    by_url = {f["url"]: f for f in feeds}
    assert set(by_url) == {
        "https://example.com/feed.xml",
        "https://ycombinator.example.com/feed",
        "https://top.example.com/rss",
    }

    top_tech = by_url["https://example.com/feed.xml"]
    assert top_tech["title"] == "Example Blog"
    assert top_tech["site_url"] == "https://example.com/"
    assert top_tech["folder"] == "Tech"

    # Nested two levels deep -> folder path segments joined with "/".
    nested = by_url["https://ycombinator.example.com/feed"]
    assert nested["folder"] == "Tech/Startups"

    # A top-level feed outline has no folder.
    toplevel = by_url["https://top.example.com/rss"]
    assert toplevel["folder"] is None
    assert toplevel["title"] == "Top Level Feed"


def test_build_then_parse_round_trip_preserves_every_feed():
    feeds = [
        {"url": "https://a.example.com/feed", "title": "Feed A",
         "site_url": "https://a.example.com/", "folder": "News"},
        {"url": "https://b.example.com/feed", "title": "Feed B",
         "site_url": "https://b.example.com/", "folder": "News"},
        {"url": "https://c.example.com/feed", "title": "Feed C",
         "site_url": "", "folder": None},
        {"url": "https://d.example.com/feed", "title": "Feed D",
         "site_url": "https://d.example.com/", "folder": "Tech/Deep"},
    ]
    xml = opml.build_opml(feeds)
    assert isinstance(xml, str)

    reparsed = opml.parse_opml(xml.encode("utf-8"))
    got = {f["url"]: f for f in reparsed}
    assert set(got) == {f["url"] for f in feeds}
    for f in feeds:
        r = got[f["url"]]
        assert r["title"] == f["title"]
        assert r["folder"] == f["folder"]


def test_build_opml_marks_feeds_type_rss_and_xmlurl():
    xml = opml.build_opml([
        {"url": "https://a.example.com/feed", "title": "A", "site_url": "https://a.example.com/", "folder": None},
    ])
    assert 'type="rss"' in xml
    assert 'xmlUrl="https://a.example.com/feed"' in xml
    assert 'htmlUrl="https://a.example.com/"' in xml


def test_parse_malformed_raises_valueerror():
    with pytest.raises(ValueError):
        opml.parse_opml((FIXTURES / "hostile.opml").read_bytes())


def test_parse_empty_or_junk_raises_valueerror():
    with pytest.raises(ValueError):
        opml.parse_opml(b"not xml at all")


def test_parse_deeply_nested_does_not_crash():
    # A pathologically deep folder tree must not blow the Python stack (the
    # walk is iterative, not recursive).
    depth = 3000
    opens = "".join(f'<outline text="L{i}">' for i in range(depth))
    closes = "</outline>" * depth
    xml = (
        '<?xml version="1.0"?><opml version="2.0"><head/><body>'
        + opens
        + '<outline type="rss" text="deep" xmlUrl="https://deep.example.com/feed"/>'
        + closes
        + "</body></opml>"
    )
    feeds = opml.parse_opml(xml.encode("utf-8"))
    assert len(feeds) == 1
    assert feeds[0]["url"] == "https://deep.example.com/feed"


def test_parse_ignores_outline_without_xmlurl():
    # Folder-only outlines (no xmlUrl) are structure, not feeds.
    xml = (
        '<?xml version="1.0"?><opml version="2.0"><body>'
        '<outline text="Empty Folder"/>'
        '<outline type="rss" text="Real" xmlUrl="https://real.example.com/feed"/>'
        "</body></opml>"
    )
    feeds = opml.parse_opml(xml.encode("utf-8"))
    assert len(feeds) == 1


# --------------------------------------------------------------------------
# Import / export API
# --------------------------------------------------------------------------

def _feed_urls(config):
    conn = get_connection(config.db_path)
    try:
        return {r["url"] for r in conn.execute("SELECT url FROM feeds").fetchall()}
    finally:
        conn.close()


def test_import_opml_adds_feeds_and_sources(authenticated_client, configured_library):
    data = (FIXTURES / "nested.opml").read_bytes()
    r = authenticated_client.post(
        "/api/feeds/import",
        files={"file": ("subs.opml", data, "text/x-opml+xml")},
    )
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["added"] == 3
    assert body["skipped"] == 0
    assert body["errors"] == []

    assert _feed_urls(configured_library) == {
        "https://example.com/feed.xml",
        "https://ycombinator.example.com/feed",
        "https://top.example.com/rss",
    }
    # Each feed got a source row of type rss.
    conn = get_connection(configured_library.db_path)
    try:
        rss_sources = conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE source_type = 'rss'"
        ).fetchone()["n"]
        # Folder carried onto the feed row.
        yc = conn.execute(
            "SELECT folder FROM feeds WHERE url = ?",
            ("https://ycombinator.example.com/feed",),
        ).fetchone()
    finally:
        conn.close()
    assert rss_sources == 3
    assert yc["folder"] == "Tech/Startups"


def test_import_opml_dedupes_against_existing(authenticated_client, configured_library):
    data = (FIXTURES / "nested.opml").read_bytes()
    first = authenticated_client.post(
        "/api/feeds/import", files={"file": ("subs.opml", data, "text/x-opml+xml")}
    )
    assert first.json()["data"]["added"] == 3

    # Re-import the same file: all three already subscribed -> skipped.
    second = authenticated_client.post(
        "/api/feeds/import", files={"file": ("subs.opml", data, "text/x-opml+xml")}
    )
    body = second.json()["data"]
    assert body["added"] == 0
    assert body["skipped"] == 3
    assert len(_feed_urls(configured_library)) == 3


def test_import_opml_rejects_non_http_scheme_per_row(authenticated_client, configured_library):
    """An OPML xmlUrl with a non-http(s) scheme (javascript:/file:/data:) is
    rejected per-row (same allowlist POST /api/feeds enforces) and reported in
    `errors`, without 400-ing the whole import — the valid row still lands."""
    xml = (
        b'<?xml version="1.0"?><opml version="2.0"><head/><body>'
        b'<outline type="rss" text="Good" xmlUrl="https://good.example.com/feed"/>'
        b'<outline type="rss" text="Evil" xmlUrl="javascript:alert(1)"/>'
        b'<outline type="rss" text="File" xmlUrl="file:///etc/passwd"/>'
        b"</body></opml>"
    )
    r = authenticated_client.post(
        "/api/feeds/import", files={"file": ("mixed.opml", xml, "text/x-opml+xml")}
    )
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["added"] == 1
    assert len(body["errors"]) == 2
    assert all("http or https" in e for e in body["errors"])
    # Only the http(s) feed was persisted.
    assert _feed_urls(configured_library) == {"https://good.example.com/feed"}


def test_import_hostile_opml_returns_400(authenticated_client):
    data = (FIXTURES / "hostile.opml").read_bytes()
    r = authenticated_client.post(
        "/api/feeds/import", files={"file": ("bad.opml", data, "text/x-opml+xml")}
    )
    assert r.status_code == 400


def test_import_oversized_opml_returns_400(authenticated_client):
    big = b"<opml><body>" + b"<!-- pad -->" * (500 * 1024) + b"</body></opml>"
    assert len(big) > 5 * 1024 * 1024
    r = authenticated_client.post(
        "/api/feeds/import", files={"file": ("big.opml", big, "text/x-opml+xml")}
    )
    assert r.status_code == 400


def test_export_opml_round_trips_into_empty_library(authenticated_client, configured_library):
    # Subscribe via import, export, then re-import into the same set -> stable.
    data = (FIXTURES / "nested.opml").read_bytes()
    authenticated_client.post(
        "/api/feeds/import", files={"file": ("subs.opml", data, "text/x-opml+xml")}
    )

    r = authenticated_client.get("/api/feeds/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/x-opml+xml")
    exported = r.content

    parsed = opml.parse_opml(exported)
    got = {f["url"]: f for f in parsed}
    assert set(got) == {
        "https://example.com/feed.xml",
        "https://ycombinator.example.com/feed",
        "https://top.example.com/rss",
    }
    assert got["https://ycombinator.example.com/feed"]["folder"] == "Tech/Startups"
