"""Snapshot create/restore: portable, secret-free, recursion-safe."""

import json
import tarfile

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
