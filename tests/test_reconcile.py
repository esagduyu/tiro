"""Sync S1: local reconcile engine (write-path plumbing + reconcile_library)."""
import frontmatter

from tiro.anchors import content_hash
from tiro.database import get_connection
from tiro.ingestion.processor import process_article


def _ingest(config, title="Hello World", body="# Hello\n\nSome body text.",
            url="https://example.com/hello"):
    """Ingest one article offline (conftest blocks external APIs;
    extract_metadata degrades to empty defaults with no key)."""
    return process_article(
        title=title, author="A. Writer", content_md=body, url=url, config=config,
    )


def _row(config, article_id, cols="*"):
    conn = get_connection(config.db_path)
    try:
        return conn.execute(
            f"SELECT {cols} FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
    finally:
        conn.close()


def _clear_meta_ts(config, article_id):
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            "UPDATE articles SET meta_updated_at = NULL WHERE id = ?", (article_id,)
        )
        conn.commit()
    finally:
        conn.close()


class TestWritePathPlumbing:
    def test_ingest_stamps_body_hash(self, initialized_library):
        art = _ingest(initialized_library)
        row = _row(initialized_library, art["id"], "body_hash, markdown_path")
        on_disk = frontmatter.load(
            str(initialized_library.articles_dir / row["markdown_path"])
        ).content
        assert row["body_hash"] == content_hash(on_disk)

    def test_meta_routes_bump_meta_updated_at(self, configured_library, authenticated_client):
        art = _ingest(configured_library)
        aid = art["id"]
        cases = [
            ("rate set", "patch", f"/api/articles/{aid}/rate", {"rating": 1}),
            ("rate clear", "patch", f"/api/articles/{aid}/rate", {"rating": None}),
            ("read mark", "patch", f"/api/articles/{aid}/read", {"is_read": True}),
            ("read unmark", "patch", f"/api/articles/{aid}/read", {"is_read": False}),
            ("snooze preset", "patch", f"/api/articles/{aid}/snooze", {"preset": "tomorrow"}),
            ("unsnooze", "patch", f"/api/articles/{aid}/snooze", {}),
        ]
        for label, method, path, body in cases:
            _clear_meta_ts(configured_library, aid)
            resp = getattr(authenticated_client, method)(path, json=body)
            assert resp.status_code == 200, f"{label}: {resp.text}"
            row = _row(configured_library, aid, "meta_updated_at")
            assert row["meta_updated_at"], f"{label} did not bump meta_updated_at"
            assert row["meta_updated_at"].endswith("Z")
