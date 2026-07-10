"""/feeds management page (Phase 4 M4.1): page render, nav entry, pinned order.

Mirrors test_highlights_views.py's page-auth/template/sidebar pin style. The
route-walk invariant (test_auth.py) auto-covers the anonymous 302; the explicit
redirect test below just pins the exact target.
"""

from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent.parent / "tiro" / "frontend" / "templates"


# --- Page presence -----------------------------------------------------------

def test_feeds_page_redirects_when_anonymous(auth_client):
    r = auth_client.get("/feeds", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_feeds_page_renders_authenticated(authenticated_client):
    r = authenticated_client.get("/feeds")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'id="feeds-root"' in r.text
    assert 'id="feeds-empty"' in r.text
    assert 'id="feed-url-input"' in r.text


def test_feeds_page_loads_feeds_js(authenticated_client):
    from tiro.app import STATIC_VERSION

    r = authenticated_client.get("/feeds")
    assert f"/static/js/feeds.js?v={STATIC_VERSION}" in r.text


def test_feeds_page_marks_nav_feeds_active(authenticated_client):
    r = authenticated_client.get("/feeds")
    assert 'href="/feeds" class="sidebar-item active"' in r.text


# --- Sidebar nav link + pinned order -----------------------------------------

def test_base_html_has_feeds_nav_link(authenticated_client):
    r = authenticated_client.get("/inbox")
    assert 'href="/feeds"' in r.text


def test_base_html_feeds_link_after_sources_order_preserved():
    base_html = (TEMPLATES_DIR / "base.html").read_text()
    wiki_pos = base_html.index('href="/wiki"')
    highlights_pos = base_html.index('href="/highlights"')
    sources_pos = base_html.index('href="/sources"')
    feeds_pos = base_html.index('href="/feeds"')
    graph_pos = base_html.index('href="/graph"')
    # The test-pinned relative order (wiki -> highlights -> sources) survives,
    # with Feeds inserted after Sources and before Graph.
    assert wiki_pos < highlights_pos < sources_pos < feeds_pos < graph_pos


def test_inbox_does_not_mark_nav_feeds_active(authenticated_client):
    r = authenticated_client.get("/inbox")
    assert 'href="/feeds" class="sidebar-item active"' not in r.text


# --- Keyboard: Shift+F navigates to /feeds (n was already taken by "save") ----

def test_inbox_js_binds_feeds_navigation():
    js = (Path(__file__).parent.parent / "tiro" / "frontend" / "static" / "js" / "inbox.js").read_text()
    assert '"/feeds"' in js


def test_reader_js_binds_feeds_navigation():
    js = (Path(__file__).parent.parent / "tiro" / "frontend" / "static" / "js" / "reader.js").read_text()
    assert '"/feeds"' in js
