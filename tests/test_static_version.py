"""Cache-busting must have exactly one source of truth: app.STATIC_VERSION."""

import re
from pathlib import Path

TEMPLATES = Path(__file__).parent.parent / "tiro" / "frontend" / "templates"


def test_no_literal_version_numbers_in_templates():
    offenders = []
    for tpl in TEMPLATES.glob("*.html"):
        for m in re.finditer(r"\?v=(?!\{\{)", tpl.read_text()):
            offenders.append(f"{tpl.name}:{m.start()}")
    assert not offenders, f"literal ?v= (not ?v={{{{ static_v }}}}) found: {offenders}"


def test_pages_render_with_current_version(authenticated_client):
    from tiro.app import STATIC_VERSION

    resp = authenticated_client.get("/inbox")
    assert resp.status_code == 200
    # app.js was split into js/sidebar.js (base.html, every page) + js/inbox.js
    # (inbox.html only) in M2.0 Task 2 — see
    # docs/plans/2026-07-05-m2-0-frontend-modules-plan.md. sidebar.js's entry
    # tag carries the normal ?v= query (restored in T5 closeout): the earlier
    # bare-URL workaround for the double-instantiation bug (a versioned tag
    # + an unversioned relative `import ... from "./sidebar.js"` were two
    # distinct module identities) was replaced by an import map in
    # base.html that rewrites the resolved "/static/js/sidebar.js" and
    # "/static/js/core.js" specifiers to the SAME versioned URL the entry
    # tag/other importers resolve to — see base.html's importmap comment and
    # .superpowers/sdd/task-5-report.md for the full reasoning.
    assert f"/static/js/sidebar.js?v={STATIC_VERSION}" in resp.text
    assert f"/static/js/inbox.js?v={STATIC_VERSION}" in resp.text
    # Import map pins: all three mapped specifiers must resolve to the
    # current STATIC_VERSION, so nobody can silently remove or desync the map
    # (which would resurrect the double-instantiation bug, or — for
    # annotate.js, added in M2.2 as reader.js's relative-imported
    # markdown<->plain-text projection core — reintroduce the cache-bust gap
    # a stale unversioned import would leave open).
    assert 'type="importmap"' in resp.text
    assert f'"/static/js/core.js": "/static/js/core.js?v={STATIC_VERSION}"' in resp.text
    assert f'"/static/js/icons.js": "/static/js/icons.js?v={STATIC_VERSION}"' in resp.text
    assert f'"/static/js/sidebar.js": "/static/js/sidebar.js?v={STATIC_VERSION}"' in resp.text
    assert f'"/static/js/annotate.js": "/static/js/annotate.js?v={STATIC_VERSION}"' in resp.text
    assert "?v=56" not in resp.text or STATIC_VERSION == "56"
