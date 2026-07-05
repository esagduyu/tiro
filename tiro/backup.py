"""Library snapshots: create, restore, auto-backup, retention.

Format: tar + zstd. ChromaDB is NEVER copied as internal files — vectors are
exported to a portable embeddings.jsonl (id/embedding/metadata/document per
line) and re-upserted on restore; anything missing falls back to
vector_status='pending' and the retry loop re-embeds. {library}/backups/ is
excluded from snapshots (recursion guard). Secrets are stripped from the
bundled config-snapshot.yaml (reference only — restore never touches the
live config.yaml).
"""

import json
import logging
import shutil
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import yaml
import zstandard

from tiro import __version__
from tiro.config import TiroConfig
from tiro.database import get_connection

logger = logging.getLogger(__name__)

SNAPSHOT_FORMAT_VERSION = 1

SECRET_CONFIG_KEYS = {
    "anthropic_api_key",
    "openai_api_key",
    "ai_openai_api_key",
    "smtp_password",
    "imap_password",
    "auth_password_hash",
}

_EMBED_BATCH = 500


@contextmanager
def _open_tar_zst_write(dest: Path):
    cctx = zstandard.ZstdCompressor(level=9)
    with dest.open("wb") as raw, cctx.stream_writer(raw) as z:
        with tarfile.open(mode="w|", fileobj=z) as tar:
            yield tar


@contextmanager
def _open_tar_zst_read(src: Path):
    dctx = zstandard.ZstdDecompressor()
    with src.open("rb") as raw, dctx.stream_reader(raw) as z:
        with tarfile.open(mode="r|", fileobj=z) as tar:
            yield tar


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    import io

    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _sanitized_config_yaml(config: TiroConfig) -> str:
    from dataclasses import fields

    data = {}
    for f in fields(TiroConfig):
        if f.name == "config_path":
            continue
        value = getattr(config, f.name)
        data[f.name] = "REDACTED" if (f.name in SECRET_CONFIG_KEYS and value) else value
    return yaml.safe_dump(data, sort_keys=True)


def _dump_embeddings_jsonl(config: TiroConfig) -> bytes:
    """Portable vector dump. Returns b"" when ChromaDB is unavailable/empty."""
    try:
        from tiro.vectorstore import get_collection

        collection = get_collection()
    except Exception:
        logger.warning("ChromaDB unavailable during snapshot — embeddings.jsonl will be empty")
        return b""
    lines: list[str] = []
    offset = 0
    while True:
        batch = collection.get(
            include=["embeddings", "metadatas", "documents"],
            limit=_EMBED_BATCH,
            offset=offset,
        )
        ids = batch.get("ids") or []
        if not ids:
            break
        for i, id_ in enumerate(ids):
            emb = batch["embeddings"][i]
            lines.append(json.dumps({
                "id": id_,
                "embedding": [float(x) for x in emb],
                "metadata": batch["metadatas"][i],
                "document": batch["documents"][i],
            }))
        offset += len(ids)
    return ("\n".join(lines) + ("\n" if lines else "")).encode()


def _checkpointed_db_copy(config: TiroConfig, dest: Path) -> None:
    conn = get_connection(config.db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    shutil.copy2(config.db_path, dest)


def create_snapshot(
    config: TiroConfig,
    output_path: Path | None = None,
    *,
    include_audio: bool = False,
) -> Path:
    """Write a full library snapshot. Returns the snapshot path."""
    if output_path is None:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_path = config.library / "backups" / "manual" / f"tiro-{ts}.tar.zst"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(config.db_path)
    try:
        article_count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    finally:
        conn.close()

    manifest = {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tiro_version": __version__,
        "include_audio": include_audio,
        "article_count": article_count,
    }

    with tempfile.TemporaryDirectory() as td:
        db_copy = Path(td) / "tiro.db"
        _checkpointed_db_copy(config, db_copy)

        with _open_tar_zst_write(output_path) as tar:
            _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=2).encode())
            tar.add(db_copy, arcname="tiro.db")
            _add_bytes(tar, "config-snapshot.yaml", _sanitized_config_yaml(config).encode())
            if config.articles_dir.exists():
                for md in sorted(config.articles_dir.glob("*.md")):
                    tar.add(md, arcname=f"articles/{md.name}")
            if config.wiki_dir.exists():
                for page in sorted(config.wiki_dir.glob("*.md")):
                    tar.add(page, arcname=f"wiki/{page.name}")
            _add_bytes(tar, "embeddings.jsonl", _dump_embeddings_jsonl(config))
            audio_dir = config.library / "audio"
            if include_audio and audio_dir.exists():
                for mp3 in sorted(audio_dir.glob("*.mp3")):
                    tar.add(mp3, arcname=f"audio/{mp3.name}")

    logger.info("Snapshot written: %s (%d articles)", output_path, article_count)
    return output_path
