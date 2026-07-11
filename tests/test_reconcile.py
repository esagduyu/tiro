"""Sync S1: local reconcile engine (write-path plumbing + reconcile_library)."""
from pathlib import Path

import frontmatter
import pytest

from tiro.anchors import content_hash
from tiro.database import get_connection
from tiro.ingestion.processor import process_article
from tiro.sync import reconcile as rec
from tiro.sync.reconcile import ReconcileReport, is_conflict_file, reconcile_library


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


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch):
    monkeypatch.setattr(rec, "SETTLE_SECONDS", 0.0)


def _article_path(config, art):
    return config.articles_dir / art["markdown_path"]


def _edit_body(config, art, new_body):
    """External-editor simulation: rewrite the body, preserve frontmatter
    (including fields Tiro doesn't know about)."""
    path = _article_path(config, art)
    post = frontmatter.load(str(path))
    post.content = new_body
    path.write_text(frontmatter.dumps(post))
    return path


class TestScanAndChanged:
    def test_noop_pass_reports_nothing(self, initialized_library):
        _ingest(initialized_library)
        report = reconcile_library(initialized_library)
        assert isinstance(report, ReconcileReport)
        assert (report.changed, report.ingested, report.deleted,
                report.skipped_unsettled) == (0, 0, 0, 0)

    def test_conflict_file_names(self):
        assert is_conflict_file("2026-07-10_x.conflict-local-20260710.md")
        assert is_conflict_file("2026-07-10_x.conflict-local-20260710-2.md")
        assert not is_conflict_file("2026-07-10_x.md")
        assert not is_conflict_file("notes-about-conflict.md")

    def test_changed_body_updates_row_and_marks_pending(self, initialized_library):
        art = _ingest(initialized_library)
        new_body = "# Hello\n\nCompletely rewritten body with more words in it."
        _edit_body(initialized_library, art, new_body)

        report = reconcile_library(initialized_library)
        assert report.changed == 1

        row = _row(initialized_library, art["id"],
                   "body_hash, word_count, vector_status")
        assert row["body_hash"] == content_hash(new_body)
        assert row["word_count"] == len(new_body.split())
        assert row["vector_status"] == "pending"

    def test_changed_frontmatter_title_and_tags_win(self, initialized_library):
        art = _ingest(initialized_library)
        path = _article_path(initialized_library, art)
        post = frontmatter.load(str(path))
        post.metadata["title"] = "Renamed Externally"
        post.metadata["tags"] = ["obsidian", "external"]
        post.metadata["obsidian_custom"] = 42  # unknown field
        post.content = post.content + "\n\nEdited."
        path.write_text(frontmatter.dumps(post))
        before = path.read_bytes()

        report = reconcile_library(initialized_library)
        assert report.changed == 1
        # Unknown-frontmatter preservation: S1 never rewrites the file.
        assert path.read_bytes() == before

        conn = get_connection(initialized_library.db_path)
        try:
            row = conn.execute(
                "SELECT title FROM articles WHERE id = ?", (art["id"],)
            ).fetchone()
            assert row["title"] == "Renamed Externally"
            tags = {
                r["name"] for r in conn.execute(
                    "SELECT t.name FROM tags t JOIN article_tags at "
                    "ON t.id = at.tag_id WHERE at.article_id = ?", (art["id"],)
                )
            }
            assert tags == {"obsidian", "external"}
        finally:
            conn.close()

    def test_null_body_hash_is_backfilled_not_changed(self, initialized_library):
        art = _ingest(initialized_library)
        conn = get_connection(initialized_library.db_path)
        try:
            conn.execute(
                "UPDATE articles SET body_hash = NULL WHERE id = ?", (art["id"],)
            )
            conn.commit()
        finally:
            conn.close()
        report = reconcile_library(initialized_library)
        assert report.changed == 0
        assert art["markdown_path"] in report.details.get("backfilled", [])
        assert _row(initialized_library, art["id"], "body_hash")["body_hash"]

    def test_unsettled_file_is_skipped(self, initialized_library, monkeypatch):
        art = _ingest(initialized_library)
        _edit_body(initialized_library, art, "# Hello\n\nFirst external edit.")
        real = rec.body_hash_of_file
        calls = {"n": 0}

        def flappy(path):
            h = real(path)
            if Path(path).name == art["markdown_path"]:
                calls["n"] += 1
                if calls["n"] > 1:
                    return content_hash("simulated mid-write content")
            return h

        monkeypatch.setattr(rec, "body_hash_of_file", flappy)
        report = reconcile_library(initialized_library)
        assert report.skipped_unsettled == 1
        assert report.changed == 0

    def test_dry_run_reports_without_acting(self, initialized_library):
        art = _ingest(initialized_library)
        old_hash = _row(initialized_library, art["id"], "body_hash")["body_hash"]
        _edit_body(initialized_library, art, "# Hello\n\nDry-run edit body.")
        report = reconcile_library(initialized_library, dry_run=True)
        assert report.changed == 1
        assert _row(initialized_library, art["id"], "body_hash")["body_hash"] == old_hash

    def test_reanchor_census_counts_and_warns(self, initialized_library):
        art = _ingest(initialized_library,
                      body="# Hello\n\nAlpha bravo charlie delta echo.")
        # Anchor a highlight on the current body via the real machinery.
        from tiro.anchors import make_anchor
        from tiro.annotations import append_highlight
        path = _article_path(initialized_library, art)
        body = frontmatter.load(str(path)).content
        start = body.index("bravo charlie")
        anchor = make_anchor(body, start, start + len("bravo charlie"))
        conn = get_connection(initialized_library.db_path)
        try:
            row = conn.execute(
                "SELECT id, uid, markdown_path FROM articles WHERE id = ?",
                (art["id"],),
            ).fetchone()
            append_highlight(
                initialized_library,
                conn,
                row,
                quote=anchor["quote"],
                prefix=anchor["prefix"],
                suffix=anchor["suffix"],
                position_start=anchor["position_start"],
                position_end=anchor["position_end"],
                content_hash=content_hash(body),
                color="yellow",
            )
            conn.commit()
        finally:
            conn.close()

        # Shift the quote (prefix grows) -> live status 'shifted' -> census counts it.
        _edit_body(initialized_library, art,
                   "# Hello\n\nNEW LEAD-IN alpha bravo charlie delta echo.")
        report = reconcile_library(initialized_library)
        assert report.changed == 1
        assert report.re_anchored == 1

        # Destroy the quote -> hash_mismatch -> warning detail, not re_anchored.
        _edit_body(initialized_library, art, "# Hello\n\nEntirely different text.")
        report2 = reconcile_library(initialized_library)
        assert report2.re_anchored == 0
        assert report2.details["anchor_warnings"][0]["status"] == "hash_mismatch"
