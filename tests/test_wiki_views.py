"""Wiki views UI (Task 9): /wiki list page, /wiki/{slug} page view.

Unauthenticated-redirect coverage lives in test_smoke.py (route-list pattern);
this file covers authenticated 200s + template/JS pins so a future refactor
can't silently drop an id another script depends on.
"""

from pathlib import Path

STATIC_DIR = Path(__file__).parent.parent / "tiro" / "frontend" / "static"
TEMPLATES_DIR = Path(__file__).parent.parent / "tiro" / "frontend" / "templates"


# --- Page presence -----------------------------------------------------------


def test_wiki_list_page_renders_authenticated(authenticated_client):
    r = authenticated_client.get("/wiki")
    assert r.status_code == 200
    assert 'id="wiki-section-entity"' in r.text
    assert 'id="wiki-section-concept"' in r.text
    assert 'id="wiki-tbody-entity"' in r.text
    assert 'id="wiki-tbody-concept"' in r.text
    assert 'id="wiki-empty"' in r.text


def test_wiki_page_view_renders_with_slug_authenticated(authenticated_client):
    r = authenticated_client.get("/wiki/entities/anthropic")
    assert r.status_code == 200
    # Slug flows to the JS via data-slug, same pattern as reader.html's
    # data-article-id.
    assert 'data-slug="entities/anthropic"' in r.text
    assert 'id="wiki-regenerate-btn"' in r.text
    assert 'id="wiki-page-body"' in r.text


def test_wiki_page_view_handles_nested_slug_path(authenticated_client):
    # {slug:path} must accept multi-segment slugs (kind prefix + name).
    r = authenticated_client.get("/wiki/concepts/context-engineering")
    assert r.status_code == 200
    assert 'data-slug="concepts/context-engineering"' in r.text


# --- Static asset wiring ------------------------------------------------------


def test_wiki_list_page_loads_wiki_js(authenticated_client):
    from tiro.app import STATIC_VERSION

    r = authenticated_client.get("/wiki")
    assert f"/static/js/wiki.js?v={STATIC_VERSION}" in r.text


def test_wiki_page_view_loads_wiki_js(authenticated_client):
    from tiro.app import STATIC_VERSION

    r = authenticated_client.get("/wiki/entities/anthropic")
    assert f"/static/js/wiki.js?v={STATIC_VERSION}" in r.text


def test_static_version_is_59():
    from tiro.app import STATIC_VERSION

    assert STATIC_VERSION == "59"


# --- Sidebar nav link ----------------------------------------------------------


def test_base_html_has_wiki_nav_link(authenticated_client):
    r = authenticated_client.get("/inbox")
    assert r.status_code == 200
    assert 'href="/wiki"' in r.text


def test_base_html_wiki_link_between_graph_and_sources():
    base_html = (TEMPLATES_DIR / "base.html").read_text()
    graph_pos = base_html.index('href="/graph"')
    wiki_pos = base_html.index('href="/wiki"')
    sources_pos = base_html.index('href="/sources"')
    assert graph_pos < wiki_pos < sources_pos


def test_wiki_page_marks_nav_wiki_active(authenticated_client):
    r = authenticated_client.get("/wiki")
    assert r.status_code == 200
    assert 'href="/wiki" class="sidebar-item active"' in r.text


def test_inbox_page_does_not_mark_nav_wiki_active(authenticated_client):
    r = authenticated_client.get("/inbox")
    assert r.status_code == 200
    assert 'href="/wiki" class="sidebar-item active"' not in r.text


# --- wiki.js structural pins ---------------------------------------------------


def test_wiki_js_defines_expected_functions():
    content = (STATIC_DIR / "js" / "wiki.js").read_text()
    for fn in [
        "function loadWikiList",
        "function loadWikiPage",
        "export function resolveWikilinks",
        "export function escapeMarkdownLinkText",
        "function doWikiRegenerate",
    ]:
        assert fn in content, f"missing {fn}"
    # renderMarkdown is no longer defined locally (M2.0 Task 4: migrated to
    # the shared core.js implementation, verified byte-identical) — pin the
    # import instead of a local function definition.
    assert 'import { esc, num, renderMarkdown, timeAgo } from "./core.js";' in content


def test_wiki_js_regenerate_uses_textcontent_for_server_error():
    # Server-provided `detail` strings (including 422s from
    # WikiGenerationError) must never be assigned via innerHTML.
    content = (STATIC_DIR / "js" / "wiki.js").read_text()
    assert "errEl.textContent = (json && json.detail)" in content
    assert "errEl.innerHTML" not in content


def test_wiki_js_no_cdn_references():
    content = (STATIC_DIR / "js" / "wiki.js").read_text()
    for marker in ("cdn.jsdelivr", "unpkg.com", "cdnjs.", "googleapis.com"):
        assert marker not in content
