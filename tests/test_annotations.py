"""Highlights + notes sidecar store: files-as-truth (tiro/annotations.py)."""

import json

import pytest

from tiro.annotations import (
    annotations_dir,
    delete_note,
    notes_dir,
    read_annotations,
    read_note,
    rebuild_sidecars_for_article,
    reconcile_annotations,
    sidecar_stem,
    write_annotations,
    write_note,
)
from tiro.database import get_connection, init_db
from tiro.migrations import new_ulid

# --- seeding helpers ---------------------------------------------------------


def _seed_article(config, stem="article-1", uid=None, title="T"):
    conn = get_connection(config.db_path)
    try:
        conn.execute("INSERT INTO sources (name, source_type) VALUES ('s', 'web')")
        article_uid = uid or new_ulid()
        conn.execute(
            "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
            " VALUES (?, last_insert_rowid(), ?, ?, ?)",
            (article_uid, title, stem, f"{stem}.md"),
        )
        article_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
        return article_id, article_uid
    finally:
        conn.close()


def _seed_highlight(config, article_id, uid=None, quote="hello world", color="yellow"):
    conn = get_connection(config.db_path)
    try:
        h_uid = uid or new_ulid()
        conn.execute(
            """INSERT INTO highlights
               (uid, article_id, quote_text, prefix_context, suffix_context,
                text_position_start, text_position_end, content_hash, color,
                created_at, updated_at)
               VALUES (?, ?, ?, 'pre', 'suf', 0, 11, 'hash1', ?, '2026-01-01T00:00:00Z',
                       '2026-01-01T00:00:00Z')""",
            (h_uid, article_id, quote, color),
        )
        highlight_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
        return highlight_id, h_uid
    finally:
        conn.close()


def _seed_note(config, article_id, highlight_id=None, body="a note", uid=None):
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            """INSERT INTO notes (uid, article_id, highlight_id, body_markdown,
               created_at, updated_at)
               VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
            (uid or new_ulid(), article_id, highlight_id, body),
        )
        note_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
        return note_id
    finally:
        conn.close()


@pytest.fixture
def db_config(test_config):
    """A TiroConfig with an initialized SQLite DB, no ChromaDB/vectorstore --
    annotations.py never touches vectors, so the lighter fixture keeps these
    tests fast."""
    init_db(test_config.db_path)
    return test_config


# --- annotations_dir / notes_dir ---------------------------------------------


def test_annotations_dir_and_notes_dir_paths(db_config):
    assert annotations_dir(db_config) == db_config.library / "annotations"
    assert notes_dir(db_config) == db_config.library / "notes"


# --- sidecar_stem -------------------------------------------------------------


def test_sidecar_stem_from_mapping():
    assert sidecar_stem({"markdown_path": "my-article.md"}) == "my-article"


def test_sidecar_stem_from_sqlite_row(db_config):
    aid, _ = _seed_article(db_config, stem="row-article")
    conn = get_connection(db_config.db_path)
    try:
        row = conn.execute("SELECT * FROM articles WHERE id = ?", (aid,)).fetchone()
    finally:
        conn.close()
    assert sidecar_stem(row) == "row-article"


def test_sidecar_stem_from_string_and_path():
    from pathlib import Path

    assert sidecar_stem("foo.md") == "foo"
    assert sidecar_stem(Path("dir/foo.md")) == "foo"


# --- round trip: annotations --------------------------------------------------


def test_write_then_read_annotations_round_trip(db_config):
    lines = [
        {
            "uid": "H1",
            "article_uid": "A1",
            "quote": "hello",
            "prefix": "pre",
            "suffix": "suf",
            "position_start": 0,
            "position_end": 5,
            "content_hash": "abc",
            "color": "yellow",
            "note_markdown": "a note",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
        {"uid": "H2", "article_uid": "A1", "quote": "world"},
    ]
    write_annotations(db_config, "stem-a", lines)

    result = read_annotations(db_config, "stem-a")
    assert len(result) == 2
    assert result[0]["uid"] == "H1"
    assert result[0]["note_markdown"] == "a note"
    assert result[1]["uid"] == "H2"
    # Fields omitted by the caller come back as None (stable schema).
    assert result[1]["note_markdown"] is None
    assert result[1]["content_hash"] is None


def test_write_annotations_enforces_stable_field_order(db_config):
    # Deliberately out-of-order + with an unknown extra key.
    write_annotations(
        db_config,
        "stem-b",
        [{"color": "blue", "uid": "H1", "bogus": "dropped", "quote": "q"}],
    )
    path = annotations_dir(db_config) / "stem-b.jsonl"
    raw_line = path.read_text().splitlines()[0]
    keys = list(json.loads(raw_line, object_pairs_hook=lambda pairs: [k for k, _ in pairs]))
    assert keys == [
        "uid", "article_uid", "quote", "prefix", "suffix", "position_start",
        "position_end", "content_hash", "color", "note_markdown", "created_at",
        "updated_at",
    ]
    assert "bogus" not in raw_line


def test_read_annotations_missing_file_returns_empty_list(db_config):
    assert read_annotations(db_config, "does-not-exist") == []


# --- round trip: notes --------------------------------------------------------


def test_write_read_delete_note_round_trip(db_config):
    assert read_note(db_config, "stem-c") is None
    write_note(db_config, "stem-c", "# My note\n\nBody text.")
    assert read_note(db_config, "stem-c") == "# My note\n\nBody text."
    delete_note(db_config, "stem-c")
    assert read_note(db_config, "stem-c") is None
    # Deleting an already-absent note is a no-op, not an error.
    delete_note(db_config, "stem-c")


# --- rebuild_sidecars_for_article (index -> files) ---------------------------


def test_rebuild_sidecars_for_article_writes_both_kinds(db_config):
    aid, article_uid = _seed_article(db_config, stem="rebuild-me")
    hid, h_uid = _seed_highlight(db_config, aid, quote="quoted text")
    _seed_note(db_config, aid, highlight_id=hid, body="highlight note")
    _seed_note(db_config, aid, highlight_id=None, body="article-level note")

    rebuild_sidecars_for_article(db_config, aid)

    lines = read_annotations(db_config, "rebuild-me")
    assert len(lines) == 1
    assert lines[0]["uid"] == h_uid
    assert lines[0]["article_uid"] == article_uid
    assert lines[0]["quote"] == "quoted text"
    assert lines[0]["note_markdown"] == "highlight note"

    assert read_note(db_config, "rebuild-me") == "article-level note"


def test_rebuild_sidecars_for_article_no_highlights_no_file(db_config):
    aid, _ = _seed_article(db_config, stem="empty-article")
    rebuild_sidecars_for_article(db_config, aid)
    assert not (annotations_dir(db_config) / "empty-article.jsonl").exists()
    assert read_note(db_config, "empty-article") is None


def test_rebuild_sidecars_for_article_unknown_id_raises(db_config):
    with pytest.raises(ValueError):
        rebuild_sidecars_for_article(db_config, 999999)


# --- reconcile_annotations: files-win insert ---------------------------------


def test_reconcile_inserts_highlight_from_new_file(db_config):
    aid, article_uid = _seed_article(db_config, stem="insert-me")
    write_annotations(
        db_config,
        "insert-me",
        [
            {
                "uid": "H-NEW",
                "article_uid": article_uid,
                "quote": "brand new quote",
                "prefix": "p",
                "suffix": "s",
                "position_start": 1,
                "position_end": 20,
                "content_hash": "hash-x",
                "color": "green",
                "note_markdown": "inserted note",
                "created_at": "2026-02-01T00:00:00Z",
                "updated_at": "2026-02-01T00:00:00Z",
            }
        ],
    )

    counts = reconcile_annotations(db_config)
    assert counts["highlights_inserted"] == 1
    assert counts["notes_inserted"] == 1

    conn = get_connection(db_config.db_path)
    try:
        row = conn.execute("SELECT * FROM highlights WHERE uid = ?", ("H-NEW",)).fetchone()
        assert row is not None
        assert row["article_id"] == aid
        assert row["quote_text"] == "brand new quote"
        assert row["color"] == "green"
        note = conn.execute(
            "SELECT * FROM notes WHERE highlight_id = ?", (row["id"],)
        ).fetchone()
        assert note["body_markdown"] == "inserted note"
    finally:
        conn.close()


# --- reconcile_annotations: files-win update ---------------------------------


def test_reconcile_updates_drifted_highlight_to_match_file(db_config):
    aid, article_uid = _seed_article(db_config, stem="update-me")
    _hid, h_uid = _seed_highlight(db_config, aid, uid="H-DRIFT", quote="old quote", color="yellow")

    write_annotations(
        db_config,
        "update-me",
        [
            {
                "uid": "H-DRIFT",
                "article_uid": article_uid,
                "quote": "NEW quote text",
                "prefix": "pre",
                "suffix": "suf",
                "position_start": 0,
                "position_end": 11,
                "content_hash": "hash1",
                "color": "pink",
            }
        ],
    )

    counts = reconcile_annotations(db_config)
    assert counts["highlights_updated"] == 1
    assert counts["highlights_inserted"] == 0

    conn = get_connection(db_config.db_path)
    try:
        row = conn.execute("SELECT * FROM highlights WHERE uid = ?", ("H-DRIFT",)).fetchone()
        assert row["quote_text"] == "NEW quote text"
        assert row["color"] == "pink"
    finally:
        conn.close()


def test_reconcile_matches_unchanged_highlight_without_update(db_config):
    aid, article_uid = _seed_article(db_config, stem="same-stem")
    _seed_highlight(db_config, aid, uid="H-SAME", quote="hello world", color="yellow")

    write_annotations(
        db_config,
        "same-stem",
        [
            {
                "uid": "H-SAME",
                "article_uid": article_uid,
                "quote": "hello world",
                "prefix": "pre",
                "suffix": "suf",
                "position_start": 0,
                "position_end": 11,
                "content_hash": "hash1",
                "color": "yellow",
            }
        ],
    )

    counts = reconcile_annotations(db_config)
    assert counts["highlights_matched"] == 1
    assert counts["highlights_updated"] == 0
    assert counts["highlights_inserted"] == 0


# --- reconcile_annotations: files-win delete ---------------------------------


def test_reconcile_deletes_highlight_and_note_vanished_from_file(db_config):
    aid, article_uid = _seed_article(db_config, stem="delete-me")
    hid, h_uid = _seed_highlight(db_config, aid, uid="H-GONE")
    _seed_note(db_config, aid, highlight_id=hid, body="will be deleted")

    # File exists for this stem but no longer contains H-GONE.
    write_annotations(db_config, "delete-me", [])

    counts = reconcile_annotations(db_config)
    assert counts["highlights_deleted"] == 1
    assert counts["notes_deleted"] == 1

    conn = get_connection(db_config.db_path)
    try:
        assert conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-GONE",)
        ).fetchone() is None
        assert conn.execute(
            "SELECT * FROM notes WHERE highlight_id = ?", (hid,)
        ).fetchone() is None
    finally:
        conn.close()


def test_reconcile_deletes_rows_when_article_has_no_sidecar_file_at_all(db_config):
    aid, _ = _seed_article(db_config, stem="no-file-at-all")
    hid, _ = _seed_highlight(db_config, aid, uid="H-ORPHAN-ROW")
    _seed_note(db_config, aid, highlight_id=hid, body="also gone")

    # annotations/ dir exists (another article has a file in it) but this
    # article's stem never had a file written for it.
    other_aid, other_uid = _seed_article(db_config, stem="other")
    write_annotations(db_config, "other", [])

    counts = reconcile_annotations(db_config)
    assert counts["highlights_deleted"] == 1
    assert counts["notes_deleted"] == 1

    conn = get_connection(db_config.db_path)
    try:
        assert conn.execute(
            "SELECT * FROM highlights WHERE article_id = ?", (aid,)
        ).fetchone() is None
    finally:
        conn.close()


def test_reconcile_article_level_note_insert_update_delete(db_config):
    aid, _ = _seed_article(db_config, stem="note-stem")

    # insert: file exists, no row.
    write_note(db_config, "note-stem", "first version")
    counts = reconcile_annotations(db_config)
    assert counts["notes_inserted"] == 1

    conn = get_connection(db_config.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM notes WHERE article_id = ? AND highlight_id IS NULL", (aid,)
        ).fetchone()
        assert row["body_markdown"] == "first version"
        original_uid = row["uid"]
    finally:
        conn.close()

    # update: file content changed.
    write_note(db_config, "note-stem", "second version")
    counts = reconcile_annotations(db_config)
    assert counts["notes_updated"] == 1

    conn = get_connection(db_config.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM notes WHERE article_id = ? AND highlight_id IS NULL", (aid,)
        ).fetchone()
        assert row["body_markdown"] == "second version"
        # uid stable across an update.
        assert row["uid"] == original_uid
    finally:
        conn.close()

    # delete: file removed.
    delete_note(db_config, "note-stem")
    counts = reconcile_annotations(db_config)
    assert counts["notes_deleted"] == 1

    conn = get_connection(db_config.db_path)
    try:
        assert conn.execute(
            "SELECT * FROM notes WHERE article_id = ? AND highlight_id IS NULL", (aid,)
        ).fetchone() is None
    finally:
        conn.close()


# --- reconcile_annotations: orphan move ---------------------------------------


def test_reconcile_moves_orphaned_annotation_file(db_config):
    ann_dir = annotations_dir(db_config)
    ann_dir.mkdir(parents=True)
    (ann_dir / "unknown-stem.jsonl").write_text('{"uid": "X"}\n')

    counts = reconcile_annotations(db_config)
    assert counts["orphaned_files"] == 1
    assert not (ann_dir / "unknown-stem.jsonl").exists()
    assert (db_config.library / ".orphaned" / "unknown-stem.jsonl").exists()


def test_reconcile_moves_orphaned_note_file(db_config):
    nt_dir = notes_dir(db_config)
    nt_dir.mkdir(parents=True)
    (nt_dir / "unknown-stem.md").write_text("orphan body")

    counts = reconcile_annotations(db_config)
    assert counts["orphaned_files"] == 1
    assert not (nt_dir / "unknown-stem.md").exists()
    assert (db_config.library / ".orphaned" / "unknown-stem.md").exists()


def test_reconcile_orphan_move_is_collision_safe(db_config):
    orphan_dir = db_config.library / ".orphaned"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "dup.jsonl").write_text("pre-existing")

    ann_dir = annotations_dir(db_config)
    ann_dir.mkdir(parents=True)
    (ann_dir / "dup.jsonl").write_text('{"uid": "X"}\n')

    reconcile_annotations(db_config)

    assert (orphan_dir / "dup.jsonl").read_text() == "pre-existing"
    assert (orphan_dir / "dup.1.jsonl").exists()


# --- reconcile_annotations: malformed lines -----------------------------------


def test_reconcile_skips_malformed_lines_without_crashing_or_rewriting_file(db_config):
    aid, article_uid = _seed_article(db_config, stem="malformed-stem")
    ann_dir = annotations_dir(db_config)
    ann_dir.mkdir(parents=True)
    path = ann_dir / "malformed-stem.jsonl"
    raw = (
        '{"uid": "H-OK", "article_uid": "' + article_uid + '", "quote": "ok"}\n'
        "not json at all\n"
        '{"quote": "missing uid field"}\n'
    )
    path.write_text(raw)
    before = path.read_bytes()

    counts = reconcile_annotations(db_config)

    assert counts["malformed_lines"] == 2
    assert counts["highlights_inserted"] == 1
    assert path.read_bytes() == before  # never rewritten


def test_read_annotations_also_skips_malformed_lines(db_config):
    ann_dir = annotations_dir(db_config)
    ann_dir.mkdir(parents=True)
    (ann_dir / "malformed-read.jsonl").write_text(
        '{"uid": "H1", "quote": "ok"}\nnot json\n{"quote": "no uid"}\n'
    )
    result = read_annotations(db_config, "malformed-read")
    assert len(result) == 1
    assert result[0]["uid"] == "H1"


# --- reconcile_annotations: quote-less lines (final-review finding 1a) -------
# A line with a uid but no quote (or an empty one) used to sail past
# _parse_jsonl_lines' validity check (which only required uid) and then
# crash the whole reconcile with a NOT NULL constraint failure on
# highlights.quote_text. It must now be treated as malformed instead.


def test_reconcile_treats_quote_less_line_as_malformed_not_a_crash(db_config):
    aid, article_uid = _seed_article(db_config, stem="no-quote-stem")
    ann_dir = annotations_dir(db_config)
    ann_dir.mkdir(parents=True)
    path = ann_dir / "no-quote-stem.jsonl"
    raw = (
        '{"uid": "H-OK", "article_uid": "' + article_uid + '", "quote": "ok"}\n'
        '{"uid": "H-NO-QUOTE", "article_uid": "' + article_uid + '"}\n'
        '{"uid": "H-EMPTY-QUOTE", "article_uid": "' + article_uid + '", "quote": ""}\n'
    )
    path.write_text(raw)

    counts = reconcile_annotations(db_config)  # must not raise

    assert counts["malformed_lines"] == 2
    assert counts["highlights_inserted"] == 1

    conn = get_connection(db_config.db_path)
    try:
        assert conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-OK",)
        ).fetchone() is not None
        assert conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-NO-QUOTE",)
        ).fetchone() is None
        assert conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-EMPTY-QUOTE",)
        ).fetchone() is None
    finally:
        conn.close()


def test_read_annotations_also_skips_quote_less_lines(db_config):
    ann_dir = annotations_dir(db_config)
    ann_dir.mkdir(parents=True)
    (ann_dir / "no-quote-read.jsonl").write_text(
        '{"uid": "H1", "quote": "ok"}\n{"uid": "H2"}\n{"uid": "H3", "quote": ""}\n'
    )
    result = read_annotations(db_config, "no-quote-read")
    assert len(result) == 1
    assert result[0]["uid"] == "H1"


# --- reconcile_annotations: duplicate uid (final-review finding 1b/1c) -------
# A JSONL line whose uid duplicates another line's -- same file, or across
# two different files in one reconcile run -- used to crash the whole
# reconcile with "UNIQUE constraint failed: highlights.uid" on the second
# INSERT, rolling back every other article's healing too. Must now be
# skipped + counted + logged, never raised.


def test_reconcile_skips_duplicate_uid_within_one_file(db_config):
    aid, article_uid = _seed_article(db_config, stem="dup-in-file-stem")
    ann_dir = annotations_dir(db_config)
    ann_dir.mkdir(parents=True)
    path = ann_dir / "dup-in-file-stem.jsonl"
    raw = (
        '{"uid": "H-DUP", "article_uid": "' + article_uid + '", "quote": "first"}\n'
        '{"uid": "H-OK", "article_uid": "' + article_uid + '", "quote": "ok"}\n'
        '{"uid": "H-DUP", "article_uid": "' + article_uid + '", "quote": "second"}\n'
    )
    path.write_text(raw)

    counts = reconcile_annotations(db_config)  # must not raise

    assert counts["duplicate_uid_lines"] == 1
    assert counts["highlights_inserted"] == 2  # H-DUP (first occurrence) + H-OK

    conn = get_connection(db_config.db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-DUP",)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["quote_text"] == "first"  # first occurrence wins
        assert conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-OK",)
        ).fetchone() is not None
    finally:
        conn.close()


def test_reconcile_skips_duplicate_uid_across_two_articles_files(db_config):
    """Per-file `seen_uids` dedup can't see across files -- this exercises
    the sqlite3.IntegrityError fallback for a uid collision between two
    DIFFERENT articles' sidecars in the same reconcile run."""
    aid1, uid1 = _seed_article(db_config, stem="cross-file-a")
    aid2, uid2 = _seed_article(db_config, stem="cross-file-b")
    write_annotations(
        db_config,
        "cross-file-a",
        [{"uid": "H-CROSS-DUP", "article_uid": uid1, "quote": "from a"}],
    )
    write_annotations(
        db_config,
        "cross-file-b",
        [
            {"uid": "H-CROSS-DUP", "article_uid": uid2, "quote": "from b"},
            {"uid": "H-B-ONLY", "article_uid": uid2, "quote": "b only"},
        ],
    )

    counts = reconcile_annotations(db_config)  # must not raise, must not abort

    assert counts["duplicate_uid_lines"] >= 1
    # The rest of the run must still heal: H-B-ONLY (processed alongside the
    # colliding line in the same file) must still be indexed.
    conn = get_connection(db_config.db_path)
    try:
        assert conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-B-ONLY",)
        ).fetchone() is not None
        # Exactly one row exists for the colliding uid (whichever file's
        # sidecar was processed first, files are processed in sorted stem
        # order so cross-file-a wins).
        rows = conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-CROSS-DUP",)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["article_id"] == aid1
    finally:
        conn.close()


def test_reconcile_duplicate_uid_across_files_does_not_abort_other_articles(db_config):
    """The whole-transaction-rollback blast radius from the finding: a
    cross-file uid collision must not prevent an UNRELATED third article's
    drift from healing in the same run."""
    aid1, uid1 = _seed_article(db_config, stem="cross-abort-a")
    aid2, uid2 = _seed_article(db_config, stem="cross-abort-b")
    aid3, uid3 = _seed_article(db_config, stem="cross-abort-c")
    write_annotations(
        db_config,
        "cross-abort-a",
        [{"uid": "H-ABORT-DUP", "article_uid": uid1, "quote": "from a"}],
    )
    write_annotations(
        db_config,
        "cross-abort-b",
        [{"uid": "H-ABORT-DUP", "article_uid": uid2, "quote": "from b"}],
    )
    write_annotations(
        db_config,
        "cross-abort-c",
        [{"uid": "H-UNRELATED", "article_uid": uid3, "quote": "unrelated"}],
    )

    counts = reconcile_annotations(db_config)  # must not raise

    assert counts["duplicate_uid_lines"] >= 1
    conn = get_connection(db_config.db_path)
    try:
        assert conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-UNRELATED",)
        ).fetchone() is not None
    finally:
        conn.close()


# --- reconcile_annotations: missing-dir mass-deletion guard -------------------


def test_reconcile_guards_annotations_dir_missing_with_rows(db_config):
    aid, _ = _seed_article(db_config, stem="guarded-stem")
    _seed_highlight(db_config, aid, uid="H-GUARDED")
    assert not annotations_dir(db_config).exists()

    counts = reconcile_annotations(db_config)

    assert counts["guarded"] >= 1
    assert counts["highlights_deleted"] == 0

    conn = get_connection(db_config.db_path)
    try:
        assert conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-GUARDED",)
        ).fetchone() is not None
    finally:
        conn.close()
    assert not annotations_dir(db_config).exists()


def test_reconcile_guards_notes_dir_missing_with_rows(db_config):
    aid, _ = _seed_article(db_config, stem="guarded-note-stem")
    _seed_note(db_config, aid, highlight_id=None, body="do not delete me")
    assert not notes_dir(db_config).exists()

    counts = reconcile_annotations(db_config)

    assert counts["guarded"] >= 1
    assert counts["notes_deleted"] == 0

    conn = get_connection(db_config.db_path)
    try:
        assert conn.execute(
            "SELECT * FROM notes WHERE article_id = ? AND highlight_id IS NULL", (aid,)
        ).fetchone() is not None
    finally:
        conn.close()


# --- reconcile_annotations: widened guard (dir EXISTS but empty) -------------
# Reviewer finding 1 (CRITICAL): the guard above only caught a fully-missing
# directory. `rm -rf annotations/*` / a botched restore leaves the directory
# present but with zero matching files, which the old code treated as "every
# article legitimately lost its sidecar" and mass-deleted. These tests pin
# the widened guard: present-but-empty-for->1-article is now guarded too,
# single-article libraries still get ordinary files-win deletion, and the
# guard must not overfire when only SOME files are missing.


def test_reconcile_guards_annotations_dir_exists_but_empty_for_multiple_articles(db_config):
    aid1, _ = _seed_article(db_config, stem="empty-dir-highlights-1")
    _seed_highlight(db_config, aid1, uid="H-EMPTY-DIR-1")
    aid2, _ = _seed_article(db_config, stem="empty-dir-highlights-2")
    _seed_highlight(db_config, aid2, uid="H-EMPTY-DIR-2")

    ann_dir = annotations_dir(db_config)
    ann_dir.mkdir(parents=True)  # dir exists, zero sidecar files in it

    counts = reconcile_annotations(db_config)

    assert counts["guarded"] >= 1
    assert counts["highlights_deleted"] == 0

    conn = get_connection(db_config.db_path)
    try:
        assert conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-EMPTY-DIR-1",)
        ).fetchone() is not None
        assert conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-EMPTY-DIR-2",)
        ).fetchone() is not None
    finally:
        conn.close()


def test_reconcile_guards_notes_dir_exists_but_empty_for_multiple_articles(db_config):
    aid1, _ = _seed_article(db_config, stem="empty-dir-notes-1")
    _seed_note(db_config, aid1, highlight_id=None, body="note 1")
    aid2, _ = _seed_article(db_config, stem="empty-dir-notes-2")
    _seed_note(db_config, aid2, highlight_id=None, body="note 2")

    nt_dir = notes_dir(db_config)
    nt_dir.mkdir(parents=True)  # dir exists, zero sidecar files in it

    counts = reconcile_annotations(db_config)

    assert counts["guarded"] >= 1
    assert counts["notes_deleted"] == 0

    conn = get_connection(db_config.db_path)
    try:
        assert conn.execute(
            "SELECT * FROM notes WHERE article_id = ? AND highlight_id IS NULL", (aid1,)
        ).fetchone() is not None
        assert conn.execute(
            "SELECT * FROM notes WHERE article_id = ? AND highlight_id IS NULL", (aid2,)
        ).fetchone() is not None
    finally:
        conn.close()


def test_reconcile_guard_does_not_overfire_when_some_files_present(db_config):
    """Two articles have highlight rows; one has a matching (unchanged) file,
    the other has none at all. The widened guard must NOT fire (not every
    article's file is missing) -- the one legitimately-vanished article's
    rows are deleted normally, the other is untouched."""
    aid_has_file, uid_has_file = _seed_article(db_config, stem="guard-no-overfire-has-file")
    _seed_highlight(db_config, aid_has_file, uid="H-KEEP", quote="hello world")
    aid_no_file, _ = _seed_article(db_config, stem="guard-no-overfire-no-file")
    _seed_highlight(db_config, aid_no_file, uid="H-VANISH")

    write_annotations(
        db_config,
        "guard-no-overfire-has-file",
        [
            {
                "uid": "H-KEEP",
                "article_uid": uid_has_file,
                "quote": "hello world",
                "prefix": "pre",
                "suffix": "suf",
                "position_start": 0,
                "position_end": 11,
                "content_hash": "hash1",
                "color": "yellow",
            }
        ],
    )
    # "guard-no-overfire-no-file" deliberately gets no sidecar file at all,
    # while the directory (created above) exists and is non-empty.

    counts = reconcile_annotations(db_config)

    assert counts["guarded"] == 0
    assert counts["highlights_deleted"] == 1

    conn = get_connection(db_config.db_path)
    try:
        assert conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-KEEP",)
        ).fetchone() is not None
        assert conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-VANISH",)
        ).fetchone() is None
    finally:
        conn.close()


def test_reconcile_single_article_library_vanished_file_still_deletes(db_config):
    """Files-win is preserved for the ordinary single-article case: the
    directory exists (possibly empty), exactly one article has rows, and
    its sidecar is gone -- that's a legitimate delete, not a guard."""
    aid, _ = _seed_article(db_config, stem="lone-article-highlights")
    _seed_highlight(db_config, aid, uid="H-LONE")
    annotations_dir(db_config).mkdir(parents=True)  # exists, empty

    counts = reconcile_annotations(db_config)

    assert counts["guarded"] == 0
    assert counts["highlights_deleted"] == 1

    conn = get_connection(db_config.db_path)
    try:
        assert conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-LONE",)
        ).fetchone() is None
    finally:
        conn.close()


def test_reconcile_single_article_library_vanished_note_still_deletes(db_config):
    aid, _ = _seed_article(db_config, stem="lone-article-note")
    _seed_note(db_config, aid, highlight_id=None, body="lone note")
    notes_dir(db_config).mkdir(parents=True)  # exists, empty

    counts = reconcile_annotations(db_config)

    assert counts["guarded"] == 0
    assert counts["notes_deleted"] == 1

    conn = get_connection(db_config.db_path)
    try:
        assert conn.execute(
            "SELECT * FROM notes WHERE article_id = ? AND highlight_id IS NULL", (aid,)
        ).fetchone() is None
    finally:
        conn.close()


# --- reconcile_annotations: unreadable sidecar files (finding 2) -------------
# A directory created where a sidecar file is expected reproduces an
# unreadable file portably (path.read_text() raises IsADirectoryError, an
# OSError subclass) without permission bits that root/CI may ignore.


def test_reconcile_skips_unreadable_annotation_sidecar_without_aborting_others(db_config):
    aid_bad, _ = _seed_article(db_config, stem="unreadable-annotations")
    _seed_highlight(db_config, aid_bad, uid="H-UNTOUCHED")
    aid_ok, uid_ok = _seed_article(db_config, stem="readable-annotations")

    ann_dir = annotations_dir(db_config)
    ann_dir.mkdir(parents=True)
    (ann_dir / "unreadable-annotations.jsonl").mkdir()  # unreadable as a file
    write_annotations(
        db_config,
        "readable-annotations",
        [{"uid": "H-STILL-PROCESSED", "article_uid": uid_ok, "quote": "fine"}],
    )

    counts = reconcile_annotations(db_config)

    assert counts["unreadable_files"] == 1
    assert counts["guarded"] == 0
    # The other article's file is still processed -- one bad file doesn't
    # abort the reconcile.
    assert counts["highlights_inserted"] == 1

    conn = get_connection(db_config.db_path)
    try:
        # The unreadable article's existing row is untouched (not deleted).
        assert conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-UNTOUCHED",)
        ).fetchone() is not None
        assert conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-STILL-PROCESSED",)
        ).fetchone() is not None
    finally:
        conn.close()


def test_reconcile_skips_unreadable_note_sidecar_without_aborting_others(db_config):
    aid_bad, _ = _seed_article(db_config, stem="unreadable-notes")
    _seed_note(db_config, aid_bad, highlight_id=None, body="untouched note")
    aid_ok, _ = _seed_article(db_config, stem="readable-notes")

    nt_dir = notes_dir(db_config)
    nt_dir.mkdir(parents=True)
    (nt_dir / "unreadable-notes.md").mkdir()  # unreadable as a file
    write_note(db_config, "readable-notes", "processed fine")

    counts = reconcile_annotations(db_config)

    assert counts["unreadable_files"] == 1
    assert counts["guarded"] == 0
    assert counts["notes_inserted"] == 1

    conn = get_connection(db_config.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM notes WHERE article_id = ? AND highlight_id IS NULL", (aid_bad,)
        ).fetchone()
        assert row is not None
        assert row["body_markdown"] == "untouched note"
        assert conn.execute(
            "SELECT * FROM notes WHERE article_id = ? AND highlight_id IS NULL", (aid_ok,)
        ).fetchone() is not None
    finally:
        conn.close()


# --- reconcile_annotations: article_uid mismatch (finding 3) -----------------


def test_reconcile_stem_wins_on_article_uid_mismatch(db_config):
    """A hand-edited line whose article_uid disagrees with the stem-resolved
    article is still indexed under the stem-resolved article -- stem wins,
    per the module docstring -- with the disagreement logged and counted,
    never used to relocate the line."""
    aid, real_uid = _seed_article(db_config, stem="mismatch-stem")
    write_annotations(
        db_config,
        "mismatch-stem",
        [{"uid": "H-MISMATCH", "article_uid": "totally-unrelated-uid", "quote": "q"}],
    )

    counts = reconcile_annotations(db_config)

    assert counts["uid_mismatch_lines"] == 1
    assert counts["highlights_inserted"] == 1

    conn = get_connection(db_config.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-MISMATCH",)
        ).fetchone()
        assert row is not None
        assert row["article_id"] == aid  # stem-resolved article, not the claimed uid
    finally:
        conn.close()


def test_reconcile_no_mismatch_counted_when_article_uid_agrees(db_config):
    aid, real_uid = _seed_article(db_config, stem="match-stem")
    write_annotations(
        db_config,
        "match-stem",
        [{"uid": "H-MATCH", "article_uid": real_uid, "quote": "q"}],
    )

    counts = reconcile_annotations(db_config)

    assert counts["uid_mismatch_lines"] == 0
    assert counts["highlights_inserted"] == 1


# --- startup wiring: app.py lifespan calls reconcile_annotations() -----------


def test_startup_reconciles_hand_written_annotation_sidecars(configured_library):
    """A hand-added sidecar (e.g. `cp`'d in, or the DB wiped) with no
    matching derived rows must show up after the app (re)starts -- app.py's
    lifespan runs reconcile_annotations() (files win) right after the wiki
    reconcile, so drift heals on the next startup without a manual
    `tiro doctor --fix`. Mirrors test_wiki_api.py's equivalent wiki test."""
    from fastapi.testclient import TestClient

    from tiro.app import create_app

    config = configured_library
    aid, article_uid = _seed_article(config, stem="hand-written-stem")
    write_annotations(
        config,
        "hand-written-stem",
        [
            {
                "uid": "H-HANDWRITTEN",
                "article_uid": article_uid,
                "quote": "hand written",
                "note_markdown": "hand written note",
            }
        ],
    )
    write_note(config, "hand-written-stem", "hand written article note")

    conn = get_connection(config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM highlights").fetchone()["n"] == 0
    finally:
        conn.close()

    app = create_app(config)
    with TestClient(app, base_url="http://localhost"):
        pass  # lifespan startup/shutdown runs as the context manager enters/exits

    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM highlights WHERE uid = ?", ("H-HANDWRITTEN",)
        ).fetchone()
        assert row is not None
        assert row["article_id"] == aid
        note = conn.execute(
            "SELECT * FROM notes WHERE article_id = ? AND highlight_id IS NULL", (aid,)
        ).fetchone()
        assert note["body_markdown"] == "hand written article note"
    finally:
        conn.close()


# --- MCP tool: get_highlights (Phase 2 M2.1 Task 4) --------------------------
#
# Same precedent as test_wiki_api.py's list_wiki_pages/get_wiki_page tests:
# get_highlights is a plain module-level @mcp.tool()-decorated function
# (FastMCP doesn't wrap it), callable directly once the module's global
# _config points at an already-initialized library.


def _mcp_config(monkeypatch, config):
    import tiro.mcp.server as mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)
    return mcp_server


def test_mcp_get_highlights_empty(db_config, monkeypatch):
    mcp_server = _mcp_config(monkeypatch, db_config)
    assert mcp_server.get_highlights() == "No highlights found."


def test_mcp_get_highlights_lists_quote_article_and_note(db_config, monkeypatch):
    config = db_config
    article_id, _ = _seed_article(config, stem="art-1", title="Article One")
    highlight_id, h_uid = _seed_highlight(config, article_id, quote="hello world", color="green")
    _seed_note(config, article_id, highlight_id=highlight_id, body="my note")

    mcp_server = _mcp_config(monkeypatch, config)
    result = mcp_server.get_highlights()
    assert "hello world" in result
    assert "Article One" in result
    assert "green" in result
    assert "my note" in result
    assert f"article ID: {article_id}" in result


def test_mcp_get_highlights_filters_by_article_id(db_config, monkeypatch):
    config = db_config
    a1, _ = _seed_article(config, stem="art-1", title="Article One")
    a2, _ = _seed_article(config, stem="art-2", title="Article Two")
    _seed_highlight(config, a1, quote="from one")
    _seed_highlight(config, a2, quote="from two")

    mcp_server = _mcp_config(monkeypatch, config)
    result = mcp_server.get_highlights(article_id=a2)
    assert "from two" in result
    assert "from one" not in result


def test_mcp_get_highlights_filters_by_color(db_config, monkeypatch):
    config = db_config
    article_id, _ = _seed_article(config)
    _seed_highlight(config, article_id, quote="yellow one", color="yellow")
    _seed_highlight(config, article_id, quote="blue one", color="blue", uid="H-BLUE")

    mcp_server = _mcp_config(monkeypatch, config)
    result = mcp_server.get_highlights(color="blue")
    assert "blue one" in result
    assert "yellow one" not in result


def test_mcp_get_highlights_respects_limit(db_config, monkeypatch):
    config = db_config
    article_id, _ = _seed_article(config)
    for i in range(3):
        _seed_highlight(config, article_id, quote=f"quote {i}", uid=f"H-{i}")

    mcp_server = _mcp_config(monkeypatch, config)
    result = mcp_server.get_highlights(limit=1)
    assert result.count("quote ") == 1


def test_parse_jsonl_tolerates_unicode_line_separators_in_notes(db_config):
    """S2.8 hard-review nit pin (layer-local regression test for the
    splitlines -> split-on-LF fix in _parse_jsonl_lines): a note containing
    U+0085/U+2028 — legal RAW inside ensure_ascii=False JSON — must
    round-trip without shearing its own sidecar line into malformed
    fragments."""
    note = "before\u0085mid\u2028after end"
    line = {
        "uid": "01HLNEL0000000000000000001",
        "article_uid": "01ARTNEL000000000000000001",
        "quote": "q", "prefix": "", "suffix": "",
        "position_start": 0, "position_end": 1,
        "content_hash": "d" * 64, "color": "yellow",
        "note_markdown": note,
        "created_at": "2026-07-10T00:00:00Z",
        "updated_at": "2026-07-10T00:00:00Z",
    }
    write_annotations(db_config, "nel-stem", [line])
    lines = read_annotations(db_config, "nel-stem")
    assert len(lines) == 1
    assert lines[0]["note_markdown"] == note
