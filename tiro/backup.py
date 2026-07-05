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


def auto_backup(config: TiroConfig, reason: str) -> Path | None:
    """Best-effort snapshot before a destructive operation. Never raises.

    Mirrors the audit-log invariant (tiro/audit.py): failures here must never
    propagate into the caller (reclassify, source delete, etc.). Writes into
    {library}/backups/auto/ and prunes down to `config.backup_auto_keep`
    newest snapshots (mtime order; manual/ snapshots are never touched).
    """
    try:
        keep = config.backup_auto_keep
        if keep <= 0:
            return None
        auto_dir = config.library / "backups" / "auto"
        auto_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        n = 0
        while True:
            suffix = f"-{n}" if n else ""
            dest = auto_dir / f"{ts}-{reason}{suffix}.tar.zst"
            if not dest.exists():
                break
            n += 1
        path = create_snapshot(config, dest)
        # Retention: newest `keep` survive. Sort by (mtime, name) — 5 snapshots
        # created within the same second can share a coarse filesystem mtime,
        # so name is the tiebreaker (embeds the -n disambiguation suffix, which
        # increases lexically in creation order).
        snaps = sorted(auto_dir.glob("*.tar.zst"), key=lambda p: (p.stat().st_mtime, p.name))
        for old in snaps[:-keep]:
            old.unlink(missing_ok=True)
        return path
    except Exception as e:
        logger.error("Auto-backup failed (%s): %s", reason, e)
        return None


def list_snapshots(config: TiroConfig) -> list[dict]:
    """List manual + auto snapshots, newest first."""
    out = []
    for kind in ("manual", "auto"):
        d = config.library / "backups" / kind
        if not d.exists():
            continue
        for p in d.glob("*.tar.zst"):
            st = p.stat()
            out.append({
                "name": p.name,
                "path": str(p),
                "kind": kind,
                "size_bytes": st.st_size,
                "created_at": datetime.fromtimestamp(st.st_mtime, tz=UTC)
                .isoformat(timespec="seconds"),
            })
    return sorted(out, key=lambda s: s["created_at"], reverse=True)


def _safe_members(tar: tarfile.TarFile):
    """Yield members, rejecting absolute paths / traversal (CVE-class guard).
    Explicit loop instead of extractall(filter='data'): that keyword needs
    Python 3.11.4+, and the floor is 3.11.0."""
    for member in tar:
        name = member.name
        if name.startswith(("/", "\\")) or ".." in Path(name).parts:
            raise ValueError(f"unsafe path in snapshot: {name!r}")
        if member.issym() or member.islnk():
            raise ValueError(f"unsafe link member in snapshot: {name!r}")
        yield member


def restore_snapshot(config: TiroConfig, snapshot_path: Path) -> dict:
    """Replace the live library with a snapshot's contents.

    The current library directory is moved aside to a sibling
    `<name>.bak.<ts>` (never deleted). ChromaDB is rebuilt from
    embeddings.jsonl; articles without a restored vector are set to
    vector_status='pending' (retry loop re-embeds). Audio rows whose MP3 is
    not present after restore are deleted (cache reconciliation — digest and
    analysis caches live inside the restored tiro.db and are consistent by
    construction). The entire archive is validated (traversal/link checks on
    every member, plus the manifest) before anything is touched, so a
    malicious or corrupt snapshot fails before the live library is displaced.
    Run with the server stopped.
    """
    snapshot_path = Path(snapshot_path)
    # Pass 1: validate EVERY member and the manifest before touching anything
    manifest = None
    with _open_tar_zst_read(snapshot_path) as tar:
        for member in _safe_members(tar):
            if member.name == "manifest.json":
                manifest = json.loads(tar.extractfile(member).read())
    if manifest is None:
        raise ValueError("not a Tiro snapshot: manifest.json missing")
    if manifest.get("format_version") != SNAPSHOT_FORMAT_VERSION:
        raise ValueError(
            f"unsupported snapshot format_version {manifest.get('format_version')!r}"
        )

    # The library is only displaced after the full archive has passed validation
    # Displace the live library (keep it — this IS the pre-restore backup)
    library = config.library
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    displaced = library.parent / f"{library.name}.bak.{ts}"
    if library.exists():
        shutil.move(str(library), str(displaced))
    library.mkdir(parents=True, exist_ok=True)

    # Pass 2: extract (streaming tar needs a fresh open)
    embeddings_path = library / "embeddings.jsonl"
    with _open_tar_zst_read(snapshot_path) as tar:
        for member in _safe_members(tar):
            if member.name == "manifest.json" or not member.isfile():
                continue
            if member.name == "tiro.db":
                dest = config.db_path
            elif member.name == "config-snapshot.yaml":
                continue  # reference copy only; never touches live config.yaml
            elif member.name == "embeddings.jsonl":
                dest = embeddings_path
            else:
                dest = library / member.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out)

    # Snapshot may predate current schema
    from tiro.database import migrate_db

    migrate_db(config.db_path)

    # Rebuild ChromaDB from the portable dump
    from tiro.vectorstore import init_vectorstore

    collection = init_vectorstore(config.chroma_dir, config.default_embedding_model)
    vectors_restored = 0
    restored_ids: set[str] = set()
    if embeddings_path.exists():
        batch_ids, batch_emb, batch_meta, batch_doc = [], [], [], []

        def _flush():
            nonlocal vectors_restored
            if batch_ids:
                collection.upsert(
                    ids=list(batch_ids), embeddings=list(batch_emb),
                    metadatas=list(batch_meta), documents=list(batch_doc),
                )
                vectors_restored += len(batch_ids)
                batch_ids.clear()
                batch_emb.clear()
                batch_meta.clear()
                batch_doc.clear()

        for line in embeddings_path.read_text().splitlines():
            rec = json.loads(line)
            restored_ids.add(rec["id"])
            batch_ids.append(rec["id"])
            batch_emb.append(rec["embedding"])
            batch_meta.append(rec["metadata"])
            batch_doc.append(rec["document"])
            if len(batch_ids) >= 100:
                _flush()
        _flush()
        embeddings_path.unlink()

    conn = get_connection(config.db_path)
    try:
        rows = conn.execute("SELECT id FROM articles").fetchall()
        article_count = len(rows)
        vectors_pending = 0
        for row in rows:
            if f"article_{row['id']}" not in restored_ids:
                conn.execute(
                    "UPDATE articles SET vector_status = 'pending' WHERE id = ?",
                    (row["id"],),
                )
                vectors_pending += 1
        audio_rows_cleaned = 0
        for row in conn.execute("SELECT article_id, file_path FROM audio").fetchall():
            if not (library / "audio" / row["file_path"]).exists():
                conn.execute("DELETE FROM audio WHERE article_id = ?", (row["article_id"],))
                audio_rows_cleaned += 1
        conn.commit()
    finally:
        conn.close()

    logger.info(
        "Restored %d articles from %s (vectors: %d restored, %d pending; displaced: %s)",
        article_count, snapshot_path, vectors_restored, vectors_pending, displaced,
    )
    return {
        "articles": article_count,
        "vectors_restored": vectors_restored,
        "vectors_pending": vectors_pending,
        "audio_rows_cleaned": audio_rows_cleaned,
        "displaced_library": str(displaced),
    }
