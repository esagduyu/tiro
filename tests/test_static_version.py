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
    assert f"/static/app.js?v={STATIC_VERSION}" in resp.text
    assert "?v=56" not in resp.text or STATIC_VERSION == "56"
