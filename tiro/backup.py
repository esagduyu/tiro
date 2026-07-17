"""Library snapshots: create, restore, auto-backup, retention.

Format: tar + zstd. ChromaDB is NEVER copied as internal files — vectors are
exported to a portable embeddings.jsonl (id/embedding/metadata/document per
line) and re-upserted on restore; anything missing falls back to
vector_status='pending' and the retry loop re-embeds. {library}/backups/ is
excluded from snapshots (recursion guard). Secrets are stripped from the
bundled config-snapshot.yaml (reference only — restore never touches the
live config.yaml). `reading_sessions` rows (ephemeral local telemetry, M2.3)
are scrubbed from the throwaway DB copy before it's tarred — snapshots never
carry them, matching the docs' promise (see `_scrub_reading_sessions`).
Persona files (`{library}/personas/*.md`, Phase 6 K3) ride along via an
explicit per-directory allowlist entry (this is NOT a whole-library walk —
`agents/traces/` is deliberately excluded, see the K1-K2 owner-ratification
item D15).
"""

import json
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import yaml
import zstandard

from tiro import __version__
from tiro.agents.personas import personas_dir
from tiro.annotations import annotations_dir, notes_dir
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


def _scrub_reading_sessions(db_copy: Path) -> None:
    """Delete all rows from `reading_sessions` in the throwaway snapshot DB
    copy. reading_sessions (migration 010) is ephemeral local telemetry, not
    library content -- README/docs promise backups exclude it, so this makes
    that promise true rather than just weakening the docs (controller
    decision O-6). Operates ONLY on `db_copy` (a tempdir copy produced by
    this function's caller) -- the live `config.db_path` is never opened
    here. Uses a plain (non-WAL) connection so the DELETE lands directly in
    the copy's single file: `create_snapshot` only `tar.add`s that one path,
    so anything left behind in a separate -wal/-shm sidecar would silently
    fail to be scrubbed from the archive. Guards for pre-010 databases
    (table doesn't exist yet) by checking sqlite_master first rather than
    relying on a caught OperationalError.
    """
    conn = sqlite3.connect(str(db_copy))
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reading_sessions'"
        ).fetchone()
        if has_table:
            conn.execute("DELETE FROM reading_sessions")
            conn.commit()
    finally:
        conn.close()


def _checkpointed_db_copy(config: TiroConfig, dest: Path) -> None:
    conn = get_connection(config.db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    shutil.copy2(config.db_path, dest)
    _scrub_reading_sessions(dest)


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

    # Write to a sibling .tmp and os.replace() into place on success — a
    # mid-write failure (disk full, ChromaDB error, etc.) must never leave a
    # truncated .tar.zst at `output_path`: retention (auto_backup) sorts by
    # mtime and would happily count a corrupt partial file as the newest
    # snapshot, evicting a good one.
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    try:
        with tempfile.TemporaryDirectory() as td:
            db_copy = Path(td) / "tiro.db"
            _checkpointed_db_copy(config, db_copy)

            with _open_tar_zst_write(tmp_path) as tar:
                _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=2).encode())
                tar.add(db_copy, arcname="tiro.db")
                _add_bytes(tar, "config-snapshot.yaml", _sanitized_config_yaml(config).encode())
                if config.articles_dir.exists():
                    for md in sorted(config.articles_dir.glob("*.md")):
                        tar.add(md, arcname=f"articles/{md.name}")
                if config.wiki_dir.exists():
                    for page in sorted(config.wiki_dir.rglob("*.md")):
                        rel = page.relative_to(config.wiki_dir)
                        tar.add(page, arcname=f"wiki/{rel.as_posix()}")
                # Highlights + notes sidecars (Phase 2 M2.1): files-as-truth,
                # whole-library (unlike export.py, snapshots are never
                # filtered) -- restore's generic `dest = library /
                # member.name` fallback already handles these arcnames.
                ann_dir = annotations_dir(config)
                if ann_dir.exists():
                    for f in sorted(ann_dir.glob("*.jsonl")):
                        tar.add(f, arcname=f"annotations/{f.name}")
                nt_dir = notes_dir(config)
                if nt_dir.exists():
                    for f in sorted(nt_dir.glob("*.md")):
                        tar.add(f, arcname=f"notes/{f.name}")
                # Persona files (Phase 6 K3): community-shareable prompt
                # templates the user has installed/forked -- library content,
                # not regenerable, same treatment as wiki/. Traces
                # (agents/traces/) deliberately stay OUT of this allowlist
                # (K1-K2 owner-ratification item D15) -- do not add them here.
                pd = personas_dir(config)
                if pd.exists():
                    for f in sorted(pd.glob("*.md")):
                        tar.add(f, arcname=f"personas/{f.name}")
                _add_bytes(tar, "embeddings.jsonl", _dump_embeddings_jsonl(config))
                audio_dir = config.library / "audio"
                if include_audio and audio_dir.exists():
                    for mp3 in sorted(audio_dir.glob("*.mp3")):
                        tar.add(mp3, arcname=f"audio/{mp3.name}")
        os.replace(tmp_path, output_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

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

    If `snapshot_path` lives inside the library being restored (the default
    `tiro backup` output is `{library}/backups/manual/...`), it is copied to
    a tempfile BEFORE the library is displaced — otherwise pass 2 would try
    to re-open a path that `shutil.move` just relocated out from under it.
    The `backups/` directory of the displaced (pre-restore) library —
    snapshot history, an independent artifact and not library state — is
    moved back into the restored library at the end, so restoring from an
    in-library snapshot doesn't make the whole backup history (including the
    snapshot just restored from) disappear into the `.bak` sibling.

    If the restored database's schema_version is newer than this version of
    Tiro understands (`schema_newer_than_app` in the returned dict), a
    downgrade has occurred — the snapshot was made by a newer Tiro. This is
    logged loudly but not fatal; migrate_db() only applies forward
    migrations, so the schema is left as-is.
    """
    snapshot_path = Path(snapshot_path)
    library = config.library

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

    # If the snapshot is inside the library, copy it out first — the library
    # directory is about to be moved aside, which would otherwise leave
    # `snapshot_path` pointing nowhere for pass 2.
    read_path = snapshot_path
    temp_copy: Path | None = None
    if snapshot_path.resolve().is_relative_to(library.resolve()):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.zst") as tf:
            temp_copy = Path(tf.name)
        shutil.copy2(snapshot_path, temp_copy)
        read_path = temp_copy

    try:
        # The library is only displaced after the full archive has passed
        # validation. Displace the live library (keep it — this IS the
        # pre-restore backup).
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        displaced = library.parent / f"{library.name}.bak.{ts}"
        if library.exists():
            shutil.move(str(library), str(displaced))
        library.mkdir(parents=True, exist_ok=True)

        # Pass 2: extract (streaming tar needs a fresh open)
        embeddings_path = library / "embeddings.jsonl"
        with _open_tar_zst_read(read_path) as tar:
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
    finally:
        if temp_copy is not None:
            temp_copy.unlink(missing_ok=True)

    # Snapshot may predate current schema
    from tiro.database import migrate_db

    migrate_db(config.db_path)

    # Downgrade detection: the snapshot's schema may be NEWER than what this
    # Tiro version understands (restoring a newer-Tiro snapshot into an
    # older Tiro install). migrate_db only runs forward migrations, so a
    # newer schema_version is left untouched — surface it loudly rather than
    # silently proceeding against a schema this code wasn't written for.
    from tiro.migrations import LATEST_VERSION, schema_version

    conn = get_connection(config.db_path)
    try:
        restored_schema_version = schema_version(conn)
    finally:
        conn.close()
    schema_newer_than_app = restored_schema_version > LATEST_VERSION
    if schema_newer_than_app:
        logger.warning(
            "Restored snapshot's schema version (%d) is NEWER than this Tiro "
            "version supports (%d) — it was likely created by a newer Tiro. "
            "Upgrade Tiro before relying on this library.",
            restored_schema_version, LATEST_VERSION,
        )

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

    # Snapshot/backup history is an independent artifact, not library state —
    # bring the displaced library's backups/ back so it isn't stranded inside
    # the .bak sibling (this also restores the very snapshot just used above,
    # since it was copied out to `temp_copy` before displacement in item 1's
    # fix, not read from its original in-library path).
    displaced_backups = displaced / "backups"
    if displaced_backups.exists():
        shutil.move(str(displaced_backups), str(library / "backups"))

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
        "schema_newer_than_app": schema_newer_than_app,
    }
