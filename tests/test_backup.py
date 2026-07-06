"""Snapshot create/restore: portable, secret-free, recursion-safe."""

import json
import tarfile
from pathlib import Path

import zstandard

from tiro.backup import SECRET_CONFIG_KEYS, create_snapshot


def _read_snapshot_names(path):
    dctx = zstandard.ZstdDecompressor()
    with path.open("rb") as raw, dctx.stream_reader(raw) as z:
        with tarfile.open(mode="r|", fileobj=z) as tar:
            return [m.name for m in tar]


def _read_member(path, name):
    dctx = zstandard.ZstdDecompressor()
    with path.open("rb") as raw, dctx.stream_reader(raw) as z:
        with tarfile.open(mode="r|", fileobj=z) as tar:
            for m in tar:
                if m.name == name:
                    return tar.extractfile(m).read()
    raise KeyError(name)


def _seed_article(config, slug="art-1", title="T1"):
    from tiro.database import get_connection

    conn = get_connection(config.db_path)
    conn.execute("INSERT OR IGNORE INTO sources (name, source_type) VALUES ('Src', 'web')")
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
        " VALUES (?, 1, ?, ?, ?)",
        (slug.upper().ljust(26, "0"), title, slug, f"{slug}.md"),
    )
    conn.commit()
    conn.close()
    (config.articles_dir / f"{slug}.md").write_text(f"---\ntitle: {title}\n---\nbody {slug}")


def test_snapshot_contains_core_members(initialized_library, tmp_path):
    config = initialized_library
    _seed_article(config)
    (config.wiki_dir).mkdir(parents=True, exist_ok=True)
    (config.wiki_dir / "topic.md").write_text("# wiki page")

    snap = create_snapshot(config, tmp_path / "snap.tar.zst")
    names = _read_snapshot_names(snap)
    assert "manifest.json" in names
    assert "tiro.db" in names
    assert "config-snapshot.yaml" in names
    assert "articles/art-1.md" in names
    assert "wiki/topic.md" in names
    assert "embeddings.jsonl" in names

    manifest = json.loads(_read_member(snap, "manifest.json"))
    assert manifest["format_version"] == 1
    assert manifest["article_count"] == 1
    assert manifest["include_audio"] is False


def test_snapshot_contains_wiki_subdir_pages_with_preserved_arcname(initialized_library, tmp_path):
    """Wiki pages live under kind subdirectories (wiki/entities/*.md) --
    create_snapshot must recurse (rglob, not glob) and keep the subpath in
    the tar arcname."""
    config = initialized_library
    entities_dir = config.wiki_dir / "entities"
    entities_dir.mkdir(parents=True, exist_ok=True)
    (entities_dir / "anthropic.md").write_text("# Anthropic\n\nbody")

    snap = create_snapshot(config, tmp_path / "snap.tar.zst")
    names = _read_snapshot_names(snap)
    assert "wiki/entities/anthropic.md" in names


def test_restore_recreates_wiki_subdir_pages(initialized_library, tmp_path):
    """A wiki page nested under a kind subdirectory must round-trip through
    a full snapshot -> restore cycle byte-exact (restore's generic
    `dest = library / member.name` + `mkdir(parents=True)` already handles
    arbitrary depth -- this pins that behavior for the rglob'd wiki tree
    specifically)."""
    from tiro.backup import restore_snapshot

    config = initialized_library
    entities_dir = config.wiki_dir / "entities"
    entities_dir.mkdir(parents=True, exist_ok=True)
    original_bytes = b"# Anthropic\n\nbody"
    (entities_dir / "anthropic.md").write_bytes(original_bytes)

    snap = create_snapshot(config, tmp_path / "snap.tar.zst")
    restore_snapshot(config, snap)

    restored = config.wiki_dir / "entities" / "anthropic.md"
    assert restored.exists()
    assert restored.read_bytes() == original_bytes


def _seed_article_with_annotations(config, *, stem="art-1"):
    """Seed one article + one highlight (with an anchored note) + an
    article-level note, as both SQLite rows AND sidecar files."""
    from tiro.annotations import write_annotations, write_note
    from tiro.database import get_connection
    from tiro.migrations import new_ulid

    conn = get_connection(config.db_path)
    conn.execute("INSERT OR IGNORE INTO sources (name, source_type) VALUES ('S', 'web')")
    article_uid = new_ulid()
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
        " VALUES (?, 1, 'T1', ?, ?)",
        (article_uid, stem, f"{stem}.md"),
    )
    article_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    h_uid = new_ulid()
    conn.execute(
        """INSERT INTO highlights
           (uid, article_id, quote_text, prefix_context, suffix_context,
            text_position_start, text_position_end, content_hash, color,
            created_at, updated_at)
           VALUES (?, ?, 'quote', 'pre', 'suf', 0, 5, 'hash', 'yellow',
                   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
        (h_uid, article_id),
    )
    conn.execute(
        """INSERT INTO notes (uid, article_id, highlight_id, body_markdown, created_at, updated_at)
           VALUES (?, ?, NULL, 'article note', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
        (new_ulid(), article_id),
    )
    conn.commit()
    conn.close()
    (config.articles_dir / f"{stem}.md").write_text("---\ntitle: T1\n---\nbody")

    write_annotations(
        config, stem,
        [{
            "uid": h_uid, "article_uid": article_uid, "quote": "quote",
            "prefix": "pre", "suffix": "suf", "position_start": 0, "position_end": 5,
            "content_hash": "hash", "color": "yellow", "note_markdown": None,
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        }],
    )
    write_note(config, stem, "article note")
    return article_id


def test_snapshot_contains_annotation_sidecars(initialized_library, tmp_path):
    config = initialized_library
    _seed_article_with_annotations(config)

    snap = create_snapshot(config, tmp_path / "snap.tar.zst")
    names = _read_snapshot_names(snap)
    assert "annotations/art-1.jsonl" in names
    assert "notes/art-1.md" in names


def test_restore_round_trips_highlight_and_note_rows_and_files(initialized_library, tmp_path):
    """Create a highlight + note -> snapshot -> wipe the library -> restore
    -> both the sidecar FILES and the derived SQLite ROWS must be present
    (tiro.db is restored wholesale, so the rows come back with it)."""
    from tiro.backup import restore_snapshot
    from tiro.database import get_connection

    config = initialized_library
    article_id = _seed_article_with_annotations(config)

    snap = create_snapshot(config, tmp_path / "snap.tar.zst")
    restore_snapshot(config, snap)

    assert (config.library / "annotations" / "art-1.jsonl").exists()
    assert (config.library / "notes" / "art-1.md").exists()
    assert (config.library / "notes" / "art-1.md").read_text() == "article note"

    conn = get_connection(config.db_path)
    try:
        highlight = conn.execute(
            "SELECT quote_text FROM highlights WHERE article_id = ?", (article_id,)
        ).fetchone()
        note = conn.execute(
            "SELECT body_markdown FROM notes WHERE article_id = ? AND highlight_id IS NULL",
            (article_id,),
        ).fetchone()
    finally:
        conn.close()
    assert highlight is not None and highlight["quote_text"] == "quote"
    assert note is not None and note["body_markdown"] == "article note"


def test_snapshot_scrubs_reading_sessions(initialized_library, tmp_path):
    """Finding 1 (MAJOR, controller decision O-6): docs promise backups
    exclude reading_sessions telemetry -- make that promise TRUE by scrubbing
    the throwaway DB copy before it's tarred, rather than just weakening the
    docs. Snapshot -> restore (into a fresh library dir, since restore
    displaces the old one) must come back with zero reading_sessions rows
    while unrelated article/highlight rows and files survive untouched."""
    from tiro.database import get_connection
    from tiro.migrations import new_ulid

    config = initialized_library
    article_id = _seed_article_with_annotations(config)

    conn = get_connection(config.db_path)
    try:
        conn.execute(
            """
            INSERT INTO reading_sessions
                (uid, article_id, started_at, ended_at, max_scroll_pct, active_seconds, dwell_json)
            VALUES (?, ?, '2026-07-05T10:00:00Z', '2026-07-05T10:01:00Z', 50, 60, '[]')
            """,
            (new_ulid(), article_id),
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM reading_sessions").fetchone()[0] == 1
    finally:
        conn.close()

    snap = create_snapshot(config, tmp_path / "snap.tar.zst")

    # The live DB must never be touched by the scrub -- only the throwaway
    # tempdir copy that gets tarred.
    conn = get_connection(config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM reading_sessions").fetchone()[0] == 1
    finally:
        conn.close()

    from tiro.backup import restore_snapshot

    restore_snapshot(config, snap)  # restore into a fresh library dir (old one is displaced)

    conn = get_connection(config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM reading_sessions").fetchone()[0] == 0
        article = conn.execute(
            "SELECT title FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        highlight = conn.execute(
            "SELECT quote_text FROM highlights WHERE article_id = ?", (article_id,)
        ).fetchone()
    finally:
        conn.close()
    assert article is not None and article["title"] == "T1"
    assert highlight is not None and highlight["quote_text"] == "quote"


def test_snapshot_strips_secrets(initialized_library, tmp_path):
    config = initialized_library
    config.anthropic_api_key = "sk-ant-SECRET"
    config.smtp_password = "hunter2"
    snap = create_snapshot(config, tmp_path / "snap.tar.zst")
    text = _read_member(snap, "config-snapshot.yaml").decode()
    assert "SECRET" not in text and "hunter2" not in text
    for key in SECRET_CONFIG_KEYS:
        assert f"{key}:" not in text or "REDACTED" in text


def test_snapshot_excludes_backups_dir(initialized_library, tmp_path):
    config = initialized_library
    _seed_article(config)
    prior = config.library / "backups" / "manual"
    prior.mkdir(parents=True)
    (prior / "old.tar.zst").write_bytes(b"not-a-real-snapshot")
    snap = create_snapshot(config, tmp_path / "snap.tar.zst")
    names = _read_snapshot_names(snap)
    assert not any(n.startswith("backups/") for n in names)


def test_default_output_location(initialized_library):
    config = initialized_library
    _seed_article(config)
    snap = create_snapshot(config)
    assert snap.parent == config.library / "backups" / "manual"
    assert snap.name.startswith("tiro-") and snap.name.endswith(".tar.zst")


def test_restore_round_trip(initialized_library, tmp_path):
    """Roadmap acceptance criterion: snapshot -> mutate -> restore -> identical."""
    import hashlib

    from tiro.backup import restore_snapshot
    from tiro.database import get_connection
    from tiro.lifecycle import delete_article

    config = initialized_library
    _seed_article(config, "art-1", "T1")
    _seed_article(config, "art-2", "T2")
    original_hash = hashlib.sha256(
        (config.articles_dir / "art-1.md").read_bytes()
    ).hexdigest()

    snap = create_snapshot(config, tmp_path / "snap.tar.zst")

    delete_article(config, 1)  # mutate after snapshot
    assert not (config.articles_dir / "art-1.md").exists()

    result = restore_snapshot(config, snap)

    conn = get_connection(config.db_path)
    titles = {r["title"] for r in conn.execute("SELECT title FROM articles").fetchall()}
    conn.close()
    assert titles == {"T1", "T2"}
    assert hashlib.sha256(
        (config.articles_dir / "art-1.md").read_bytes()
    ).hexdigest() == original_hash
    assert result["articles"] == 2
    # displaced library preserved as a sibling .bak
    assert Path(result["displaced_library"]).exists()


def test_restore_marks_missing_vectors_pending(initialized_library, tmp_path):
    from tiro.backup import restore_snapshot
    from tiro.database import get_connection

    config = initialized_library
    _seed_article(config)
    conn = get_connection(config.db_path)
    conn.execute("UPDATE articles SET vector_status = 'indexed'")
    conn.commit()
    conn.close()

    snap = create_snapshot(config, tmp_path / "snap.tar.zst")  # 0 vectors in chroma
    result = restore_snapshot(config, snap)
    assert result["vectors_pending"] == 1

    conn = get_connection(config.db_path)
    status = conn.execute("SELECT vector_status FROM articles").fetchone()[0]
    conn.close()
    assert status == "pending"


def test_restore_cleans_audio_rows_without_files(initialized_library, tmp_path):
    from tiro.backup import restore_snapshot
    from tiro.database import get_connection

    config = initialized_library
    _seed_article(config)
    conn = get_connection(config.db_path)
    conn.execute(
        "INSERT INTO audio (article_id, file_path, voice, model, generated_at)"
        " VALUES (1, '1.mp3', 'nova', 'tts-1', '2026-01-01')"
    )
    conn.commit()
    conn.close()
    # No MP3 on disk and snapshot without --include-audio
    snap = create_snapshot(config, tmp_path / "snap.tar.zst")
    result = restore_snapshot(config, snap)
    assert result["audio_rows_cleaned"] == 1


def test_auto_backup_retention(initialized_library):
    from tiro.backup import auto_backup

    config = initialized_library
    _seed_article(config)
    config.backup_auto_keep = 3
    made = [auto_backup(config, "test") for _ in range(5)]
    assert all(p is not None for p in made)
    remaining = sorted((config.library / "backups" / "auto").glob("*.tar.zst"))
    assert len(remaining) == 3
    # newest three survive
    assert {p.name for p in remaining} == {p.name for p in made[-3:]}


def test_auto_backup_never_raises(initialized_library, monkeypatch):
    from tiro import backup as backup_mod

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(backup_mod, "create_snapshot", boom)
    assert backup_mod.auto_backup(initialized_library, "test") is None


def test_list_snapshots(initialized_library):
    from tiro.backup import auto_backup, list_snapshots

    config = initialized_library
    _seed_article(config)
    create_snapshot(config)  # manual default location
    auto_backup(config, "classify")
    snaps = list_snapshots(config)
    kinds = {s["kind"] for s in snaps}
    assert kinds == {"manual", "auto"}
    assert all(s["size_bytes"] > 0 for s in snaps)


def test_auto_backup_disabled_when_keep_zero(initialized_library):
    from tiro.backup import auto_backup

    config = initialized_library
    _seed_article(config)
    config.backup_auto_keep = 0
    assert auto_backup(config, "test") is None
    assert not (config.library / "backups" / "auto").exists()


def test_snapshots_endpoint(authenticated_client):
    resp = authenticated_client.get("/api/backup/snapshots")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "snapshots" in body["data"]
    assert body["data"]["snapshots"] == []


def test_restore_rejects_traversal_members(initialized_library, tmp_path):
    """A malicious snapshot must not write outside the library."""
    import io

    import pytest

    from tiro.backup import _open_tar_zst_write, restore_snapshot

    evil = tmp_path / "evil.tar.zst"
    with _open_tar_zst_write(evil) as tar:
        info = tarfile.TarInfo(name="manifest.json")
        payload = json.dumps({"format_version": 1}).encode()
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        info2 = tarfile.TarInfo(name="articles/../../outside.md")
        info2.size = 4
        tar.addfile(info2, io.BytesIO(b"evil"))
    with pytest.raises(ValueError, match="unsafe path"):
        restore_snapshot(initialized_library, evil)
    assert not (tmp_path / "outside.md").exists()
    # Atomicity: validation must reject the whole archive BEFORE the live
    # library is displaced — no .bak sibling should have been created.
    assert initialized_library.library.exists()
    assert not list(
        initialized_library.library.parent.glob(f"{initialized_library.library.name}.bak.*")
    )


def test_restore_from_in_library_snapshot(initialized_library):
    """M1.1 review item 1 regression: `tiro backup`'s default output lives
    under {library}/backups/manual/ — restore must survive displacing the
    live library out from under the very snapshot path it's reading."""
    from tiro.backup import restore_snapshot
    from tiro.database import get_connection
    from tiro.lifecycle import delete_article

    config = initialized_library
    _seed_article(config, "art-1", "T1")
    snap = create_snapshot(config)  # default in-library location
    assert snap.is_relative_to(config.library)

    delete_article(config, 1)  # mutate after snapshot
    assert not (config.articles_dir / "art-1.md").exists()

    result = restore_snapshot(config, snap)

    conn = get_connection(config.db_path)
    titles = {r["title"] for r in conn.execute("SELECT title FROM articles").fetchall()}
    conn.close()
    assert titles == {"T1"}
    assert (config.articles_dir / "art-1.md").exists()
    assert result["articles"] == 1


def test_create_snapshot_atomic_on_mid_write_failure(initialized_library, tmp_path, monkeypatch):
    """M1.1 review item 3: a mid-write failure must not leave a truncated
    .tar.zst (nor a stray .tmp) that retention could mistake for the
    newest — and therefore keep — snapshot."""
    import pytest

    from tiro import backup as backup_mod

    config = initialized_library
    _seed_article(config)

    def boom(_config):
        raise RuntimeError("embeddings dump exploded")

    monkeypatch.setattr(backup_mod, "_dump_embeddings_jsonl", boom)

    output = tmp_path / "snap.tar.zst"
    with pytest.raises(RuntimeError, match="embeddings dump exploded"):
        backup_mod.create_snapshot(config, output)

    assert not output.exists()
    assert not output.with_name(output.name + ".tmp").exists()


def test_restore_flags_schema_newer_than_app(initialized_library, tmp_path, caplog):
    """M1.1 review item 4: restoring a snapshot whose schema is newer than
    this Tiro understands must be flagged, not silently accepted."""
    from tiro.backup import restore_snapshot
    from tiro.database import get_connection
    from tiro.migrations import LATEST_VERSION

    config = initialized_library
    _seed_article(config)

    conn = get_connection(config.db_path)
    conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 5}")
    conn.commit()
    conn.close()

    snap = create_snapshot(config, tmp_path / "snap.tar.zst")
    with caplog.at_level("WARNING"):
        result = restore_snapshot(config, snap)

    assert result["schema_newer_than_app"] is True
    assert any("newer" in r.message.lower() for r in caplog.records)


def test_restore_schema_newer_flag_defaults_false(initialized_library, tmp_path):
    """Companion to the above: the normal (non-downgrade) path must report
    schema_newer_than_app=False, not just omit the key."""
    from tiro.backup import restore_snapshot

    config = initialized_library
    _seed_article(config)
    snap = create_snapshot(config, tmp_path / "snap.tar.zst")
    result = restore_snapshot(config, snap)
    assert result["schema_newer_than_app"] is False


def test_restore_preserves_backup_history(initialized_library):
    """M1.1 review item 5: snapshot/backup history is an independent
    artifact, not library state — it must survive a restore, including the
    very in-library snapshot that was just restored from (exercises item 1's
    temp-copy fix too)."""
    from tiro.backup import list_snapshots, restore_snapshot

    config = initialized_library
    _seed_article(config)
    snap = create_snapshot(config)  # {library}/backups/manual/tiro-....tar.zst

    restore_snapshot(config, snap)

    assert snap.exists()
    kinds = {s["kind"] for s in list_snapshots(config)}
    assert "manual" in kinds


def test_cmd_restore_refuses_when_server_running(initialized_library, tmp_path, capsys):
    """M1.1 review item 6: `tiro restore` must refuse to run against a
    library whose server is still up, unless --force is passed.

    Args-stub pattern per test_importer.py::test_cli_import — the outer
    local is named `lib_config` (not `config`) specifically so it isn't
    shadowed by the `Args` class body's own `config` attribute (Python's
    class-body scoping treats any name assigned in the class body as local
    to that block for the whole block, which would otherwise turn the
    earlier reference into a NameError).
    """
    import socket

    import pytest

    from tiro.cli import cmd_restore

    lib_config = initialized_library
    _seed_article(lib_config)
    snap_path = create_snapshot(lib_config, tmp_path / "snap.tar.zst")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        bound_port = srv.getsockname()[1]
        lib_config.host = "127.0.0.1"
        lib_config.port = bound_port

        class Args:
            _config_override = lib_config
            config = "unused"
            snapshot = str(snap_path)
            yes = True
            force = False

        with pytest.raises(SystemExit) as exc_info:
            cmd_restore(Args())
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "stop it first" in out
    finally:
        srv.close()

    # --force bypasses the guard entirely (server still bound at this point
    # would be flaky since the socket is closed above, so re-bind to prove
    # --force skips the check rather than merely happening to see it down).
    srv2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv2.bind(("127.0.0.1", 0))
        srv2.listen(1)
        lib_config.port = srv2.getsockname()[1]

        class ForcedArgs:
            _config_override = lib_config
            config = "unused"
            snapshot = str(snap_path)
            yes = True
            force = True

        cmd_restore(ForcedArgs())  # must not raise SystemExit
    finally:
        srv2.close()
