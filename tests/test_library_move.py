"""Adversarial suite for `tiro migrate-library` — the copy-then-confirm-NEVER-
remove migration (spec D3). The source library is NEVER deleted, renamed, or
moved. Any failure mid-protocol leaves BOTH libraries valid (source untouched;
partial dest cleaned or marker-flagged for a clean re-run).
"""

import os
from pathlib import Path

import pytest

from tiro.config import TiroConfig, load_config
from tiro.database import init_db, migrate_db
from tiro.library_move import MARKER_NAME, MigrationError, migrate_library


def _make_library(root: Path) -> Path:
    """Build a realistic scratch library (all seven store dirs + db).

    The `chroma/` store is created as opaque on-disk files WITHOUT spinning up a
    real ChromaDB client. migrate_library treats chroma as verbatim bytes — it
    never opens the DB (see tiro/library_move.py's module docstring) — so a live
    client here buys no additional fidelity for the code under test, while
    creating one per test (13x) would accumulate chromadb 1.5.0's process-wide
    native tokio threads and push the whole pytest process over the OS thread
    ceiling (pthread_create -> EAGAIN, surfacing as downstream ChromaDB errors
    or a hang — see the task report). We ALSO null tiro's vectorstore globals so
    the in-migration `auto_backup`'s embeddings dump takes its guarded
    "no collection -> empty embeddings.jsonl" path (tiro/backup.py) rather than
    touching a stale global collection left by an earlier test in the suite
    (which would raise, making auto_backup return None and abort the migration).
    """
    import tiro.vectorstore as vectorstore

    lib = root / "src-library"
    (lib / "articles").mkdir(parents=True)
    (lib / "annotations").mkdir()
    (lib / "notes").mkdir()
    (lib / "wiki" / "entities").mkdir(parents=True)
    (lib / "audio").mkdir()
    (lib / "audit").mkdir()
    (lib / "chroma").mkdir()
    (lib / "articles" / "hello.md").write_text("# Hello\n\nbody\n")
    (lib / "annotations" / "hello.jsonl").write_text('{"uid":"x"}\n')
    (lib / "notes" / "hello.md").write_text("a note\n")
    (lib / "wiki" / "entities" / "foo.md").write_text("wiki body\n")
    (lib / "audit" / "2026-07-10.jsonl").write_text('{"service":"x"}\n')
    (lib / "chroma" / "chroma.sqlite3").write_bytes(b"opaque-chroma-store\x00")
    cfg = TiroConfig(library_path=str(lib))
    init_db(cfg.db_path)
    migrate_db(cfg.db_path)
    vectorstore._client = None
    vectorstore._collection = None
    return lib


@pytest.fixture
def scratch(tmp_path):
    """A config whose config.yaml lives OUTSIDE the library, with a populated
    source library. Returns (config, source_path)."""
    lib = _make_library(tmp_path)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f'library_path: "{lib}"\n')
    config = load_config(cfg_file)
    return config, lib


def _tree_sizes(root: Path, exclude_top: set[str]) -> dict[str, int]:
    out = {}
    for p in root.rglob("*"):
        if p.is_file():
            rel = p.relative_to(root)
            if rel.parts and rel.parts[0] in exclude_top:
                continue
            out[rel.as_posix()] = p.stat().st_size
    return out


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_copies_and_repoints(scratch, tmp_path):
    config, src = scratch
    # Capture content excluding backups/ — the migration's own auto snapshot
    # lands under backups/auto/ mid-flight and is legitimately copied to dest.
    before = _tree_sizes(src, exclude_top={"backups"})
    dest = tmp_path / "dest-library"

    report = migrate_library(config, dest, assume_yes=True)

    assert report["status"] == "migrated"
    # every source content file present at dest with identical size
    after = _tree_sizes(dest, exclude_top={MARKER_NAME, "backups"})
    assert after == before
    # the in-flight snapshot itself was copied across too
    assert list((dest / "backups" / "auto").glob("*library-migrate*.tar.zst"))
    # marker gone on success
    assert not (dest / MARKER_NAME).exists()
    # config re-pointed — ONLY after verification
    reloaded = load_config(config.config_path)
    assert reloaded.library == dest.resolve()
    # snapshot taken under the OLD library's backups/auto/
    autos = list((src / "backups" / "auto").glob("*library-migrate*.tar.zst"))
    assert autos, "expected an auto snapshot before migration"
    # SOURCE content bit-identical after (never touched)
    assert _tree_sizes(src, exclude_top={"backups"}) == before


def test_source_never_removed(scratch, tmp_path):
    config, src = scratch
    dest = tmp_path / "dest-library"
    migrate_library(config, dest, assume_yes=True)
    assert src.exists()
    assert (src / "tiro.db").exists()
    assert (src / "articles" / "hello.md").exists()


# ---------------------------------------------------------------------------
# auto_backup failure aborts before any copy
# ---------------------------------------------------------------------------


def test_backup_failure_aborts_untouched(scratch, tmp_path, monkeypatch):
    config, src = scratch
    dest = tmp_path / "dest-library"
    monkeypatch.setattr("tiro.library_move.auto_backup", lambda *a, **k: None)

    with pytest.raises(MigrationError):
        migrate_library(config, dest, assume_yes=True)

    assert not dest.exists()  # dest absent/untouched
    assert load_config(config.config_path).library == src.resolve()  # config unchanged


# ---------------------------------------------------------------------------
# Interrupted copy — marker + partial files → re-run clears and completes
# ---------------------------------------------------------------------------


def test_interrupted_run_reruns_clean(scratch, tmp_path):
    config, src = scratch
    dest = tmp_path / "dest-library"
    # simulate a killed prior run: marker + partial junk
    dest.mkdir()
    (dest / MARKER_NAME).write_text("in progress\n")
    (dest / "articles").mkdir()
    (dest / "articles" / "partial.md").write_text("half written")
    (dest / "stray.tmp").write_text("garbage")

    report = migrate_library(config, dest, assume_yes=True)

    assert report["status"] == "migrated"
    assert not (dest / "stray.tmp").exists()  # stale partial cleared
    assert not (dest / "articles" / "partial.md").exists()
    assert (dest / "articles" / "hello.md").exists()
    assert not (dest / MARKER_NAME).exists()


# ---------------------------------------------------------------------------
# Foreign non-empty dest (no marker) → abort untouched
# ---------------------------------------------------------------------------


def test_foreign_dest_aborts(scratch, tmp_path):
    config, src = scratch
    dest = tmp_path / "dest-library"
    dest.mkdir()
    (dest / "important-user-file.txt").write_text("do not touch")

    with pytest.raises(MigrationError):
        migrate_library(config, dest, assume_yes=True)

    assert (dest / "important-user-file.txt").read_text() == "do not touch"
    assert load_config(config.config_path).library == src.resolve()


# ---------------------------------------------------------------------------
# Verify mismatch (injected mid-protocol) → abort, marker retained, config kept
# ---------------------------------------------------------------------------


def test_verify_mismatch_aborts_with_marker(scratch, tmp_path):
    config, src = scratch
    dest = tmp_path / "dest-library"

    def corrupt(dest_path):
        # mutate a copied file's size so per-file size verify fails
        (dest_path / "articles" / "hello.md").write_text("CORRUPTED-DIFFERENT-LENGTH")

    with pytest.raises(MigrationError):
        migrate_library(config, dest, assume_yes=True, _pre_verify_hook=corrupt)

    assert (dest / MARKER_NAME).exists()  # marker retained → clean re-run
    assert load_config(config.config_path).library == src.resolve()  # unchanged
    assert (src / "articles" / "hello.md").read_text() == "# Hello\n\nbody\n"  # source intact

    # re-run (without the sabotage) recovers from scratch
    report = migrate_library(config, dest, assume_yes=True)
    assert report["status"] == "migrated"
    assert not (dest / MARKER_NAME).exists()


# ---------------------------------------------------------------------------
# Re-run after success → clean no-op ("already migrated")
# ---------------------------------------------------------------------------


def test_rerun_after_success_is_noop(scratch, tmp_path):
    config, src = scratch
    dest = tmp_path / "dest-library"
    migrate_library(config, dest, assume_yes=True)

    # config now points at dest; re-running to the same dest is a no-op
    config2 = load_config(config.config_path)
    report = migrate_library(config2, dest, assume_yes=True)
    assert report["status"] == "already_at_destination"
    assert report["files_copied"] == 0


# ---------------------------------------------------------------------------
# dest == source → refusal (no copy, no error mutation)
# ---------------------------------------------------------------------------


def test_dest_equals_source_refused(scratch):
    config, src = scratch
    report = migrate_library(config, src, assume_yes=True)
    assert report["status"] == "already_at_destination"
    assert report["files_copied"] == 0


# ---------------------------------------------------------------------------
# Path containment guard — dest inside source
# ---------------------------------------------------------------------------


def test_dest_inside_source_refused(scratch):
    config, src = scratch
    inside = src / "articles" / "nested-dest"
    with pytest.raises(MigrationError):
        migrate_library(config, inside, assume_yes=True)


def test_source_inside_dest_refused(scratch, tmp_path):
    config, src = scratch
    # dest is an ancestor of source
    with pytest.raises(MigrationError):
        migrate_library(config, src.parent, assume_yes=True)


# ---------------------------------------------------------------------------
# config.yaml inside the library is excluded from the copy
# ---------------------------------------------------------------------------


def test_inside_library_config_excluded(scratch, tmp_path):
    config, src = scratch
    # Docker layout: a config.yaml living INSIDE the library dir
    (src / "config.yaml").write_text("library_path: /wherever\n")
    dest = tmp_path / "dest-library"

    migrate_library(config, dest, assume_yes=True)

    assert not (dest / "config.yaml").exists()  # excluded doppelgänger
    assert (src / "config.yaml").exists()  # source copy untouched


# ---------------------------------------------------------------------------
# Symlinked paths resolve correctly (source reached via a symlink)
# ---------------------------------------------------------------------------


def test_symlinked_source_resolves(scratch, tmp_path):
    config, src = scratch
    link = tmp_path / "src-link"
    link.symlink_to(src, target_is_directory=True)
    # point config at the symlink
    config.library_path = str(link)
    dest = tmp_path / "dest-library"

    report = migrate_library(config, dest, assume_yes=True)
    assert report["status"] == "migrated"
    assert (dest / "articles" / "hello.md").exists()


# ---------------------------------------------------------------------------
# Permission-denied target → abort, source intact, config unchanged
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_permission_denied_target(scratch, tmp_path):
    config, src = scratch
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o500)  # read+execute, no write
    dest = locked / "dest-library"
    try:
        with pytest.raises((MigrationError, PermissionError, OSError)):
            migrate_library(config, dest, assume_yes=True)
    finally:
        os.chmod(locked, 0o700)
    assert load_config(config.config_path).library == src.resolve()
    assert (src / "articles" / "hello.md").exists()
