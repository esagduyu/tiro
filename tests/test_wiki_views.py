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


def test_static_version_is_71():
    from tiro.app import STATIC_VERSION

    # Bumped 59 -> 60 in M2.0 Task 5 (frontend module closeout), then
    # 60 -> 61 in M2.2 Task 5 (reader annotation UI closeout: reader.js/
    # highlights.js + the annotate.js core landed under the old "60" pin),
    # then 61 -> 62 in M2.3 Task 2 (reader telemetry tracker added to
    # reader.js), then 62 -> 63 in M3.0 Task 4 (LAN-over-HTTP warning
    # banner: sidebar.js/base.html/styles.css), then 63 -> 64 in M3.1
    # Task 5 (PWA + remote-wizard closeout), then 64 -> 65 in M3.2 Task 5
    # (swipe-triage closeout: swipe.js/undo.js/inbox.js wiring, triage
    # pill, inbox-zero, logout SW-cache hardening), then 65 -> 66 in the
    # design-pass Task 11 closeout sweep (glyph sweep: graph.html node-panel
    # close button, base.html LAN-banner dismiss, reader.html analysis/
    # highlights panel close buttons all switched from literal &times; to
    # the icons.js/_icons.html "close" glyph; orphaned .shortcuts-close and
    # .graph-node-panel-close CSS removed), then 66 -> 67 in the Phase 4
    # (0.6.0 feeds-beta) closeout — the one static bump for the whole phase
    # (feeds.js/import UI, reader progress ResizeObserver, extension) — see
    # tests/test_static_version.py for the import-map pin that owns the
    # details of what changed at the 60 bump specifically. Then 67 -> 68 in
    # Phase 5 M5.0 Task 2 (legacy-library-path suggestion banner:
    # sidebar.js setupLibmoveBanner + styles.css .libmove-banner). Then
    # 68 -> 69 in Phase 6 K2.5 (/agents page: agents.html/agents.js,
    # base.html sidebar + Library-sheet nav entries). Then 69 -> 70 in
    # Phase 6 K3.8 (suggestion chips + /agents queue & persona management).
    # Then 70 -> 71 in sync S5.8 (sync settings card + sidebar status dot:
    # settings.html/base.html/sidebar.js/styles.css).
    assert STATIC_VERSION == "71"


# --- Sidebar nav link ----------------------------------------------------------


def test_base_html_has_wiki_nav_link(authenticated_client):
    r = authenticated_client.get("/inbox")
    assert r.status_code == 200
    assert 'href="/wiki"' in r.text


def test_base_html_library_nav_order():
    # Design pass (Task 3) reordered the Library section to
    # Wiki -> Highlights -> Sources -> Graph -> Stats (spec §6). Wiki now
    # leads the section and Graph follows Sources.
    base_html = (TEMPLATES_DIR / "base.html").read_text()
    wiki_pos = base_html.index('href="/wiki"')
    sources_pos = base_html.index('href="/sources"')
    graph_pos = base_html.index('href="/graph"')
    assert wiki_pos < sources_pos < graph_pos


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
