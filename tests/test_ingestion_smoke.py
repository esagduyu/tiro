"""Full-pipeline smoke: .eml upload -> markdown + SQLite + ChromaDB.

This is the M1 gate for the ChromaDB readonly-database issue: it proves
collection.add() succeeds inside a running (test) app when the library
was pre-initialized. NOTE: TestClient is not uvicorn — a pass here does
not fully clear the uvicorn threading variant; the pre-init workaround
plus this test are the Phase 0 mitigation (spec, M1).
"""

from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "newsletter.eml"


def test_email_ingestion_writes_all_three_stores(authenticated_client, configured_library):
    raw = FIXTURE.read_bytes()
    r = authenticated_client.post(
        "/api/ingest/email",
        files={"file": ("newsletter.eml", raw, "message/rfc822")},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    article_id = data["id"]

    # SQLite row exists, marked as email ingestion
    from tiro.database import get_connection

    conn = get_connection(configured_library.db_path)
    try:
        row = conn.execute(
            "SELECT title, ingestion_method, markdown_path FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["title"] == "Local-First Software Weekly, Issue 1"
    assert row["ingestion_method"] == "email"

    # Markdown file exists under the ISOLATED library
    md_file = configured_library.articles_dir / row["markdown_path"]
    assert md_file.exists()
    assert "local-first" in md_file.read_text().lower()

    # ChromaDB vector exists (the readonly-bug gate)
    from tiro.vectorstore import get_collection

    got = get_collection().get(ids=[f"article_{article_id}"])
    assert got["ids"] == [f"article_{article_id}"]

    # No API key in tests: AI enrichment must have been skipped gracefully
    assert data["tags"] == []


def test_duplicate_email_rejected_with_409(authenticated_client):
    raw = FIXTURE.read_bytes()
    first = authenticated_client.post(
        "/api/ingest/email",
        files={"file": ("newsletter.eml", raw, "message/rfc822")},
    )
    assert first.status_code == 200
    second = authenticated_client.post(
        "/api/ingest/email",
        files={"file": ("newsletter.eml", raw, "message/rfc822")},
    )
    assert second.status_code == 409
