"""Server-side sanitization: HTML at ingestion, markdown for AI output."""

from tiro.sanitize import sanitize_html, sanitize_markdown


def test_script_and_iframe_stripped():
    html = '<p>hi</p><script>alert(1)</script><iframe src="//evil"></iframe>'
    out = sanitize_html(html)
    assert "<script" not in out and "<iframe" not in out
    assert "hi" in out


def test_event_handlers_stripped():
    out = sanitize_html('<img src="x.png" onerror="alert(1)"><a href="/" onclick="x()">l</a>')
    assert "onerror" not in out and "onclick" not in out


def test_javascript_urls_stripped():
    out = sanitize_html('<a href="javascript:alert(1)">c</a>')
    assert "javascript:" not in out


def test_images_survive_with_attributes():
    html = '<figure><img src="https://cdn.example/i.jpg" alt="pic" width="640" height="480"><figcaption>cap</figcaption></figure>'
    out = sanitize_html(html)
    assert 'src="https://cdn.example/i.jpg"' in out
    assert 'alt="pic"' in out and 'width="640"' in out
    assert "figcaption" in out


def test_formatting_survives():
    html = "<h2>t</h2><ul><li>a</li></ul><table><tr><td>c</td></tr></table><pre><code>x</code></pre><blockquote>q</blockquote>"
    out = sanitize_html(html)
    for tag in ("<h2>", "<li>", "<td>", "<code>", "<blockquote>"):
        assert tag in out


def test_sanitize_markdown_surgical():
    md = "# Title\n\nReal *markdown* [ok](https://x.y) stays.\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n\n<em>inline html untouched</em>"
    out = sanitize_markdown(md)
    assert "<script" not in out
    assert "javascript:" not in out
    assert "[bad](#)" in out
    assert "Real *markdown* [ok](https://x.y) stays." in out
    assert "<em>inline html untouched</em>" in out  # surgical, not a full HTML pass


def test_ingested_email_is_sanitized(authenticated_client, configured_library):
    from pathlib import Path

    eml = Path(__file__).parent / "fixtures" / "hostile.eml"
    r = authenticated_client.post(
        "/api/ingest/email",
        files={"file": ("hostile.eml", eml.read_bytes(), "message/rfc822")},
    )
    assert r.status_code == 200, r.text
    md_path = None
    from tiro.database import get_connection

    conn = get_connection(configured_library.db_path)
    try:
        row = conn.execute(
            "SELECT markdown_path FROM articles WHERE id = ?", (r.json()["data"]["id"],)
        ).fetchone()
    finally:
        conn.close()
    saved = (configured_library.articles_dir / row["markdown_path"]).read_text()
    assert "<script" not in saved
    assert "onerror" not in saved
    assert "javascript:" not in saved
    assert "Legitimate paragraph content" in saved
