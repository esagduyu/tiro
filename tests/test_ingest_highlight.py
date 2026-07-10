"""`POST /api/ingest/url` optional `highlight_text` (spec D10 / M4.3).

The Chrome extension's "Save with selection as highlight" rides a backward-
compatible optional field on the ingest request: after a successful ingest the
server anchors the selected text against the freshly-written markdown body via
the SAME D7.4 helper the importer uses (`reconcile_anchor` search -> `make_anchor`
-> sidecar-first `append_highlight`), soft-failing (no highlight, still 200) when
the quote can't be located. The response gains `highlight_created: bool` only when
the field was provided.
"""

import pytest

from tiro.database import get_connection

CONTENT_MD = (
    "# On Foxes\n\n"
    "The quick brown fox jumps over the lazy dog. "
    "This is a second sentence with more prose to anchor against.\n\n"
    "A closing paragraph rounds out the article body.\n"
)


@pytest.fixture
def stub_fetch(monkeypatch):
    """Replace the network fetch with a fixed extraction whose markdown body we
    control (so a chosen quote is a genuine substring and anchors exact)."""

    async def _fake_fetch(url):
        return {
            "title": "On Foxes",
            "author": "A. Writer",
            "content_md": CONTENT_MD,
            "url": url,
        }

    from tiro.api import routes_ingest

    monkeypatch.setattr(routes_ingest, "fetch_and_extract", _fake_fetch)


def _highlight_count(config, article_id):
    conn = get_connection(config.db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM highlights WHERE article_id = ?", (article_id,)
        ).fetchone()["n"]
    finally:
        conn.close()


def test_highlight_text_absent_keeps_response_shape(authenticated_client, configured_library, stub_fetch):
    r = authenticated_client.post(
        "/api/ingest/url", json={"url": "https://example.com/a"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    # Absent field -> no highlight_created key, unchanged shape, no highlights.
    assert "highlight_created" not in body
    assert _highlight_count(configured_library, body["data"]["id"]) == 0


def test_highlight_text_anchorable_creates_highlight(authenticated_client, configured_library, stub_fetch):
    r = authenticated_client.post(
        "/api/ingest/url",
        json={"url": "https://example.com/b", "highlight_text": "quick brown fox"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["highlight_created"] is True
    aid = body["data"]["id"]
    assert _highlight_count(configured_library, aid) == 1

    # The stored highlight is a real anchor round-tripping through the annotations API.
    ann = authenticated_client.get(f"/api/articles/{aid}/annotations").json()
    hls = ann["data"]["highlights"]
    assert len(hls) == 1
    assert hls[0]["quote_text"] == "quick brown fox"
    assert hls[0]["anchor_status"]["status"] in ("exact", "shifted")


def test_highlight_text_unanchorable_soft_fails(authenticated_client, configured_library, stub_fetch):
    r = authenticated_client.post(
        "/api/ingest/url",
        json={
            "url": "https://example.com/c",
            "highlight_text": "a phrase that is definitely nowhere in the body",
        },
    )
    # Soft-fail: still 200, article saved, no highlight, highlight_created False.
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["highlight_created"] is False
    assert _highlight_count(configured_library, body["data"]["id"]) == 0


def test_highlight_creation_exception_never_500s(
    authenticated_client, configured_library, stub_fetch, monkeypatch
):
    """An unexpected exception anywhere under the highlight-anchoring path must
    not turn an already-saved article into a 500: the route logs it and reports
    highlight_created False on the 200 (M4.3 T6 fold-in)."""
    from tiro.api import routes_ingest

    def _boom(*a, **k):
        raise RuntimeError("anchoring blew up")

    monkeypatch.setattr(routes_ingest, "create_highlight_from_quote", _boom)

    r = authenticated_client.post(
        "/api/ingest/url",
        json={"url": "https://example.com/boom", "highlight_text": "quick brown fox"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["highlight_created"] is False
    # Article itself still saved.
    assert _highlight_count(configured_library, body["data"]["id"]) == 0


def test_highlight_text_ignored_on_duplicate_409(authenticated_client, configured_library, stub_fetch):
    first = authenticated_client.post(
        "/api/ingest/url", json={"url": "https://example.com/d"}
    )
    assert first.status_code == 200
    aid = first.json()["data"]["id"]
    assert _highlight_count(configured_library, aid) == 0

    # Re-saving the same URL WITH a highlight must 409 and add NO highlight.
    dup = authenticated_client.post(
        "/api/ingest/url",
        json={"url": "https://example.com/d", "highlight_text": "quick brown fox"},
    )
    assert dup.status_code == 409
    dbody = dup.json()
    assert dbody["error"] == "already_saved"
    assert "highlight_created" not in dbody
    assert _highlight_count(configured_library, aid) == 0
