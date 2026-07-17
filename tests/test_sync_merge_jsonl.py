"""Sync S2: per-uid JSONL merge (spec §4 row 4) + line op application."""
import json

import pytest

from tiro.anchors import content_hash
from tiro.database import get_connection
from tiro.ingestion.processor import process_article
from tiro.migrations import new_ulid
from tiro.sync import reconcile as rec
from tiro.sync.journal import HLCClock, LineDel, LinePut, canonical_json
from tiro.sync.merge import apply_ops, merge_jsonl


@pytest.fixture(autouse=True)
def _fast_settle(monkeypatch):
    monkeypatch.setattr(rec, "SETTLE_SECONDS", 0.0)


def _line(uid="01H", note=None, updated="2026-07-10T00:00:00Z", **over):
    d = {"uid": uid, "article_uid": "01A", "quote": "bravo",
         "prefix": "alpha ", "suffix": " charlie",
         "position_start": 6, "position_end": 11,
         "content_hash": "d" * 64, "color": "yellow",
         "note_markdown": note,
         "created_at": "2026-07-10T00:00:00Z", "updated_at": updated}
    d.update(over)
    return d


class TestMergeJsonl:
    def test_set_union_of_distinct_uids(self):
        a, b = _line(uid="01H1"), _line(uid="01H2")
        merged = merge_jsonl([a], [b])
        assert {ln["uid"] for ln in merged} == {"01H1", "01H2"}

    def test_lww_by_updated_at(self):
        older = _line(color="yellow", updated="2026-07-10T00:00:00Z")
        newer = _line(color="pink", updated="2026-07-11T00:00:00Z")
        assert merge_jsonl([older], [newer])[0]["color"] == "pink"
        assert merge_jsonl([newer], [older])[0]["color"] == "pink"

    def test_commutative_and_idempotent(self):
        a = [_line(uid="01H1", note="na", updated="2026-07-10T01:00:00Z"),
             _line(uid="01H2")]
        b = [_line(uid="01H1", note="nb", updated="2026-07-10T02:00:00Z"),
             _line(uid="01H3")]
        ab = merge_jsonl(a, b, label_a="x", label_b="y")
        ba = merge_jsonl(b, a, label_a="y", label_b="x")
        assert ab == ba
        assert merge_jsonl(ab, ab) == ab
        assert merge_jsonl(a, a) == sorted(
            a, key=lambda ln: (ln.get("created_at") or "", ln["uid"]))

    def test_losing_note_preserved_as_conflict_blockquote(self):
        loser = _line(note="precious loser text",
                      updated="2026-07-10T00:00:00Z")
        winner = _line(note="winner text", color="pink",
                       updated="2026-07-11T00:00:00Z")
        (merged,) = merge_jsonl([loser], [winner],
                                label_a="laptop", label_b="phone")
        assert "winner text" in merged["note_markdown"]
        assert "precious loser text" in merged["note_markdown"]
        assert "[conflict 2026-07-10 laptop]" in merged["note_markdown"]

    def test_identical_notes_not_duplicated(self):
        a = _line(note="same", updated="2026-07-10T00:00:00Z")
        b = _line(note="same", color="pink", updated="2026-07-11T00:00:00Z")
        (merged,) = merge_jsonl([a], [b])
        assert merged["note_markdown"] == "same"

    def test_null_loser_note_adds_nothing(self):
        a = _line(note=None, updated="2026-07-10T00:00:00Z")
        b = _line(note="keep", updated="2026-07-11T00:00:00Z")
        (merged,) = merge_jsonl([a], [b])
        assert merged["note_markdown"] == "keep"

    def test_winner_without_note_inherits_losers(self):
        # No-note-loss even when the WINNING line has no note.
        loser = _line(note="only note text", updated="2026-07-10T00:00:00Z")
        winner = _line(note=None, color="pink", updated="2026-07-11T00:00:00Z")
        (merged,) = merge_jsonl([loser], [winner], label_a="a", label_b="b")
        assert "only note text" in (merged["note_markdown"] or "")

    def test_missing_updated_at_loses(self):
        a = _line(note="undated", updated=None)
        b = _line(note="dated", updated="2026-07-10T00:00:00Z")
        (merged,) = merge_jsonl([a], [b])
        assert merged["color"] == b["color"]
        assert "undated" in merged["note_markdown"]

    def test_equal_updated_at_ties_deterministically(self):
        a = _line(color="yellow", updated="2026-07-10T00:00:00Z")
        b = _line(color="pink", updated="2026-07-10T00:00:00Z")
        assert merge_jsonl([a], [b]) == merge_jsonl([b], [a])

    def test_output_sorted_stably(self):
        lines = [_line(uid="01H3", created_at="2026-07-12T00:00:00Z"),
                 _line(uid="01H1", created_at="2026-07-10T00:00:00Z"),
                 _line(uid="01H2", created_at="2026-07-11T00:00:00Z")]
        merged = merge_jsonl(lines, [])
        assert [ln["uid"] for ln in merged] == ["01H1", "01H2", "01H3"]


def _ingest(config, title="Hello World",
            body="# Hello\n\nalpha bravo charlie delta.",
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


class TestLineOps:
    def _seed(self, config):
        art = _ingest(config)
        row = _arow(config, art["id"])
        return art, row, row["markdown_path"].rsplit(".", 1)[0]

    def test_line_put_creates_sidecar_line_and_row(self, initialized_library):
        art, row, stem = self._seed(initialized_library)
        line = _line(uid=new_ulid(), article_uid=row["uid"])
        clock = _clock()
        op = LinePut(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                     uid=line["uid"], article_uid=row["uid"], line=line)
        report = apply_ops(initialized_library, [op])
        assert report.applied == 1
        from tiro.annotations import read_annotations
        lines = read_annotations(initialized_library, stem)
        assert [ln["uid"] for ln in lines] == [line["uid"]]
        conn = get_connection(initialized_library.db_path)
        try:
            hrow = conn.execute("SELECT * FROM highlights WHERE uid = ?",
                                (line["uid"],)).fetchone()
            assert hrow is not None and hrow["color"] == "yellow"
        finally:
            conn.close()

    def test_line_put_shadow_matches_on_disk_projection(self, initialized_library):
        """Mandates A+B: the shadow row for an apply-written highlight must
        carry exactly what build_manifest's _add_highlights would compute for
        the same sidecar line — hash over the RE-READ (projected) line, the
        re-read line itself in fields, and the annotations path_hint. A wire
        line with unknown/missing keys is where in-memory and on-disk
        genuinely diverge (_ordered_line drops unknowns, Nones missing)."""
        art, row, stem = self._seed(initialized_library)
        line = _line(uid=new_ulid(), article_uid=row["uid"])
        line["future_field"] = "unknown key, dropped by _FIELD_ORDER"
        del line["suffix"]  # missing key -> None on disk
        clock = _clock()
        op = LinePut(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                     uid=line["uid"], article_uid=row["uid"], line=line)
        report = apply_ops(initialized_library, [op])
        assert report.applied == 1 and report.errors == 0
        from tiro.annotations import read_annotations
        (disk_line,) = read_annotations(initialized_library, stem)
        assert "future_field" not in disk_line
        assert disk_line["suffix"] is None
        # In-memory line != on-disk projection here, by construction.
        assert content_hash(canonical_json(line)) != content_hash(
            canonical_json(disk_line))
        conn = get_connection(initialized_library.db_path)
        try:
            srow = conn.execute(
                "SELECT hash, fields_json FROM sync_shadow "
                "WHERE kind = 'highlight' AND uid = ?",
                (line["uid"],)).fetchone()
        finally:
            conn.close()
        assert srow["hash"] == content_hash(canonical_json(disk_line))
        fields = json.loads(srow["fields_json"])
        assert fields["path_hint"] == f"annotations/{stem}.jsonl"
        assert fields["line"] == disk_line
        assert fields["article_uid"] == row["uid"]

    def test_line_put_merges_with_existing_line(self, initialized_library):
        art, row, stem = self._seed(initialized_library)
        from tiro.annotations import append_highlight
        conn = get_connection(initialized_library.db_path)
        try:
            hl_uid = append_highlight(
                initialized_library, conn, row, quote="bravo",
                prefix="alpha ", suffix=" charlie",
                position_start=6, position_end=11,
                content_hash="d" * 64, color="yellow",
                note_markdown="local note",
                now="2026-07-11T00:00:00Z")
            conn.commit()
        finally:
            conn.close()
        remote = _line(uid=hl_uid, article_uid=row["uid"], note="remote note",
                       color="pink", updated="2026-07-10T00:00:00Z")  # OLDER
        clock = _clock()
        op = LinePut(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                     uid=hl_uid, article_uid=row["uid"], line=remote)
        apply_ops(initialized_library, [op])
        from tiro.annotations import read_annotations
        (line,) = read_annotations(initialized_library, stem)
        assert line["color"] == "yellow"           # local (newer) won
        assert "local note" in line["note_markdown"]
        assert "remote note" in line["note_markdown"]  # loser preserved
        conn = get_connection(initialized_library.db_path)
        try:
            nrow = conn.execute(
                "SELECT body_markdown FROM notes n JOIN highlights h "
                "ON h.id = n.highlight_id WHERE h.uid = ?", (hl_uid,)).fetchone()
            assert "remote note" in nrow["body_markdown"]  # row mirrors sidecar
        finally:
            conn.close()

    def test_line_put_unknown_article_deferred(self, initialized_library):
        line = _line(uid=new_ulid(), article_uid="01NOSUCHARTICLE")
        clock = _clock()
        op = LinePut(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                     uid=line["uid"], article_uid="01NOSUCHARTICLE", line=line)
        report = apply_ops(initialized_library, [op])
        assert report.deferred == 1 and report.errors == 0

    def test_line_del_clean(self, initialized_library):
        art, row, stem = self._seed(initialized_library)
        from tiro.annotations import append_highlight, read_annotations
        conn = get_connection(initialized_library.db_path)
        try:
            hl_uid = append_highlight(
                initialized_library, conn, row, quote="bravo",
                prefix="alpha ", suffix=" charlie", position_start=6,
                position_end=11, content_hash="d" * 64, color="yellow",
                now="2026-07-10T00:00:00Z")
            conn.commit()
        finally:
            conn.close()
        clock = _clock()
        op = LineDel(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                     uid=hl_uid, article_uid=row["uid"],
                     observed_updated_at="2026-07-10T00:00:00Z")
        report = apply_ops(initialized_library, [op])
        assert report.applied == 1 and report.tombstones == 1
        assert read_annotations(initialized_library, stem) == []
        conn = get_connection(initialized_library.db_path)
        try:
            assert conn.execute("SELECT 1 FROM highlights WHERE uid = ?",
                                (hl_uid,)).fetchone() is None
        finally:
            conn.close()

    def test_line_del_keeps_empty_sidecar_file(self, initialized_library):
        """Mandate C: deleting the last line keeps an EMPTY sidecar file
        (write_annotations([]) writes, never unlinks) — reconcile parses an
        empty file as zero lines, and the mass-delete guard counts its stem
        as present, so the empty file is inert and safe."""
        art, row, stem = self._seed(initialized_library)
        from tiro.annotations import annotations_dir, append_highlight
        conn = get_connection(initialized_library.db_path)
        try:
            hl_uid = append_highlight(
                initialized_library, conn, row, quote="bravo",
                prefix="alpha ", suffix=" charlie", position_start=6,
                position_end=11, content_hash="d" * 64, color="yellow",
                now="2026-07-10T00:00:00Z")
            conn.commit()
        finally:
            conn.close()
        clock = _clock()
        op = LineDel(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                     uid=hl_uid, article_uid=row["uid"],
                     observed_updated_at="2026-07-10T00:00:00Z")
        apply_ops(initialized_library, [op])
        sidecar = annotations_dir(initialized_library) / f"{stem}.jsonl"
        assert sidecar.exists() and sidecar.read_text() == ""

    def test_line_del_with_concurrent_note_edit_resurrects_note(
            self, initialized_library):
        """Spec §4: delete wins over concurrent edit EXCEPT a concurrent
        note_markdown edit resurrects as an article-level conflict note —
        user text is never destroyed by a race."""
        art, row, stem = self._seed(initialized_library)
        from tiro.annotations import append_highlight, read_annotations
        conn = get_connection(initialized_library.db_path)
        try:
            hl_uid = append_highlight(
                initialized_library, conn, row, quote="bravo",
                prefix="alpha ", suffix=" charlie", position_start=6,
                position_end=11, content_hash="d" * 64, color="yellow",
                note_markdown="edited after their delete observed",
                now="2026-07-11T00:00:00Z")  # NEWER than observed below
            conn.commit()
        finally:
            conn.close()
        clock = _clock()
        op = LineDel(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                     uid=hl_uid, article_uid=row["uid"],
                     observed_updated_at="2026-07-10T00:00:00Z")
        report = apply_ops(initialized_library, [op])
        assert report.tombstones == 1        # highlight still deleted
        assert report.resurrected == 1       # ...but the note text survives
        assert read_annotations(initialized_library, stem) == []
        conflicts = list((initialized_library.library / "notes").glob(
            f"{stem}.conflict-devb-*.md"))
        assert len(conflicts) == 1
        assert "edited after their delete observed" in conflicts[0].read_text()

    def test_line_del_missing_highlight_is_noop_tombstone(self, initialized_library):
        art, row, stem = self._seed(initialized_library)
        clock = _clock()
        op = LineDel(op_id=new_ulid(), hlc=clock.tick(), device="dev-b",
                     uid=new_ulid(), article_uid=row["uid"],
                     observed_updated_at=None)
        report = apply_ops(initialized_library, [op])
        assert report.errors == 0 and report.tombstones == 1
