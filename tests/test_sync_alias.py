"""Sync S2: article URL dedupe + alias ops (spec §4 dedupe row)."""
import frontmatter
import pytest

from tiro.anchors import content_hash
from tiro.database import get_connection
from tiro.ingestion.processor import process_article
from tiro.migrations import new_ulid
from tiro.sync import reconcile as rec
from tiro.sync.journal import Alias, FilePut, HLCClock
from tiro.sync.merge import apply_ops


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch):
    monkeypatch.setattr(rec, "SETTLE_SECONDS", 0.0)


def _ingest(config, title="Hello World", body="# Hello\n\nSome body text.",
            url="https://example.com/hello"):
    return process_article(
        title=title, author="A. Writer", content_md=body, url=url, config=config,
    )


def _clock(ms=1720000000000, device="dev-b"):
    return HLCClock(device, now_ms=lambda: ms)


def _remote_doc(url, body="Remote body.", title="Remote Copy"):
    post = frontmatter.Post(body)
    post.metadata = {"title": title, "url": url, "tags": ["dupe"]}
    return frontmatter.dumps(post)


def _fileput(uid, path_hint, doc, clock=None):
    clock = clock or _clock()
    return FilePut(op_id=new_ulid(), hlc=clock.tick(), device=clock.device,
                   uid=uid, path_hint=path_hint,
                   object_hash=content_hash(doc), body=doc)


def _article_uid(config, article_id):
    conn = get_connection(config.db_path)
    try:
        return conn.execute("SELECT uid FROM articles WHERE id = ?",
                            (article_id,)).fetchone()["uid"]
    finally:
        conn.close()


class TestUrlDedupe:
    def test_same_canonical_url_different_uid_dedupes(self, initialized_library):
        art = _ingest(initialized_library, url="https://example.com/hello")
        local_uid = _article_uid(initialized_library, art["id"])
        # Incoming uid deliberately LARGER (ULID-newer) -> local uid survives.
        incoming_uid = "7ZZZZZZZZZZZZZZZZZZZZZZZZZ"
        doc = _remote_doc("https://example.com/hello?utm_source=x")
        op = _fileput(incoming_uid, "articles/2026-07-01_remote-copy.md", doc)
        report = apply_ops(initialized_library, [op])

        conn = get_connection(initialized_library.db_path)
        try:
            n = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
            assert n == 1  # no second row materialized
            survivor = conn.execute("SELECT uid FROM articles").fetchone()["uid"]
            assert survivor == local_uid
        finally:
            conn.close()
        # An alias op was emitted for the journal (old=incoming, new=local).
        aliases = [o for o in report.emitted_ops if isinstance(o, Alias)]
        assert len(aliases) == 1
        assert aliases[0].uid == incoming_uid
        assert aliases[0].new_uid == local_uid
        # The incoming BODY was routed through the file-merge path: since the
        # local body differs and the local side wins (fresh local tick beats
        # the remote stamp only if newer — here remote is older), assert a
        # deterministic outcome: either fast-forward applied or conflict file,
        # but never a silent drop.
        assert report.conflicts + report.applied >= 1

    def test_incoming_older_uid_survives_local_renamed(self, initialized_library):
        """Decision #12 (FROZEN): the lexicographically-smaller uid wins even
        when it is the INCOMING one — the local row adopts it."""
        from tiro.annotations import append_highlight, read_annotations, sidecar_stem
        from tiro.sync.manifest import build_manifest, save_shadow

        art = _ingest(initialized_library, url="https://example.com/hello")
        conn = get_connection(initialized_library.db_path)
        try:
            row = conn.execute("SELECT * FROM articles WHERE id = ?",
                               (art["id"],)).fetchone()
            body = frontmatter.load(str(
                initialized_library.articles_dir / row["markdown_path"])).content
            append_highlight(
                initialized_library, conn, row, quote="body",
                prefix="Some ", suffix=" text",
                position_start=body.index("body"),
                position_end=body.index("body") + 4,
                content_hash=content_hash(body), color="yellow")
            conn.commit()
        finally:
            conn.close()
        local_uid = row["uid"]
        # Seed live shadow rows so the tombstoning is observable.
        save_shadow(initialized_library, build_manifest(initialized_library))

        # Real ULIDs start "01…" (2026 wall clocks) — 26 zeros sorts below any.
        incoming_uid = "0" * 26
        doc = _remote_doc("https://example.com/hello?utm_source=x")
        op = _fileput(incoming_uid, "articles/2026-07-01_remote-copy.md", doc)
        report = apply_ops(initialized_library, [op])

        conn = get_connection(initialized_library.db_path)
        try:
            n = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
            assert n == 1
            survivor = conn.execute("SELECT uid FROM articles").fetchone()["uid"]
            assert survivor == incoming_uid
            # Old local uid's article shadow row tombstoned by the rename.
            srow = conn.execute(
                "SELECT deleted_at FROM sync_shadow WHERE kind='article' AND uid=?",
                (local_uid,)).fetchone()
            assert srow is not None and srow["deleted_at"] is not None
        finally:
            conn.close()
        aliases = [o for o in report.emitted_ops if isinstance(o, Alias)]
        assert len(aliases) == 1
        assert aliases[0].uid == local_uid
        assert aliases[0].new_uid == incoming_uid
        # Sidecar lines rewrote article_uid to the surviving uid.
        lines = read_annotations(initialized_library, sidecar_stem(row))
        assert lines and all(ln["article_uid"] == incoming_uid for ln in lines)
        assert report.conflicts + report.applied >= 1

    def test_dedupe_alias_emitted_once_per_batch(self, initialized_library):
        """Mandate B: two file_puts for the SAME duplicate in one batch emit
        exactly ONE alias op (decision #11 'dedupe alias ops')."""
        art = _ingest(initialized_library, url="https://example.com/hello")
        local_uid = _article_uid(initialized_library, art["id"])
        incoming_uid = "7ZZZZZZZZZZZZZZZZZZZZZZZZZ"
        clock = _clock()
        op1 = _fileput(incoming_uid, "articles/2026-07-01_remote-copy.md",
                       _remote_doc("https://example.com/hello?utm_source=x"),
                       clock=clock)
        op2 = _fileput(incoming_uid, "articles/2026-07-01_remote-copy.md",
                       _remote_doc("https://example.com/hello?utm_source=x",
                                   body="Remote body, second delivery."),
                       clock=clock)
        report = apply_ops(initialized_library, [op1, op2])
        aliases = [o for o in report.emitted_ops if isinstance(o, Alias)]
        assert len(aliases) == 1
        assert (aliases[0].uid, aliases[0].new_uid) == (incoming_uid, local_uid)
        # Locally-authored stamp (S2.7 review Minor 1): the alias op is the
        # MERGE's own, never attributed to the triggering remote device.
        assert aliases[0].device != "dev-b"
        # as_dict renders emitted ops in wire form (S2.7 review Nit 1).
        wire = report.as_dict()["emitted_ops"]
        assert len(wire) == 1
        assert wire[0]["kind"] == "alias"
        assert wire[0]["uid"] == incoming_uid
        assert wire[0]["payload"] == {"new_uid": local_uid}
        conn = get_connection(initialized_library.db_path)
        try:
            n = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
            assert n == 1
        finally:
            conn.close()

    def test_email_dedupe_by_title_and_sender_not_here(self, initialized_library):
        """Title+email_sender dedupe requires the source row — S2 dedupes on
        canonical URL only; email-duplicate handling matches by url=''
        never (documented limitation, revisit in S5 integration tests)."""
        # Guard test: an email-ish doc with no url materializes normally.
        doc = _remote_doc("", title="No URL Newsletter")
        uid = new_ulid()
        op = _fileput(uid, "articles/2026-07-01_no-url.md", doc)
        report = apply_ops(initialized_library, [op])
        assert report.applied == 1 and not report.emitted_ops


class TestAliasOp:
    def _two_articles(self, config):
        a1 = _ingest(config, title="Keep", url="https://example.com/keep")
        a2 = _ingest(config, title="Lose", url="https://example.com/lose")
        conn = get_connection(config.db_path)
        try:
            keep = conn.execute("SELECT * FROM articles WHERE id = ?",
                                (a1["id"],)).fetchone()
            lose = conn.execute("SELECT * FROM articles WHERE id = ?",
                                (a2["id"],)).fetchone()
        finally:
            conn.close()
        return keep, lose

    def test_alias_repoints_and_removes_loser(self, initialized_library):
        keep, lose = self._two_articles(initialized_library)
        # Give the loser a highlight + tag so repointing is observable.
        from tiro.annotations import append_highlight, read_annotations, sidecar_stem
        conn = get_connection(initialized_library.db_path)
        try:
            body = frontmatter.load(str(
                initialized_library.articles_dir / lose["markdown_path"])).content
            hl_uid = append_highlight(
                initialized_library, conn, lose, quote="body",
                prefix="Some ", suffix=" text",
                position_start=body.index("body"),
                position_end=body.index("body") + 4,
                content_hash=content_hash(body), color="yellow",
                note_markdown="keep me")
            conn.execute("INSERT INTO tags (uid, name) VALUES (?, 'shared')",
                         (new_ulid(),))
            tag_id = conn.execute("SELECT id FROM tags WHERE name='shared'"
                                  ).fetchone()["id"]
            conn.execute("INSERT INTO article_tags (article_id, tag_id) "
                         "VALUES (?, ?)", (lose["id"], tag_id))
            conn.commit()
        finally:
            conn.close()

        clock = _clock()
        op = Alias(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                   uid=lose["uid"], new_uid=keep["uid"])
        report = apply_ops(initialized_library, [op])
        assert report.applied == 1 and report.errors == 0

        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute("SELECT 1 FROM articles WHERE uid = ?",
                                (lose["uid"],)).fetchone() is None
            tag_links = conn.execute(
                "SELECT article_id FROM article_tags WHERE tag_id = ?",
                (tag_id,)).fetchall()
            assert [r["article_id"] for r in tag_links] == [keep["id"]]
            hrow = conn.execute("SELECT article_id FROM highlights WHERE uid = ?",
                                (hl_uid,)).fetchone()
            assert hrow["article_id"] == keep["id"]
        finally:
            conn.close()
        # Sidecar lines moved to the survivor's stem with article_uid rewritten.
        keep_lines = read_annotations(initialized_library, sidecar_stem(keep))
        assert [ln["uid"] for ln in keep_lines] == [hl_uid]
        assert keep_lines[0]["article_uid"] == keep["uid"]
        assert keep_lines[0]["note_markdown"] == "keep me"

    def test_alias_heals_highlight_manifest_entries(self, initialized_library):
        """Mandate D: after an alias apply, the moved highlight's stale shadow
        row (fields still citing the OLD article_uid) is healed by the next
        build_manifest — its entries are computed from the on-disk sidecar
        line, which now carries the SURVIVOR's article_uid."""
        from tiro.annotations import append_highlight, sidecar_stem
        from tiro.sync.manifest import build_manifest, save_shadow

        keep, lose = self._two_articles(initialized_library)
        conn = get_connection(initialized_library.db_path)
        try:
            body = frontmatter.load(str(
                initialized_library.articles_dir / lose["markdown_path"])).content
            hl_uid = append_highlight(
                initialized_library, conn, lose, quote="body",
                prefix="Some ", suffix=" text",
                position_start=body.index("body"),
                position_end=body.index("body") + 4,
                content_hash=content_hash(body), color="yellow")
            conn.commit()
        finally:
            conn.close()
        # Persist a shadow citing the OLD article_uid, proving heal is real.
        save_shadow(initialized_library, build_manifest(initialized_library))

        clock = _clock()
        op = Alias(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                   uid=lose["uid"], new_uid=keep["uid"])
        report = apply_ops(initialized_library, [op])
        assert report.errors == 0

        m = build_manifest(initialized_library)
        entry = m.entries[("highlight", hl_uid)]
        assert entry.fields["article_uid"] == keep["uid"]
        assert entry.fields["path_hint"] == \
            f"annotations/{sidecar_stem(keep)}.jsonl"

    def test_alias_note_conflict_preserved_not_duplicated(self, initialized_library):
        """Mandate C: a differing loser article-level note becomes a conflict
        file; the survivor keeps its own note and never gains a SECOND
        article-level notes row."""
        from tiro.annotations import notes_dir, read_note, sidecar_stem

        keep, lose = self._two_articles(initialized_library)
        conn = get_connection(initialized_library.db_path)
        try:
            from tiro.annotations import write_note
            write_note(initialized_library, sidecar_stem(keep), "survivor note")
            write_note(initialized_library, sidecar_stem(lose), "loser note")
            now = "2026-07-15T00:00:00Z"
            for row, note in ((keep, "survivor note"), (lose, "loser note")):
                conn.execute(
                    "INSERT INTO notes (uid, article_id, highlight_id, "
                    "body_markdown, created_at, updated_at) "
                    "VALUES (?, ?, NULL, ?, ?, ?)",
                    (new_ulid(), row["id"], note, now, now))
            conn.commit()
        finally:
            conn.close()

        clock = _clock()
        op = Alias(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                   uid=lose["uid"], new_uid=keep["uid"])
        report = apply_ops(initialized_library, [op])
        assert report.applied == 1 and report.errors == 0
        assert report.conflicts == 1

        assert read_note(initialized_library, sidecar_stem(keep)) == "survivor note"
        conflicts = list(notes_dir(initialized_library).glob(
            f"{sidecar_stem(keep)}.conflict-*.md"))
        assert len(conflicts) == 1
        assert conflicts[0].read_text() == "loser note"
        conn = get_connection(initialized_library.db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM notes WHERE article_id = ? "
                "AND highlight_id IS NULL", (keep["id"],)).fetchone()["n"]
            assert n == 1
        finally:
            conn.close()

    def test_alias_moves_note_when_survivor_has_none(self, initialized_library):
        from tiro.annotations import read_note, sidecar_stem, write_note

        keep, lose = self._two_articles(initialized_library)
        conn = get_connection(initialized_library.db_path)
        try:
            write_note(initialized_library, sidecar_stem(lose), "traveling note")
            now = "2026-07-15T00:00:00Z"
            conn.execute(
                "INSERT INTO notes (uid, article_id, highlight_id, "
                "body_markdown, created_at, updated_at) VALUES (?, ?, NULL, ?, ?, ?)",
                (new_ulid(), lose["id"], "traveling note", now, now))
            conn.commit()
        finally:
            conn.close()

        clock = _clock()
        op = Alias(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                   uid=lose["uid"], new_uid=keep["uid"])
        report = apply_ops(initialized_library, [op])
        assert report.applied == 1 and report.errors == 0
        assert report.conflicts == 0
        assert read_note(initialized_library, sidecar_stem(keep)) == "traveling note"
        conn = get_connection(initialized_library.db_path)
        try:
            rows = conn.execute(
                "SELECT article_id FROM notes WHERE highlight_id IS NULL"
            ).fetchall()
            assert [r["article_id"] for r in rows] == [keep["id"]]
        finally:
            conn.close()

    def test_alias_with_unknown_new_uid_is_deferred_and_recorded(
            self, initialized_library):
        keep, lose = self._two_articles(initialized_library)
        clock = _clock()
        op = Alias(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                   uid=lose["uid"], new_uid="01NOTARRIVEDYET0000000000")
        report = apply_ops(initialized_library, [op])
        assert report.deferred == 1 and report.errors == 0
        # Mapping persisted so later ops for the old uid can re-target.
        from tiro.sync.manifest import load_shadow
        s = load_shadow(initialized_library)
        assert s.aliases.get(lose["uid"]) == "01NOTARRIVEDYET0000000000"

    def test_alias_idempotent(self, initialized_library):
        keep, lose = self._two_articles(initialized_library)
        clock = _clock()
        op = Alias(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                   uid=lose["uid"], new_uid=keep["uid"])
        apply_ops(initialized_library, [op])
        report2 = apply_ops(initialized_library, [op])
        assert report2.errors == 0  # already-applied alias is a clean no-op

    def test_deferred_alias_self_heals_when_survivor_arrives(
            self, initialized_library):
        """S2.7 review Nit 3: a deferred alias (survivor absent) converges
        later without consuming the mapping — when the survivor's own
        file_put arrives, URL dedupe fires against the still-present loser
        and the local article adopts the surviving uid (branch b)."""
        art = _ingest(initialized_library, url="https://example.com/hello")
        local_uid = _article_uid(initialized_library, art["id"])
        survivor_uid = "0" * 26  # lexicographically smaller than any real ULID
        clock = _clock()
        alias = Alias(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                      uid=local_uid, new_uid=survivor_uid)
        r1 = apply_ops(initialized_library, [alias])
        assert r1.deferred == 1

        # The survivor's article now arrives (same canonical URL).
        put = _fileput(survivor_uid, "articles/2026-07-01_survivor.md",
                       _remote_doc("https://example.com/hello?utm_source=x"),
                       clock=_clock(ms=2000000000000))
        r2 = apply_ops(initialized_library, [put])
        assert r2.errors == 0
        conn = get_connection(initialized_library.db_path)
        try:
            rows = conn.execute("SELECT uid FROM articles").fetchall()
        finally:
            conn.close()
        assert [r["uid"] for r in rows] == [survivor_uid]  # renamed, single row
        from tiro.sync.manifest import load_shadow
        assert load_shadow(initialized_library).aliases.get(local_uid) == \
            survivor_uid

    def test_alias_self_reference_is_an_error(self, initialized_library):
        keep, _lose = self._two_articles(initialized_library)
        clock = _clock()
        op = Alias(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                   uid=keep["uid"], new_uid=keep["uid"])
        report = apply_ops(initialized_library, [op])
        # Self-alias would repoint onto itself then delete the SURVIVOR —
        # refused as a per-op error, never applied.
        assert report.errors == 1 and report.applied == 0
        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute("SELECT 1 FROM articles WHERE uid = ?",
                                (keep["uid"],)).fetchone() is not None
        finally:
            conn.close()
