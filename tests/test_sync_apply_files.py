"""Sync S2: apply_ops — file ops (fast-forward, conflict, materialize)."""
import json

import frontmatter
import pytest

from tiro.anchors import content_hash
from tiro.database import get_connection
from tiro.ingestion.processor import process_article
from tiro.migrations import new_ulid
from tiro.sync import reconcile as rec
from tiro.sync.journal import FileDel, FilePut, HLCClock
from tiro.sync.merge import ApplyReport, apply_ops


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch):
    monkeypatch.setattr(rec, "SETTLE_SECONDS", 0.0)


def _ingest(config, title="Hello World", body="# Hello\n\nSome body text.",
            url="https://example.com/hello"):
    return process_article(
        title=title, author="A. Writer", content_md=body, url=url, config=config,
    )


def _arow(config, article_id):
    conn = get_connection(config.db_path)
    try:
        return conn.execute("SELECT * FROM articles WHERE id = ?",
                            (article_id,)).fetchone()
    finally:
        conn.close()


def _clock(ms=1720000000000, device="dev-b"):
    return HLCClock(device, now_ms=lambda: ms)


def _fileput(uid, path_hint, body, *, base_hash=None, clock=None):
    clock = clock or _clock()
    return FilePut(op_id=new_ulid(), hlc=clock.tick(), device=clock.device,
                   uid=uid, path_hint=path_hint,
                   object_hash=content_hash(body), base_hash=base_hash,
                   body=body)


def _remote_article_doc(title="Remote Article", url="https://remote.example.com/a",
                        body="Remote body text here.", tags=("alpha",)):
    post = frontmatter.Post(body)
    post.metadata = {"title": title, "author": "R. Writer", "source": "Remote",
                     "url": url, "published": "2026-07-01",
                     "tags": list(tags), "summary": "A remote summary."}
    return frontmatter.dumps(post)


class TestFilePutArticle:
    def test_fast_forward_known_article(self, initialized_library):
        art = _ingest(initialized_library)
        row = _arow(initialized_library, art["id"])
        doc_path = initialized_library.articles_dir / row["markdown_path"]
        post = frontmatter.load(str(doc_path))
        post.content = "# Hello\n\nEdited on the other device."
        new_doc = frontmatter.dumps(post)

        # base_hash is a BODY hash (body_hash semantics), never a whole-file hash.
        op = _fileput(row["uid"], f"articles/{row['markdown_path']}", new_doc,
                      base_hash=row["body_hash"])
        report = apply_ops(initialized_library, [op])
        assert isinstance(report, ApplyReport)
        assert report.applied == 1 and report.conflicts == 0

        after = _arow(initialized_library, art["id"])
        on_disk = frontmatter.load(str(doc_path)).content
        assert "other device" in on_disk
        assert after["body_hash"] == content_hash(on_disk)
        assert after["vector_status"] == "pending"

    def test_idempotent_reapply_is_noop(self, initialized_library):
        art = _ingest(initialized_library)
        row = _arow(initialized_library, art["id"])
        doc_path = initialized_library.articles_dir / row["markdown_path"]
        post = frontmatter.load(str(doc_path))
        post.content = "# Hello\n\nEdited once."
        new_doc = frontmatter.dumps(post)
        op = _fileput(row["uid"], f"articles/{row['markdown_path']}", new_doc,
                      base_hash=row["body_hash"])
        apply_ops(initialized_library, [op])
        before = doc_path.read_bytes()
        report2 = apply_ops(initialized_library, [op])
        assert report2.conflicts == 0 and report2.errors == 0
        assert doc_path.read_bytes() == before

    def test_shadow_hash_is_body_space_and_carries_path_hint(
            self, initialized_library):
        """Review Major #1 (hash spaces) + guard soundness: an applied
        article op's shadow row stores the BODY-space hash — NEVER
        op.object_hash, which for hydrated article ops is the FULL-file
        blob address — and its fields carry path_hint so the
        unreadable-protection guard in diff/save_shadow can see it."""
        art = _ingest(initialized_library)
        row = _arow(initialized_library, art["id"])
        doc_path = initialized_library.articles_dir / row["markdown_path"]
        post = frontmatter.load(str(doc_path))
        post.content = "# Hello\n\nBody-space edit."
        new_doc = frontmatter.dumps(post)
        op = _fileput(row["uid"], f"articles/{row['markdown_path']}", new_doc,
                      base_hash=row["body_hash"])
        # The two spaces genuinely differ (the doc has frontmatter).
        assert op.object_hash != content_hash(frontmatter.loads(new_doc).content)

        report1 = apply_ops(initialized_library, [op])
        assert report1.applied == 1
        before = doc_path.read_bytes()
        report2 = apply_ops(initialized_library, [op])  # second apply: no-op
        assert report2.applied == 0 and report2.skipped_stale == 1
        assert report2.conflicts == 0 and report2.errors == 0
        assert doc_path.read_bytes() == before

        conn = get_connection(initialized_library.db_path)
        try:
            srow = conn.execute(
                "SELECT hash, fields_json FROM sync_shadow "
                "WHERE kind = 'article' AND uid = ?", (row["uid"],)).fetchone()
        finally:
            conn.close()
        assert srow["hash"] == rec.body_hash_of_file(doc_path)  # BODY space
        assert srow["hash"] != op.object_hash                   # never blob space
        assert (json.loads(srow["fields_json"])["path_hint"]
                == f"articles/{row['markdown_path']}")

    def test_same_body_newer_hlc_is_fast_forward_noop(self, initialized_library):
        """Decision #8 rule (c): same content arriving under a NEWER HLC is a
        no-op fast-forward — counts as applied, advances the shadow hlc,
        leaves the file bytes untouched (S2.4 review Major #2i)."""
        art = _ingest(initialized_library)
        row = _arow(initialized_library, art["id"])
        hint = f"articles/{row['markdown_path']}"
        doc_path = initialized_library.articles_dir / row["markdown_path"]
        post = frontmatter.load(str(doc_path))
        post.content = "# Hello\n\nConverged content."
        new_doc = frontmatter.dumps(post)
        op1 = _fileput(row["uid"], hint, new_doc, base_hash=row["body_hash"],
                       clock=_clock(ms=1000))
        apply_ops(initialized_library, [op1])
        before = doc_path.read_bytes()

        # Same body, different device, NEWER HLC (base_hash stale on purpose
        # — rule (c) fires on content identity before any concurrency check).
        op2 = _fileput(row["uid"], hint, new_doc, base_hash="0" * 64,
                       clock=_clock(ms=2000, device="dev-c"))
        report = apply_ops(initialized_library, [op2])
        assert report.applied == 1 and report.conflicts == 0
        assert report.skipped_stale == 0 and report.errors == 0
        assert "fast_forward_noop" in report.details
        assert doc_path.read_bytes() == before
        conn = get_connection(initialized_library.db_path)
        try:
            srow = conn.execute(
                "SELECT hlc FROM sync_shadow WHERE kind='article' AND uid = ?",
                (row["uid"],)).fetchone()
        finally:
            conn.close()
        assert srow["hlc"] == op2.hlc.to_str()  # shadow advanced to op2

    def test_concurrent_edit_remote_wins(self, initialized_library):
        """Decision #8 rule (e), remote side: unedited local (shadow seeded,
        file unchanged since) + base-hash mismatch + NEWER remote HLC ⇒
        remote body becomes canonical, LOCAL body preserved as a
        conflict-local file, row refreshed (S2.4 review Major #2ii)."""
        art = _ingest(initialized_library)
        row = _arow(initialized_library, art["id"])
        hint = f"articles/{row['markdown_path']}"
        doc_path = initialized_library.articles_dir / row["markdown_path"]
        # Seed the shadow: a clean applied edit from dev-b at HLC ms=1000.
        v1 = _remote_article_doc(body="v1 synced everywhere")
        op1 = _fileput(row["uid"], hint, v1, base_hash=row["body_hash"],
                       clock=_clock(ms=1000))
        assert apply_ops(initialized_library, [op1]).applied == 1
        local_body = frontmatter.load(str(doc_path)).content

        # Remote edit made against a THIRD ancestor (base mismatch) but
        # NEWER than the local side's shadow hlc -> remote wins.
        v2 = _remote_article_doc(title="Remote Winner", body="v2 remote winner")
        op2 = _fileput(row["uid"], hint, v2, base_hash="0" * 64,
                       clock=_clock(ms=2000, device="dev-c"))
        report = apply_ops(initialized_library, [op2])
        assert report.conflicts == 1 and report.errors == 0
        assert "conflict_remote_won" in report.details
        assert frontmatter.load(str(doc_path)).content == "v2 remote winner"
        conflicts = list(initialized_library.articles_dir.glob(
            f"{doc_path.stem}.conflict-local-*.md"))
        assert len(conflicts) == 1
        assert local_body in conflicts[0].read_text()
        after = _arow(initialized_library, art["id"])
        assert after["title"] == "Remote Winner"  # row refreshed

    def test_stale_op_skipped_after_newer_applied(self, initialized_library):
        art = _ingest(initialized_library)
        row = _arow(initialized_library, art["id"])
        hint = f"articles/{row['markdown_path']}"
        newer = _fileput(row["uid"], hint,
                         _remote_article_doc(body="v2 newer"),
                         base_hash=row["body_hash"],
                         clock=_clock(ms=2000000000000))
        older = _fileput(row["uid"], hint,
                         _remote_article_doc(body="v1 older"),
                         base_hash=row["body_hash"],
                         clock=_clock(ms=1000000000000))
        apply_ops(initialized_library, [newer])
        report = apply_ops(initialized_library, [older])
        assert report.skipped_stale == 1
        body = frontmatter.load(
            str(initialized_library.articles_dir / row["markdown_path"])).content
        assert "v2 newer" in body

    def test_concurrent_edit_conflict_preserves_loser(self, initialized_library):
        art = _ingest(initialized_library)
        row = _arow(initialized_library, art["id"])
        doc_path = initialized_library.articles_dir / row["markdown_path"]
        # Local un-diffed edit (file differs from shadow == no shadow row).
        post = frontmatter.load(str(doc_path))
        post.content = "# Hello\n\nLocal concurrent edit."
        doc_path.write_text(frontmatter.dumps(post))
        local_body = frontmatter.load(str(doc_path)).content

        remote_doc = _remote_article_doc(body="Remote concurrent edit.")
        op = _fileput(row["uid"], f"articles/{row['markdown_path']}",
                      remote_doc, base_hash=row["body_hash"],
                      clock=_clock(ms=1000))  # remote HLC far in the past -> loses
        report = apply_ops(initialized_library, [op])
        assert report.conflicts == 1
        # Local stayed canonical; remote preserved as conflict file.
        assert frontmatter.load(str(doc_path)).content == local_body
        stem = doc_path.stem
        conflicts = list(initialized_library.articles_dir.glob(
            f"{stem}.conflict-devb-*.md"))
        assert len(conflicts) == 1
        assert "Remote concurrent edit." in conflicts[0].read_text()

    def test_unknown_uid_materializes_article(self, initialized_library):
        doc = _remote_article_doc()
        uid = new_ulid()
        op = _fileput(uid, "articles/2026-07-01_remote-article.md", doc)
        report = apply_ops(initialized_library, [op])
        assert report.applied == 1
        conn = get_connection(initialized_library.db_path)
        try:
            row = conn.execute("SELECT * FROM articles WHERE uid = ?",
                               (uid,)).fetchone()
            assert row is not None
            assert row["title"] == "Remote Article"
            assert row["ingestion_method"] == "sync"
            assert row["vector_status"] == "pending"
            assert row["url"] == "https://remote.example.com/a"
            tags = {r["name"] for r in conn.execute(
                "SELECT t.name FROM tags t JOIN article_tags at "
                "ON t.id = at.tag_id JOIN articles a ON a.id = at.article_id "
                "WHERE a.uid = ?", (uid,))}
            assert tags == {"alpha"}
        finally:
            conn.close()
        assert (initialized_library.articles_dir /
                "2026-07-01_remote-article.md").exists()

    def test_path_hint_traversal_rejected(self, initialized_library):
        op = _fileput(new_ulid(), "../outside.md", "evil")
        report = apply_ops(initialized_library, [op])
        assert report.errors == 1 and report.applied == 0
        assert not (initialized_library.library.parent / "outside.md").exists()

    def test_unhydrated_file_put_is_error_not_raise(self, initialized_library):
        clock = _clock()
        op = FilePut(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                     uid=new_ulid(), path_hint="articles/x.md",
                     object_hash="a" * 64, body=None)
        report = apply_ops(initialized_library, [op])
        assert report.errors == 1


class TestNoteWikiPathfile:
    def test_note_put_creates_file_and_row(self, initialized_library):
        art = _ingest(initialized_library)
        row = _arow(initialized_library, art["id"])
        stem = row["markdown_path"].rsplit(".", 1)[0]
        op = _fileput(row["uid"], f"notes/{stem}.md", "a synced note")
        report = apply_ops(initialized_library, [op])
        assert report.applied == 1
        assert (initialized_library.library / "notes" / f"{stem}.md"
                ).read_text() == "a synced note"
        conn = get_connection(initialized_library.db_path)
        try:
            nrow = conn.execute(
                "SELECT body_markdown FROM notes WHERE article_id = ? "
                "AND highlight_id IS NULL", (art["id"],)).fetchone()
            assert nrow["body_markdown"] == "a synced note"
        finally:
            conn.close()

    def test_note_concurrent_edit_conflict(self, initialized_library):
        art = _ingest(initialized_library)
        row = _arow(initialized_library, art["id"])
        stem = row["markdown_path"].rsplit(".", 1)[0]
        from tiro.annotations import write_note
        write_note(initialized_library, stem, "local note version")
        op = _fileput(row["uid"], f"notes/{stem}.md", "remote note version",
                      base_hash=content_hash("some third ancestor"),
                      clock=_clock(ms=1000))
        report = apply_ops(initialized_library, [op])
        assert report.conflicts == 1
        note_path = initialized_library.library / "notes" / f"{stem}.md"
        assert note_path.read_text() == "local note version"
        conflicts = list((initialized_library.library / "notes").glob(
            f"{stem}.conflict-devb-*.md"))
        assert conflicts and conflicts[0].read_text() == "remote note version"

    def test_note_concurrent_edit_remote_wins(self, initialized_library):
        """Decision #8 rule (e) remote side, non-article path: seeded shadow
        + unchanged local note + base mismatch + newer remote HLC ⇒ remote
        note canonical, local preserved as conflict-local file, notes row
        mirrors the new body (S2.4 review Major #2ii)."""
        art = _ingest(initialized_library)
        row = _arow(initialized_library, art["id"])
        stem = row["markdown_path"].rsplit(".", 1)[0]
        op1 = _fileput(row["uid"], f"notes/{stem}.md", "v1 note",
                       clock=_clock(ms=1000))
        assert apply_ops(initialized_library, [op1]).applied == 1
        op2 = _fileput(row["uid"], f"notes/{stem}.md", "v2 remote note",
                       base_hash=content_hash("third ancestor"),
                       clock=_clock(ms=2000, device="dev-c"))
        report = apply_ops(initialized_library, [op2])
        assert report.conflicts == 1 and report.errors == 0
        note_path = initialized_library.library / "notes" / f"{stem}.md"
        assert note_path.read_text() == "v2 remote note"
        conflicts = list((initialized_library.library / "notes").glob(
            f"{stem}.conflict-local-*.md"))
        assert len(conflicts) == 1
        assert conflicts[0].read_text() == "v1 note"
        conn = get_connection(initialized_library.db_path)
        try:
            nrow = conn.execute(
                "SELECT body_markdown FROM notes WHERE article_id = ? "
                "AND highlight_id IS NULL", (art["id"],)).fetchone()
            assert nrow["body_markdown"] == "v2 remote note"
        finally:
            conn.close()

    def test_wiki_put_and_index_refresh(self, initialized_library):
        doc = ("---\nuid: 01WIKI0000000000000000001\nkind: entity\n"
               "title: Anthropic\nstatus: fresh\narticle_uids: []\n---\n\nBody.")
        op = _fileput("01WIKI0000000000000000001", "wiki/entities/anthropic.md", doc)
        report = apply_ops(initialized_library, [op])
        assert report.applied == 1
        assert (initialized_library.wiki_dir / "entities" / "anthropic.md").exists()
        conn = get_connection(initialized_library.db_path)
        try:
            wrow = conn.execute(
                "SELECT * FROM wiki_pages WHERE slug = 'entities/anthropic'"
            ).fetchone()
            assert wrow is not None  # reconcile_wiki_index ran once at the end
        finally:
            conn.close()

    def test_wiki_conflict_file_never_indexed(self, initialized_library):
        """Spec §4: conflict files sync as ordinary files but are EXCLUDED
        from ingest/index. A synced wiki conflict file (same frontmatter uid
        as the real page) must never appear in wiki_pages (S2.4 review
        Major #1)."""
        doc = ("---\nuid: 01WIKI0000000000000000001\nkind: entity\n"
               "title: Anthropic\nstatus: fresh\narticle_uids: []\n---\n\nBody.")
        real = _fileput("01WIKI0000000000000000001",
                        "wiki/entities/anthropic.md", doc, clock=_clock(ms=1000))
        conflict = _fileput(
            "path:wiki/entities/anthropic.conflict-devb-20260716.md",
            "wiki/entities/anthropic.conflict-devb-20260716.md", doc,
            clock=_clock(ms=2000))
        report = apply_ops(initialized_library, [real, conflict])
        assert report.applied == 2 and report.errors == 0
        assert (initialized_library.wiki_dir / "entities" /
                "anthropic.conflict-devb-20260716.md").exists()  # file synced
        conn = get_connection(initialized_library.db_path)
        try:
            slugs = [r["slug"] for r in conn.execute(
                "SELECT slug FROM wiki_pages")]
        finally:
            conn.close()
        assert slugs == ["entities/anthropic"]  # ...but never indexed

    def test_pathfile_roundtrip(self, initialized_library):
        op = _fileput("path:articles/x.conflict-devb-20260710.md",
                      "articles/x.conflict-devb-20260710.md", "loser body")
        report = apply_ops(initialized_library, [op])
        assert report.applied == 1
        p = initialized_library.articles_dir / "x.conflict-devb-20260710.md"
        assert p.read_text() == "loser body"
        # And it never becomes an article row (conflict files excluded).
        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute("SELECT COUNT(*) AS n FROM articles"
                                ).fetchone()["n"] == 0
        finally:
            conn.close()

    def test_file_del_edit_wins(self, initialized_library):
        art = _ingest(initialized_library)
        row = _arow(initialized_library, art["id"])
        stem = row["markdown_path"].rsplit(".", 1)[0]
        from tiro.annotations import write_note
        write_note(initialized_library, stem, "edited since their shadow")
        clock = _clock()
        op = FileDel(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                     uid=row["uid"], path_hint=f"notes/{stem}.md",
                     base_hash=content_hash("what dev-b last saw"))
        report = apply_ops(initialized_library, [op])
        assert report.resurrected == 1
        assert (initialized_library.library / "notes" / f"{stem}.md").exists()

    def test_file_del_clean_delete(self, initialized_library):
        art = _ingest(initialized_library)
        row = _arow(initialized_library, art["id"])
        stem = row["markdown_path"].rsplit(".", 1)[0]
        from tiro.annotations import write_note
        write_note(initialized_library, stem, "note body")
        clock = _clock()
        op = FileDel(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                     uid=row["uid"], path_hint=f"notes/{stem}.md",
                     base_hash=content_hash("note body"))
        report = apply_ops(initialized_library, [op])
        assert report.applied == 1
        assert not (initialized_library.library / "notes" / f"{stem}.md").exists()

    def test_file_del_without_base_hash_never_blind_deletes(
            self, initialized_library):
        """base_hash=None on an EXISTING file is concurrent-by-construction
        (put-side parity, retention bias): the file is kept and reported
        resurrected, never silently unlinked (S2.4 review Minor #2)."""
        art = _ingest(initialized_library)
        row = _arow(initialized_library, art["id"])
        stem = row["markdown_path"].rsplit(".", 1)[0]
        from tiro.annotations import write_note
        write_note(initialized_library, stem, "user text stays")
        clock = _clock()
        op = FileDel(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                     uid=row["uid"], path_hint=f"notes/{stem}.md",
                     base_hash=None)
        report = apply_ops(initialized_library, [op])
        assert report.resurrected == 1 and report.applied == 0
        assert (initialized_library.library / "notes" /
                f"{stem}.md").read_text() == "user text stays"

    def test_article_file_del_is_error_never_delete(self, initialized_library):
        """Article deletion is row_del through delete_article ONLY — a
        file_del aimed at articles/ must land in report.errors and touch
        nothing (S2.4 review Major #2, safety invariant pin)."""
        art = _ingest(initialized_library)
        row = _arow(initialized_library, art["id"])
        clock = _clock()
        op = FileDel(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                     uid=row["uid"],
                     path_hint=f"articles/{row['markdown_path']}",
                     base_hash=row["body_hash"])
        report = apply_ops(initialized_library, [op])
        assert report.errors == 1 and report.applied == 0
        assert (initialized_library.articles_dir /
                row["markdown_path"]).exists()
        assert _arow(initialized_library, art["id"]) is not None
