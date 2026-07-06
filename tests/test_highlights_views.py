"""Highlights review view UI (M2.2 Task 4): /highlights page + navigation.

Mirrors test_wiki_views.py's page-auth/template/sidebar/JS-structural pin
style. Unauthenticated-redirect coverage for the route itself is also
auto-covered by test_auth.py's route-walk invariant (new page routes are
enrolled automatically); the explicit redirect test below just pins the
exact 302 target the same way test_sources.py's does.
"""

from pathlib import Path

STATIC_DIR = Path(__file__).parent.parent / "tiro" / "frontend" / "static"
TEMPLATES_DIR = Path(__file__).parent.parent / "tiro" / "frontend" / "templates"


# --- Page presence -----------------------------------------------------------


def test_highlights_page_redirects_when_anonymous(auth_client):
    r = auth_client.get("/highlights", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_highlights_page_renders_authenticated(authenticated_client):
    r = authenticated_client.get("/highlights")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'id="highlights-groups"' in r.text
    assert 'id="highlights-empty"' in r.text
    assert 'id="highlights-color-filters"' in r.text
    assert 'id="highlights-source-select"' in r.text
    assert 'id="highlights-load-more"' in r.text


# --- Static asset wiring ------------------------------------------------------


def test_highlights_page_loads_highlights_js(authenticated_client):
    from tiro.app import STATIC_VERSION

    r = authenticated_client.get("/highlights")
    assert f"/static/js/highlights.js?v={STATIC_VERSION}" in r.text


# --- Sidebar nav link ----------------------------------------------------------


def test_base_html_has_highlights_nav_link(authenticated_client):
    r = authenticated_client.get("/inbox")
    assert r.status_code == 200
    assert 'href="/highlights"' in r.text


def test_base_html_highlights_link_between_wiki_and_sources():
    base_html = (TEMPLATES_DIR / "base.html").read_text()
    wiki_pos = base_html.index('href="/wiki"')
    highlights_pos = base_html.index('href="/highlights"')
    sources_pos = base_html.index('href="/sources"')
    assert wiki_pos < highlights_pos < sources_pos


def test_highlights_page_marks_nav_highlights_active(authenticated_client):
    r = authenticated_client.get("/highlights")
    assert r.status_code == 200
    assert 'href="/highlights" class="sidebar-item active"' in r.text


def test_inbox_page_does_not_mark_nav_highlights_active(authenticated_client):
    r = authenticated_client.get("/inbox")
    assert r.status_code == 200
    assert 'href="/highlights" class="sidebar-item active"' not in r.text


# --- highlights.js structural pins --------------------------------------------


def test_highlights_js_defines_expected_functions():
    content = (STATIC_DIR / "js" / "highlights.js").read_text()
    for fn in [
        "function fetchHighlights",
        "function loadSourceOptions",
        "function renderGroups",
        "function highlightRowHtml",
        "function setupFilters",
        "function setupKeyboard",
    ]:
        assert fn in content, f"missing {fn}"
    assert 'import { esc, formatDate } from "./core.js";' in content


def test_highlights_js_never_renders_note_markdown():
    # Constraint from the task brief: note excerpts on this page are plain,
    # escaped text — never run through renderMarkdown/marked/DOMPurify.
    content = (STATIC_DIR / "js" / "highlights.js").read_text()
    assert "renderMarkdown(" not in content
    assert "renderMarkdown }" not in content and "renderMarkdown," not in content
    # The note excerpt is built via esc(truncate(h.note_markdown, ...))
    assert "esc(noteExcerpt)" in content


def test_highlights_js_flash_handoff_uses_sessionstorage_key():
    content = (STATIC_DIR / "js" / "highlights.js").read_text()
    assert 'sessionStorage.setItem("tiro:flash-highlight", uid)' in content


def test_highlights_js_no_cdn_references():
    content = (STATIC_DIR / "js" / "highlights.js").read_text()
    for marker in ("cdn.jsdelivr", "unpkg.com", "cdnjs.", "googleapis.com"):
        assert marker not in content


# --- Keyboard wiring pins ------------------------------------------------------


def test_inbox_js_binds_h_to_highlights():
    content = (STATIC_DIR / "js" / "inbox.js").read_text()
    assert 'case "h":' in content
    assert 'window.location.href = "/highlights";' in content


def test_reader_js_binds_h_to_highlights():
    content = (STATIC_DIR / "js" / "reader.js").read_text()
    assert 'case "h":' in content
    assert 'window.location.href = "/highlights";' in content


def test_reader_js_consumes_flash_handoff_via_existing_flash_helper():
    # M2.2 Task 4: reader.js must reuse T3's flashHighlightRange rather than
    # inventing a second scroll/flash mechanism.
    content = (STATIC_DIR / "js" / "reader.js").read_text()
    assert "function consumeFlashHandoff" in content
    assert 'sessionStorage.getItem("tiro:flash-highlight")' in content
    assert "flashHighlightRange(uid)" in content


def test_sidebar_js_shortcuts_mention_highlights_for_both_views():
    content = (STATIC_DIR / "js" / "sidebar.js").read_text()
    # Both INBOX_SHORTCUTS and READER_SHORTCUTS arrays gained an entry.
    assert content.count('desc: "Go to highlights"') == 2
