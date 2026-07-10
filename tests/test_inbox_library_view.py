"""Owner UX wave 1: unread-first inbox + Library view, reading progress bar,
and the feeds `javascript:`-scheme href guard.

Structural pins in the same no-browser style as test_triage_pill.py /
test_swipe_undo_ui.py — grep the static JS/template source for the DOM hooks
and wiring so the features can't silently regress. Full interaction is left to
Playwright / manual visual verification.
"""

from pathlib import Path

STATIC_DIR = Path(__file__).parent.parent / "tiro" / "frontend" / "static"
TEMPLATES_DIR = Path(__file__).parent.parent / "tiro" / "frontend" / "templates"


# --- Unread-first default + Library view -------------------------------------


def test_inbox_default_fetch_is_unread_only():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    # The default list fetch pins is_read=false unless in Library view / an
    # explicit is_read filter is active.
    assert 'params.set("is_read", "false")' in content
    assert "libraryView" in content


def test_inbox_has_library_toggle_and_function():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "function toggleLibrary" in content
    assert "function setLibraryView" in content
    html = (TEMPLATES_DIR / "inbox.html").read_text()
    assert 'id="library-toggle"' in html


def test_library_view_is_linkable_via_url_param():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    # Written into the URL on sync, and restored from it on load.
    assert 'params.set("view", "library")' in content
    assert 'params.get("view") === "library"' in content


def test_inbox_zero_state_offers_library_link():
    html = (TEMPLATES_DIR / "inbox.html").read_text()
    assert 'id="inbox-zero-library-btn"' in html
    assert "/inbox?view=library" in html
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert "inbox-zero-library-btn" in content


def test_default_triage_view_excludes_library():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    # The inbox-zero celebration must not fire while browsing the library.
    assert "!libraryView" in content


def test_inbox_page_renders_library_toggle(authenticated_client):
    r = authenticated_client.get("/inbox")
    assert r.status_code == 200
    assert 'id="library-toggle"' in r.text
    assert 'id="inbox-zero-library-btn"' in r.text


def test_a_key_toggles_library():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert 'case "a":' in content


# --- Reading progress bar ----------------------------------------------------


def test_reader_has_reading_progress_markup():
    html = (TEMPLATES_DIR / "reader.html").read_text()
    assert 'id="reading-progress"' in html
    assert 'id="reading-progress-bar"' in html


def test_reader_wires_reading_progress():
    content = (STATIC_DIR / "js" / "reader.js").read_text()
    assert "computeReadingProgress" in content
    assert "setupReadingProgress" in content
    # Pure core lives in its own node-tested module.
    assert (STATIC_DIR / "js" / "reading-progress.js").exists()


def test_reading_progress_uses_accent_color():
    css = (STATIC_DIR / "styles.css").read_text()
    assert ".reading-progress-bar" in css
    assert "var(--tiro-accent)" in css


def test_reader_page_renders_progress_bar(authenticated_client):
    # The /articles/{id} page route renders the reader shell regardless of
    # whether the article exists (the JS fetches + handles a 404 client-side),
    # so a bare id is enough to assert the progress-bar markup ships.
    r = authenticated_client.get("/articles/1")
    assert r.status_code == 200
    assert 'id="reading-progress"' in r.text


# --- Feeds javascript:-scheme href guard -------------------------------------


def test_feeds_guards_site_url_with_is_safe_href():
    content = (STATIC_DIR / "js" / "feeds.js").read_text()
    assert "isSafeHref" in content
    assert "isSafeHref(f.site_url)" in content


def test_core_exports_is_safe_href():
    content = (STATIC_DIR / "js" / "core.js").read_text()
    assert "export function isSafeHref" in content
