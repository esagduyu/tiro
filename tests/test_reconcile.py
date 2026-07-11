"""Sync S1: local reconcile engine (write-path plumbing + reconcile_library)."""
import os
import time as _time
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

    def test_apply_time_unreadable_file_does_not_roll_back_pass(
        self, initialized_library, monkeypatch
    ):
        """A file that turns unreadable between the settle poll and the apply
        loop must not roll back other files' already-applied updates in the
        same pass (Finding 1)."""
        good = _ingest(initialized_library, title="Good", url="https://example.com/good")
        bad = _ingest(initialized_library, title="Bad", url="https://example.com/bad")
        _edit_body(initialized_library, good, "# Hello\n\nGood external edit.")
        _edit_body(initialized_library, bad, "# Hello\n\nBad external edit.")

        real_load = rec.frontmatter.load
        bad_name = bad["markdown_path"]
        calls = {"n": 0}

        def flaky_load(path_str):
            if Path(path_str).name == bad_name:
                calls["n"] += 1
                # Calls 1-2 are the scan + settle-poll re-hash (via
                # body_hash_of_file) — let those succeed so the file makes
                # it into scan.changed. Call 3 is the direct apply-time
                # read in _apply_changed — fail exactly there.
                if calls["n"] >= 3:
                    raise OSError("simulated unreadable file at apply time")
            return real_load(path_str)

        monkeypatch.setattr(rec.frontmatter, "load", flaky_load)

        report = reconcile_library(initialized_library)

        # The good file's update must have survived the bad file's failure.
        assert report.changed == 1
        assert bad_name in report.details.get("unreadable", [])
        good_row = _row(initialized_library, good["id"], "body_hash")
        assert good_row["body_hash"] == content_hash("# Hello\n\nGood external edit.")
        # The bad file's row must be untouched (still has its original hash).
        bad_row = _row(initialized_library, bad["id"], "body_hash")
        assert bad_row["body_hash"] != content_hash("# Hello\n\nBad external edit.")

    def test_post_read_failure_rolls_back_and_reports_apply_error(
        self, initialized_library, monkeypatch
    ):
        """Fix wave 2: a failure AFTER the row UPDATE has executed (e.g.
        inside tag sync) must roll back to the per-file SAVEPOINT — the row
        must be untouched, the failure must land in details["apply_errors"]
        (not "unreadable", which is reserved for read/extraction failures),
        and the other file in the same pass must still succeed."""
        good = _ingest(initialized_library, title="Good", url="https://example.com/good")
        bad = _ingest(initialized_library, title="Bad", url="https://example.com/bad")
        _edit_body(initialized_library, good, "# Hello\n\nGood external edit.")
        _edit_body(initialized_library, bad, "# Hello\n\nBad external edit.")

        bad_title_before = _row(initialized_library, bad["id"], "title")["title"]
        bad_hash_before = _row(initialized_library, bad["id"], "body_hash")["body_hash"]

        real_sync = rec._sync_tags_from_frontmatter

        def flaky_sync(conn, article_id, tag_names):
            if article_id == bad["id"]:
                raise RuntimeError("simulated tag-sync failure")
            return real_sync(conn, article_id, tag_names)

        monkeypatch.setattr(rec, "_sync_tags_from_frontmatter", flaky_sync)

        # Give both articles frontmatter tags so _sync_tags_from_frontmatter
        # is actually invoked for each (it's gated on isinstance(..., list)).
        for art in (good, bad):
            path = _article_path(initialized_library, art)
            post = frontmatter.load(str(path))
            post.metadata["tags"] = ["x"]
            frontmatter.dump(post, str(path))

        report = reconcile_library(initialized_library)

        # Only the good file counts as changed.
        assert report.changed == 1
        assert good["markdown_path"] in report.details.get("changed_files", [])
        assert bad["markdown_path"] not in report.details.get("changed_files", [])
        assert bad["markdown_path"] in report.details.get("apply_errors", [])
        assert bad["markdown_path"] not in report.details.get("unreadable", [])

        # The bad article's row is completely unchanged (title AND body_hash),
        # proving the UPDATE that ran before the tag-sync failure was rolled
        # back, not swept into the final commit.
        bad_row = _row(initialized_library, bad["id"], "title, body_hash")
        assert bad_row["title"] == bad_title_before
        assert bad_row["body_hash"] == bad_hash_before

        # The good article's update survives.
        good_row = _row(initialized_library, good["id"], "body_hash")
        assert good_row["body_hash"] == content_hash("# Hello\n\nGood external edit.")

    def test_apply_time_body_hash_recomputed_from_read_body(self, initialized_library):
        """Stored body_hash must match content_hash() of the body actually
        read at apply time, not the settle-time hash (Finding 1, second half)."""
        art = _ingest(initialized_library)
        new_body = "# Hello\n\nRecomputed-hash body."
        _edit_body(initialized_library, art, new_body)
        report = reconcile_library(initialized_library)
        assert report.changed == 1
        row = _row(initialized_library, art["id"], "body_hash")
        assert row["body_hash"] == content_hash(new_body)

    def test_empty_title_falls_back_to_row_title(self, initialized_library):
        """Finding 2: title is NOT NULL/display-critical, so an explicit
        empty-string (or removed) frontmatter title keeps the row's title —
        deliberately asymmetric with author/summary below."""
        art = _ingest(initialized_library, title="Original Title")
        path = _article_path(initialized_library, art)
        post = frontmatter.load(str(path))
        post.metadata["title"] = ""
        post.content = post.content + "\n\nEdited body, empty title."
        path.write_text(frontmatter.dumps(post))

        report = reconcile_library(initialized_library)
        assert report.changed == 1
        row = _row(initialized_library, art["id"], "title")
        assert row["title"] == "Original Title"

    def test_author_key_absent_keeps_row_present_null_clears(self, initialized_library):
        """Finding 2: author is nullable, so it honors explicit user intent —
        key absent keeps the existing row value; key present as null clears
        it. This is the opposite of title's any-falsy-value fallback."""
        art = _ingest(initialized_library, title="Author Test")
        path = _article_path(initialized_library, art)

        # Case A: key removed entirely -> row value survives.
        post = frontmatter.load(str(path))
        assert "author" in post.metadata
        del post.metadata["author"]
        post.content = post.content + "\n\nFirst edit, no author key."
        path.write_text(frontmatter.dumps(post))
        report = reconcile_library(initialized_library)
        assert report.changed == 1
        row = _row(initialized_library, art["id"], "author")
        assert row["author"] == "A. Writer"

        # Case B: key present as explicit null -> cleared to None.
        post2 = frontmatter.load(str(path))
        post2.metadata["author"] = None
        post2.content = post2.content + "\n\nSecond edit, explicit null author."
        path.write_text(frontmatter.dumps(post2))
        report2 = reconcile_library(initialized_library)
        assert report2.changed == 1
        row2 = _row(initialized_library, art["id"], "author")
        assert row2["author"] is None

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


class TestExternalCreate:
    def test_new_file_ingested_as_external(self, initialized_library):
        path = initialized_library.articles_dir / "2026-07-10_my-obsidian-note.md"
        post = frontmatter.Post("# My Note\n\nWritten outside Tiro entirely.")
        post.metadata = {
            "title": "My Obsidian Note",
            "tags": ["thinking"],
            "obsidian_custom": "kept",
        }
        path.write_text(frontmatter.dumps(post))
        before = path.read_bytes()

        report = reconcile_library(initialized_library)
        assert report.ingested == 1
        assert path.read_bytes() == before  # file untouched

        conn = get_connection(initialized_library.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM articles WHERE markdown_path = ?", (path.name,)
            ).fetchone()
            assert row is not None
            assert row["ingestion_method"] == "external"
            assert row["title"] == "My Obsidian Note"
            assert row["uid"]
            assert row["body_hash"] == content_hash(
                "# My Note\n\nWritten outside Tiro entirely.")
            tags = {r["name"] for r in conn.execute(
                "SELECT t.name FROM tags t JOIN article_tags at ON t.id = at.tag_id "
                "WHERE at.article_id = ?", (row["id"],))}
            assert "thinking" in tags
        finally:
            conn.close()

    def test_bare_file_without_frontmatter(self, initialized_library):
        path = initialized_library.articles_dir / "loose-thought.md"
        path.write_text("# A Loose Thought\n\nNo frontmatter at all here.")
        report = reconcile_library(initialized_library)
        assert report.ingested == 1
        conn = get_connection(initialized_library.db_path)
        try:
            row = conn.execute(
                "SELECT title, ingestion_method FROM articles WHERE markdown_path = ?",
                (path.name,),
            ).fetchone()
            assert row["title"] == "A Loose Thought"  # first heading
            assert row["ingestion_method"] == "external"
        finally:
            conn.close()

    def test_duplicate_url_is_skipped(self, initialized_library):
        _ingest(initialized_library, url="https://example.com/dupe")
        path = initialized_library.articles_dir / "copied-in.md"
        post = frontmatter.Post("# Dupe\n\nSame url as an existing article.")
        post.metadata = {"title": "Dupe", "url": "https://example.com/dupe?utm_source=x"}
        path.write_text(frontmatter.dumps(post))

        report = reconcile_library(initialized_library)
        assert report.ingested == 0
        assert "copied-in.md" in report.details.get("skipped_duplicates", [])
        assert path.exists()  # never touched
        conn = get_connection(initialized_library.db_path)
        try:
            n = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
            assert n == 1
        finally:
            conn.close()

    def test_conflict_file_is_never_ingested(self, initialized_library):
        (initialized_library.articles_dir /
         "x.conflict-local-20260710.md").write_text("loser version body")
        report = reconcile_library(initialized_library)
        assert report.ingested == 0

    def test_dry_run_counts_without_ingesting(self, initialized_library):
        (initialized_library.articles_dir / "dry.md").write_text("# Dry\n\nBody.")
        report = reconcile_library(initialized_library, dry_run=True)
        assert report.ingested == 1
        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"] == 0
        finally:
            conn.close()


class TestExternalDelete:
    def test_deleted_file_completes_deletion(self, initialized_library):
        arts = [_ingest(initialized_library, title=f"T{i}",
                        url=f"https://example.com/{i}") for i in range(3)]
        _article_path(initialized_library, arts[0]).unlink()
        report = reconcile_library(initialized_library)
        assert report.deleted == 1
        conn = get_connection(initialized_library.db_path)
        try:
            ids = {r["id"] for r in conn.execute("SELECT id FROM articles")}
            assert arts[0]["id"] not in ids
            assert {arts[1]["id"], arts[2]["id"]} <= ids
        finally:
            conn.close()

    def test_all_files_missing_is_guarded(self, initialized_library):
        arts = [_ingest(initialized_library, title=f"G{i}",
                        url=f"https://example.com/g{i}") for i in range(3)]
        for a in arts:
            _article_path(initialized_library, a).unlink()
        report = reconcile_library(initialized_library)
        assert report.deleted == 0
        assert report.details.get("delete_guard")
        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"] == 3
        finally:
            conn.close()

    def test_over_threshold_is_guarded(self, initialized_library):
        arts = [_ingest(initialized_library, title=f"H{i}",
                        url=f"https://example.com/h{i}") for i in range(12)]
        for a in arts[:11]:  # 11 of 12 missing: > max(10, ceil(2.4)) = 10
            _article_path(initialized_library, a).unlink()
        report = reconcile_library(initialized_library)
        assert report.deleted == 0
        assert report.details.get("delete_guard")

    def test_dry_run_never_deletes(self, initialized_library):
        art = _ingest(initialized_library)
        _ingest(initialized_library, title="Keeper", url="https://example.com/k")
        _article_path(initialized_library, art).unlink()
        report = reconcile_library(initialized_library, dry_run=True)
        assert report.deleted == 1
        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"] == 2
        finally:
            conn.close()

    def test_exact_threshold_is_not_guarded(self, initialized_library):
        # total=12, missing=10 -> max(10, ceil(0.2*12)=3) = 10; guard is a
        # strict `>`, so missing == threshold must NOT be guarded (frozen
        # semantics — see _delete_guarded).
        arts = [_ingest(initialized_library, title=f"E{i}",
                        url=f"https://example.com/e{i}") for i in range(12)]
        for a in arts[:10]:
            _article_path(initialized_library, a).unlink()
        report = reconcile_library(initialized_library)
        assert report.deleted == 10
        assert "delete_guard" not in report.details
        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"] == 2
        finally:
            conn.close()

    def test_single_article_carve_out(self, initialized_library):
        # total=1, missing=1: the all-missing guard requires total > 1, so a
        # lone article's file removal must delete cleanly, not be guarded.
        art = _ingest(initialized_library)
        _article_path(initialized_library, art).unlink()
        report = reconcile_library(initialized_library)
        assert report.deleted == 1
        assert "delete_guard" not in report.details
        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"] == 0
        finally:
            conn.close()

    def test_delete_error_isolated_per_row(self, initialized_library, monkeypatch):
        art1 = _ingest(initialized_library, title="Fails", url="https://example.com/fail")
        art2 = _ingest(initialized_library, title="Succeeds", url="https://example.com/ok")
        _ingest(initialized_library, title="Keeper", url="https://example.com/keeper")
        _article_path(initialized_library, art1).unlink()
        _article_path(initialized_library, art2).unlink()

        import tiro.lifecycle as lifecycle_mod
        real_delete_article = lifecycle_mod.delete_article

        def flaky_delete_article(config, article_id, *a, **kw):
            if article_id == art1["id"]:
                raise RuntimeError("simulated delete failure")
            return real_delete_article(config, article_id, *a, **kw)

        monkeypatch.setattr(lifecycle_mod, "delete_article", flaky_delete_article)

        report = reconcile_library(initialized_library)  # must not raise

        assert report.deleted == 1
        errors = report.details.get("delete_errors")
        assert errors == [{"id": art1["id"], "title": "Fails"}]

        conn = get_connection(initialized_library.db_path)
        try:
            ids = {r["id"] for r in conn.execute("SELECT id FROM articles")}
            assert art1["id"] in ids  # left in place for retry next pass
            assert art2["id"] not in ids
        finally:
            conn.close()


class TestSidecarReconcile:
    def test_external_note_edit_wins_files_win(self, initialized_library):
        art = _ingest(initialized_library)
        from tiro.annotations import sidecar_stem, write_note
        conn = get_connection(initialized_library.db_path)
        try:
            arow = conn.execute("SELECT * FROM articles WHERE id = ?",
                                (art["id"],)).fetchone()
        finally:
            conn.close()
        stem = sidecar_stem(arow)
        write_note(initialized_library, stem, "original note")
        # Row exists too (reconcile_annotations inserts it on the next pass).
        reconcile_library(initialized_library)
        # External edit: file newer than row -> plain files-win, no conflict.
        note_path = initialized_library.library / "notes" / f"{stem}.md"
        note_path.write_text("edited in Obsidian")
        report = reconcile_library(initialized_library)
        assert report.conflicts == 0
        conn = get_connection(initialized_library.db_path)
        try:
            row = conn.execute(
                "SELECT body_markdown FROM notes WHERE article_id = ? "
                "AND highlight_id IS NULL", (art["id"],)).fetchone()
            assert row["body_markdown"] == "edited in Obsidian"
        finally:
            conn.close()

    def test_ambiguous_note_prefers_external_and_writes_conflict(
            self, initialized_library):
        art = _ingest(initialized_library)
        from tiro.annotations import sidecar_stem, write_note
        conn = get_connection(initialized_library.db_path)
        try:
            arow = conn.execute("SELECT * FROM articles WHERE id = ?",
                                (art["id"],)).fetchone()
        finally:
            conn.close()
        stem = sidecar_stem(arow)
        write_note(initialized_library, stem, "file version")
        reconcile_library(initialized_library)  # index the row

        # Simulate row leading the file: row body differs AND row.updated_at
        # is far newer than the file's mtime.
        conn = get_connection(initialized_library.db_path)
        try:
            conn.execute(
                "UPDATE notes SET body_markdown = 'db-only version', "
                "updated_at = ? WHERE article_id = ? AND highlight_id IS NULL",
                ("2099-01-01T00:00:00Z", art["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        note_path = initialized_library.library / "notes" / f"{stem}.md"
        old = _time.time() - 3600
        os.utime(note_path, (old, old))

        report = reconcile_library(initialized_library)
        assert report.conflicts == 1
        conflict = list((initialized_library.library / "notes").glob(
            f"{stem}.conflict-local-*.md"))
        assert len(conflict) == 1
        assert conflict[0].read_text() == "db-only version"
        # External (file) version won in the index:
        conn = get_connection(initialized_library.db_path)
        try:
            row = conn.execute(
                "SELECT body_markdown FROM notes WHERE article_id = ? "
                "AND highlight_id IS NULL", (art["id"],)).fetchone()
            assert row["body_markdown"] == "file version"
        finally:
            conn.close()

    def test_annotations_counts_surface_in_details(self, initialized_library):
        _ingest(initialized_library)
        report = reconcile_library(initialized_library)
        assert "annotations" in report.details
        assert "highlights_matched" in report.details["annotations"]

    def test_dry_run_skips_sidecar_phase(self, initialized_library):
        _ingest(initialized_library)
        report = reconcile_library(initialized_library, dry_run=True)
        assert report.details.get("annotations") == "skipped (dry-run)"


class TestConflictWriter:
    def test_collision_safe_naming(self, tmp_path):
        from tiro.sync.reconcile import write_conflict_file
        p1 = write_conflict_file(tmp_path, "stem", "one")
        p2 = write_conflict_file(tmp_path, "stem", "two")
        assert p1 != p2
        assert is_conflict_file(p1.name) and is_conflict_file(p2.name)
        assert p1.read_text() == "one" and p2.read_text() == "two"


class TestDoctorConflictCensus:
    def test_conflict_files_not_orphaned_and_censused(self, initialized_library):
        from tiro.doctor import scan
        _ingest(initialized_library)
        (initialized_library.articles_dir /
         "old.conflict-local-20260710.md").write_text("loser body")
        report = scan(initialized_library)
        assert "old.conflict-local-20260710.md" not in report["orphaned_markdown"]
        assert "old.conflict-local-20260710.md" in report["conflict_files"]
        # Report-only: census never flips the health verdicts.
        assert report["structurally_consistent"] is True
        assert report["clean"] is True

    def test_fix_leaves_conflict_files_in_place(self, initialized_library):
        from tiro.doctor import fix
        _ingest(initialized_library)
        p = initialized_library.articles_dir / "keep.conflict-local-20260710.md"
        p.write_text("loser body")
        fix(initialized_library)
        assert p.exists()
        assert not (initialized_library.library / ".orphaned" /
                    "keep.conflict-local-20260710.md").exists()
