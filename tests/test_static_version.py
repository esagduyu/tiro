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
    # docs/plans/2026-07-05-m2-0-frontend-modules-plan.md. sidebar.js is
    # deliberately loaded WITHOUT a ?v= query (see the comment above its tag
    # in base.html): it's the one module both entry-loaded from a template
    # and relative-imported by other modules (inbox.js/digest.js), and a
    # mismatched query string there causes the browser to instantiate it
    # twice under native ES module resolution.
    assert '/static/js/sidebar.js"' in resp.text
    assert f"/static/js/inbox.js?v={STATIC_VERSION}" in resp.text
    assert "?v=56" not in resp.text or STATIC_VERSION == "56"
