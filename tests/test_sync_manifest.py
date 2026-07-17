"""Sync S2: build_manifest + shadow store (sync_shadow)."""
import json

import frontmatter

from tiro.anchors import content_hash
from tiro.database import get_connection
from tiro.ingestion.processor import process_article
from tiro.sync.journal import HLCClock
from tiro.sync.manifest import (
    LINK_TABLES,
    META_FIELDS,
    ROW_TABLES,
    build_manifest,
    expire_tombstones,
    load_shadow,
    save_shadow,
)


def _ingest(config, title="Hello World", body="# Hello\n\nSome body text.",
            url="https://example.com/hello"):
    return process_article(
        title=title, author="A. Writer", content_md=body, url=url, config=config,
    )


class TestBuildManifest:
    def test_article_entry_with_meta_fields(self, initialized_library):
        art = _ingest(initialized_library)
        conn = get_connection(initialized_library.db_path)
        try:
            row = conn.execute(
                "SELECT uid, markdown_path, body_hash FROM articles WHERE id = ?",
                (art["id"],),
            ).fetchone()
        finally:
            conn.close()
        m = build_manifest(initialized_library)
        entry = m.entries[("article", row["uid"])]
        assert entry.hash == row["body_hash"]
        assert entry.fields["path_hint"] == f"articles/{row['markdown_path']}"
        for f in META_FIELDS:
            assert f in entry.fields
        assert entry.fields["url"] == "https://example.com/hello"
        # source_uid resolves through the sources join (migration 015 uid)
        assert entry.fields["source_uid"]

    def test_note_and_highlight_entries(self, initialized_library):
        art = _ingest(initialized_library)
        from tiro.annotations import append_highlight, sidecar_stem, write_note
        conn = get_connection(initialized_library.db_path)
        try:
            arow = conn.execute(
                "SELECT * FROM articles WHERE id = ?", (art["id"],)).fetchone()
            body = frontmatter.load(
                str(initialized_library.articles_dir / arow["markdown_path"])).content
            start = body.index("body")
            hl_uid = append_highlight(
                initialized_library, conn, arow,
                quote="body", prefix=body[max(0, start - 8):start],
                suffix=body[start + 4:start + 12],
                position_start=start, position_end=start + 4,
                content_hash=content_hash(body), color="yellow",
                note_markdown="my note",
            )
            conn.commit()
        finally:
            conn.close()
        stem = sidecar_stem(arow)
        write_note(initialized_library, stem, "article-level note")

        m = build_manifest(initialized_library)
        hl = m.entries[("highlight", hl_uid)]
        assert hl.fields["article_uid"] == arow["uid"]
        assert hl.fields["line"]["note_markdown"] == "my note"
        note = m.entries[("note", arow["uid"])]
        assert note.hash == content_hash("article-level note")

    def test_row_link_and_pathfile_entries(self, initialized_library):
        art = _ingest(initialized_library)
        conn = get_connection(initialized_library.db_path)
        try:
            arow = conn.execute(
                "SELECT id, uid FROM articles WHERE id = ?", (art["id"],)).fetchone()
            from tiro.migrations import new_ulid
            conn.execute("INSERT INTO tags (uid, name) VALUES (?, 'ml')",
                         (new_ulid(),))
            tag = conn.execute("SELECT id, uid FROM tags WHERE name='ml'").fetchone()
            conn.execute(
                "INSERT INTO article_tags (article_id, tag_id) VALUES (?, ?)",
                (arow["id"], tag["id"]))
            conn.commit()
            src_uid = conn.execute("SELECT uid FROM sources LIMIT 1").fetchone()["uid"]
        finally:
            conn.close()
        (initialized_library.articles_dir /
         "x.conflict-local-20260710.md").write_text("loser body")

        m = build_manifest(initialized_library)
        assert ("row:tags", tag["uid"]) in m.entries
        assert ("row:sources", src_uid) in m.entries
        link = m.entries[("link:article_tags", f"{arow['uid']}:{tag['uid']}")]
        assert link.fields == {"a_uid": arow["uid"], "b_uid": tag["uid"]}
        pf = m.entries[("pathfile", "path:articles/x.conflict-local-20260710.md")]
        assert pf.hash == content_hash("loser body")

    def test_never_synced_stores_absent(self, initialized_library):
        _ingest(initialized_library)
        m = build_manifest(initialized_library)
        kinds = {k for (k, _uid) in m.entries}
        # spec §2 "never synced": no audio, sessions, auth, feeds, stats kinds.
        assert kinds <= (
            {"article", "note", "wiki", "pathfile", "highlight"}
            | {f"row:{t}" for t in ROW_TABLES}
            | {f"link:{t}" for t in LINK_TABLES}
        )

    def test_deterministic(self, initialized_library):
        _ingest(initialized_library)
        assert build_manifest(initialized_library).entries == \
            build_manifest(initialized_library).entries

    def test_null_body_hash_falls_back_to_stripped_body_hash(
            self, initialized_library):
        """Review Major #1: the NULL-body_hash disk fallback must hash the
        frontmatter-STRIPPED body (body_hash semantics everywhere else),
        never the raw file text."""
        art = _ingest(initialized_library)
        conn = get_connection(initialized_library.db_path)
        try:
            row = conn.execute(
                "SELECT uid, markdown_path FROM articles WHERE id = ?",
                (art["id"],)).fetchone()
            conn.execute("UPDATE articles SET body_hash = NULL WHERE id = ?",
                         (art["id"],))
            conn.commit()
        finally:
            conn.close()
        path = initialized_library.articles_dir / row["markdown_path"]
        expected = content_hash(frontmatter.load(str(path)).content)
        raw = content_hash(path.read_text())
        assert expected != raw  # the two hash spaces genuinely differ
        entry = build_manifest(initialized_library).entries[
            ("article", row["uid"])]
        assert entry.hash == expected


class TestShadowStore:
    def test_save_load_roundtrip(self, initialized_library):
        _ingest(initialized_library)
        m = build_manifest(initialized_library)
        clock = HLCClock("dev-a", now_ms=lambda: 1720000000000)
        save_shadow(initialized_library, m, clock=clock)
        s = load_shadow(initialized_library)
        assert set(s.entries) == set(m.entries)
        for key, entry in m.entries.items():
            assert s.entries[key].hash == entry.hash
            assert s.entries[key].fields == entry.fields
            assert s.entries[key].hlc is not None

    def test_missing_entries_become_tombstones(self, initialized_library):
        art = _ingest(initialized_library)
        m = build_manifest(initialized_library)
        save_shadow(initialized_library, m,
                    clock=HLCClock("dev-a", now_ms=lambda: 1))
        from tiro.lifecycle import delete_article
        delete_article(initialized_library, art["id"])
        m2 = build_manifest(initialized_library)
        save_shadow(initialized_library, m2,
                    clock=HLCClock("dev-a", now_ms=lambda: 2))
        s = load_shadow(initialized_library)
        tomb_kinds = {k for (k, _u) in s.tombstones}
        assert "article" in tomb_kinds
        assert ("article",) not in {(k,) for (k, _u) in s.entries}

    def test_unreadable_file_not_tombstoned(self, initialized_library):
        """Review Major #2: a file that EXISTS but cannot be read (perm
        blip, non-UTF-8 bytes, iCloud lazy materialization) must never be
        treated as deleted — save_shadow carries its entry forward."""
        art = _ingest(initialized_library)
        from tiro.annotations import sidecar_stem, write_note
        conn = get_connection(initialized_library.db_path)
        try:
            arow = conn.execute("SELECT * FROM articles WHERE id = ?",
                                (art["id"],)).fetchone()
        finally:
            conn.close()
        stem = sidecar_stem(arow)
        write_note(initialized_library, stem, "precious note")
        m1 = build_manifest(initialized_library)
        note_key = ("note", arow["uid"])
        assert note_key in m1.entries
        save_shadow(initialized_library, m1,
                    clock=HLCClock("dev-a", now_ms=lambda: 1))

        note_path = initialized_library.library / "notes" / f"{stem}.md"
        note_path.write_bytes(b"\xff\xfe\x00 not utf-8 \xff")  # undecodable
        m2 = build_manifest(initialized_library)
        assert note_key not in m2.entries
        assert f"notes/{stem}.md" in m2.unreadable
        save_shadow(initialized_library, m2,
                    clock=HLCClock("dev-a", now_ms=lambda: 2))
        s = load_shadow(initialized_library)
        assert note_key in s.entries          # carried forward, still live
        assert note_key not in s.tombstones   # NOT propagated as a delete
        assert s.entries[note_key].hash == m1.entries[note_key].hash

    def test_corrupt_sidecar_highlights_not_tombstoned(
            self, initialized_library):
        """S2.3 review Major #2: a sidecar whose lines are corrupt-but-
        readable (truncation, partial materialization — spec §10) must not
        report its highlights as deleted: the file is marked unreadable,
        diff emits no LineDel, save_shadow keeps the shadow rows live."""
        art = _ingest(initialized_library)
        from tiro.annotations import append_highlight, sidecar_stem
        conn = get_connection(initialized_library.db_path)
        try:
            arow = conn.execute("SELECT * FROM articles WHERE id = ?",
                                (art["id"],)).fetchone()
            body = frontmatter.load(str(
                initialized_library.articles_dir /
                arow["markdown_path"])).content
            start = body.index("body")
            hl_uid = append_highlight(
                initialized_library, conn, arow, quote="body",
                prefix=body[max(0, start - 8):start],
                suffix=body[start + 4:start + 12],
                position_start=start, position_end=start + 4,
                content_hash=content_hash(body), color="yellow",
                note_markdown="precious highlight note")
            conn.commit()
        finally:
            conn.close()
        stem = sidecar_stem(arow)
        m1 = build_manifest(initialized_library)
        hl_key = ("highlight", hl_uid)
        assert hl_key in m1.entries
        assert m1.entries[hl_key].fields["path_hint"] == \
            f"annotations/{stem}.jsonl"
        save_shadow(initialized_library, m1,
                    clock=HLCClock("dev-a", now_ms=lambda: 1))

        # Truncation: the valid line replaced by garbage (still utf-8).
        sidecar = (initialized_library.library / "annotations" /
                   f"{stem}.jsonl")
        sidecar.write_text('{"uid": "01TRUNC\n')
        m2 = build_manifest(initialized_library)
        assert hl_key not in m2.entries
        assert f"annotations/{stem}.jsonl" in m2.unreadable

        from tiro.sync.manifest import diff
        ops = diff(m2, load_shadow(initialized_library),
                   clock=HLCClock("dev-a", now_ms=lambda: 2))
        assert not any(type(o).kind == "line_del" for o in ops)

        save_shadow(initialized_library, m2,
                    clock=HLCClock("dev-a", now_ms=lambda: 3))
        s = load_shadow(initialized_library)
        assert hl_key in s.entries          # carried forward, still live
        assert hl_key not in s.tombstones   # NOT propagated as a delete

    def test_save_shadow_keeps_hlc_for_unchanged_entries(
            self, initialized_library):
        """Monotone shadow: re-saving an unchanged manifest must NOT restamp
        hlc values; a genuinely changed entry advances alone."""
        art = _ingest(initialized_library)
        m1 = build_manifest(initialized_library)
        save_shadow(initialized_library, m1,
                    clock=HLCClock("dev-a", now_ms=lambda: 1))
        before = {k: e.hlc for k, e in load_shadow(
            initialized_library).entries.items()}

        save_shadow(initialized_library, build_manifest(initialized_library),
                    clock=HLCClock("dev-a", now_ms=lambda: 2))
        after_noop = {k: e.hlc for k, e in load_shadow(
            initialized_library).entries.items()}
        assert after_noop == before

        conn = get_connection(initialized_library.db_path)
        try:
            uid = conn.execute("SELECT uid FROM articles WHERE id = ?",
                               (art["id"],)).fetchone()["uid"]
            conn.execute("UPDATE articles SET rating = 2 WHERE id = ?",
                         (art["id"],))
            conn.commit()
        finally:
            conn.close()
        save_shadow(initialized_library, build_manifest(initialized_library),
                    clock=HLCClock("dev-a", now_ms=lambda: 3))
        after_change = {k: e.hlc for k, e in load_shadow(
            initialized_library).entries.items()}
        changed = {k for k in before if after_change[k] != before[k]}
        assert changed == {("article", uid)}

    def test_expire_tombstones_purges_only_old(self, initialized_library):
        conn = get_connection(initialized_library.db_path)
        try:
            conn.execute(
                "INSERT INTO sync_shadow (kind, uid, fields_json, deleted_at) "
                "VALUES ('article', 'OLD', '{}', '2020-01-01T00:00:00Z')")
            conn.execute(
                "INSERT INTO sync_shadow (kind, uid, fields_json, deleted_at) "
                "VALUES ('article', 'NEW', '{}', '2099-01-01T00:00:00Z')")
            # alias rows are exempt from TTL (plan decision #18)
            conn.execute(
                "INSERT INTO sync_shadow (kind, uid, fields_json, deleted_at) "
                "VALUES ('alias', 'A', ?, '2020-01-01T00:00:00Z')",
                (json.dumps({"new_uid": "B"}),))
            conn.commit()
        finally:
            conn.close()
        purged = expire_tombstones(initialized_library)
        assert purged == 1
        s = load_shadow(initialized_library)
        assert ("article", "NEW") in s.tombstones
        assert ("article", "OLD") not in s.tombstones
        assert s.aliases == {"A": "B"}
