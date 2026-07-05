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
