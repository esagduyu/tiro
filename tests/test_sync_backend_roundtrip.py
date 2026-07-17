"""Sync S3 milestone gate: library -> complete spec-para-5 backend dir ->
fresh library, byte-equal file bodies, via BOTH the journal path and the
snapshot path, in BOTH plaintext and age-encrypted modes.

Uses a real tempdir tree written with pathlib (skeleton: 'plain dict/tempdir
stand-in' -- the FilesystemAdapter arrives in S4)."""
import base64
import json
from pathlib import Path

import frontmatter
import pytest

from tiro.anchors import content_hash
from tiro.database import get_connection
from tiro.ingestion.processor import process_article
from tiro.migrations import new_ulid
from tiro.sync.crypto import (
    KdfParams,
    build_format_json,
    codec_from_passphrase,
    open_format,
)
from tiro.sync.journal import HLC, FilePut
from tiro.sync.manifest import Shadow, build_manifest, diff, hydrate_bodies
from tiro.sync.merge import apply_ops
from tiro.sync.snapshot import (
    DeviceInfo,
    build_snapshot,
    decode_object,
    decode_segment,
    decode_snapshot,
    device_key,
    encode_device_doc,
    encode_object,
    encode_segment,
    encode_snapshot,
    journal_key,
    materialize_ops,
    object_key,
    snapshot_key,
)

KDF = KdfParams(salt_b64=base64.b64encode(b"\x06" * 16).decode(), m=8, t=1, p=1)


def _fresh_library(root: Path):
    """Throwaway receiving library: init_db only -- NO ChromaDB (apply's
    vector ops are best-effort try/except; replicates
    tests/test_sync_properties.py::_mini_lib rather than importing across
    test modules)."""
    from tiro.config import TiroConfig
    from tiro.database import init_db

    (root / "articles").mkdir(parents=True)
    config = TiroConfig(library_path=str(root))
    init_db(config.db_path)
    return config


def _populate(config):
    """A small but representative library: 2 articles (one unicode-heavy),
    a highlight with a note, an article-level note, a rating."""
    from tiro.annotations import append_highlight, write_note

    a1 = process_article(title="Hello World", author="A. Writer",
                         content_md="# Hello\n\nSome body text to highlight.\n",
                         url="https://example.com/hello", config=config)
    process_article(title="Unicode Été", author="B. Öz",
                    content_md="# Été\n\nCorps — “cité” à noël.\n",
                    url="https://example.com/ete", config=config)
    conn = get_connection(config.db_path)
    try:
        arow = conn.execute("SELECT * FROM articles WHERE id = ?",
                            (a1["id"],)).fetchone()
        body = frontmatter.load(str(config.articles_dir / arow["markdown_path"])).content
        start = body.index("body")
        append_highlight(config, conn, arow,
                         quote="body", prefix=body[max(0, start - 8):start],
                         suffix=body[start + 4:start + 12],
                         position_start=start, position_end=start + 4,
                         content_hash=content_hash(body), color="yellow",
                         note_markdown="my synced note")
        conn.execute("UPDATE articles SET rating = 2, "
                     "meta_updated_at = '2026-07-11T00:00:00Z' WHERE id = ?",
                     (a1["id"],))
        conn.commit()
        write_note(config, Path(arow["markdown_path"]).stem, "Article-level note.\n")
    finally:
        conn.close()


def _write_backend(config, root: Path, codec, fmt_text: str):
    """Full spec-para-5 tree: format.json, device doc, one journal segment
    (+ objects), one snapshot (+ its objects)."""
    (root / "journal" / "dev-a").mkdir(parents=True)
    (root / "devices").mkdir()
    (root / "locks").mkdir()  # empty; the lock file itself is S5 territory
    (root / "format.json").write_text(fmt_text)

    manifest = build_manifest(config)
    ops = hydrate_bodies(config, diff(manifest, Shadow()))
    seg, objects = encode_segment(ops, codec)
    (root / journal_key("dev-a", 1)).write_bytes(seg)
    for h, blob in objects.items():
        p = root / object_key(h)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)

    snap_id = new_ulid()
    # D-S3: article manifest hashes are BODY-space while objects/ blobs are
    # keyed by the FULL-file plaintext sha256 — build_snapshot requires the
    # path_hint -> blob-address map (hydrated object_hash IS the address).
    object_hashes = {op.path_hint: op.object_hash
                     for op in ops if isinstance(op, FilePut)}
    doc_text, hashes = build_snapshot(manifest, snapshot_id=snap_id,
                                      created_by="dev-a", covers={"dev-a": 1},
                                      object_hashes=object_hashes)
    bodies = {op.object_hash: op.body for op in ops
              if isinstance(op, FilePut) and op.body is not None}
    for h in hashes:
        p = root / object_key(h)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(encode_object(bodies[h], codec)[1])
    snap_path = root / snapshot_key(snap_id)
    snap_path.parent.mkdir(parents=True)
    snap_path.write_bytes(encode_snapshot(doc_text, codec))

    (root / device_key("dev-a")).write_text(encode_device_doc(DeviceInfo(
        device_id="dev-a", name="writer", last_seen="2026-07-11T00:00:00Z",
        last_seq=1, app_version="0.7.0", acked={})))
    return snap_id


def _read_objects(root: Path) -> dict[str, bytes]:
    return {p.stem: p.read_bytes() for p in (root / "objects").rglob("*.age")}


def _assert_equal_libraries(src, dst):
    for sub in ("articles", "notes"):
        src_files = {p.name: p.read_bytes()
                     for p in (Path(src.library) / sub).glob("*.md")}
        dst_files = {p.name: p.read_bytes()
                     for p in (Path(dst.library) / sub).glob("*.md")}
        assert dst_files == src_files, f"{sub}/ not byte-equal"

    # Annotations: per-uid line equality (writer may reorder deterministically).
    def _lines(cfg):
        out = {}
        d = Path(cfg.library) / "annotations"
        for p in (d.glob("*.jsonl") if d.exists() else []):
            for line in p.read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    out[rec["uid"]] = rec
        return out

    assert _lines(dst) == _lines(src)
    sconn, dconn = get_connection(src.db_path), get_connection(dst.db_path)
    try:
        s = sconn.execute("SELECT rating FROM articles WHERE url = ?",
                          ("https://example.com/hello",)).fetchone()
        d = dconn.execute("SELECT rating FROM articles WHERE url = ?",
                          ("https://example.com/hello",)).fetchone()
        assert d is not None and d["rating"] == s["rating"] == 2
        assert dconn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"] == 2
    finally:
        sconn.close()
        dconn.close()


@pytest.fixture(params=["plain", "age"])
def crypto_mode(request):
    if request.param == "plain":
        return build_format_json("01LIBTEST0000000000000001"), None
    codec = codec_from_passphrase("hunter2", KDF)
    return (build_format_json("01LIBTEST0000000000000001", kdf=KDF,
                              age_recipient=codec.recipient),
            "hunter2")


def test_journal_roundtrip(initialized_library, tmp_path, crypto_mode):
    """Writer library -> backend dir -> fresh library via journal segments."""
    fmt_text, passphrase = crypto_mode
    _, codec = open_format(fmt_text, passphrase=passphrase)
    _populate(initialized_library)
    root = tmp_path / "backend"
    _write_backend(initialized_library, root, codec, fmt_text)

    # Fresh device: open format (wrong passphrase would refuse cleanly here),
    # decode the segment, apply.
    _, codec2 = open_format((root / "format.json").read_text(),
                            passphrase=passphrase)
    ops = decode_segment((root / journal_key("dev-a", 1)).read_bytes(),
                         codec2, _read_objects(root))
    fresh = _fresh_library(tmp_path / "fresh")
    report = apply_ops(fresh, ops)
    assert not report.errors, report.details
    _assert_equal_libraries(initialized_library, fresh)


def test_snapshot_bootstrap_roundtrip(initialized_library, tmp_path, crypto_mode):
    """Writer library -> backend dir -> fresh library via the SNAPSHOT
    (the S5 bootstrap path, spec para 6.6)."""
    fmt_text, passphrase = crypto_mode
    _, codec = open_format(fmt_text, passphrase=passphrase)
    _populate(initialized_library)
    root = tmp_path / "backend"
    snap_id = _write_backend(initialized_library, root, codec, fmt_text)

    _, codec2 = open_format((root / "format.json").read_text(),
                            passphrase=passphrase)
    doc = decode_snapshot((root / snapshot_key(snap_id)).read_bytes(), codec2)
    objects_plain = {h: decode_object(b, codec2, expected_hash=h)
                     for h, b in _read_objects(root).items()}
    ops = materialize_ops(doc, objects_plain)
    fresh = _fresh_library(tmp_path / "fresh")
    report = apply_ops(fresh, ops)
    assert not report.errors, report.details
    _assert_equal_libraries(initialized_library, fresh)


def test_bootstrap_then_older_tail_op_applies(initialized_library, tmp_path):
    """S3.5-fix commitment (review Major #1): materialize_ops' epoch-pinned
    HLC stamps must let a journal-tail op whose wall time is far OLDER than
    the bootstrap moment — but newer than the snapshot's epoch stamps —
    still apply, never skip-as-stale. That is the covers contract."""
    fmt_text = build_format_json("01LIBTEST0000000000000001")
    _, codec = open_format(fmt_text)
    _populate(initialized_library)
    root = tmp_path / "backend"
    snap_id = _write_backend(initialized_library, root, codec, fmt_text)

    # Bootstrap the fresh library from the snapshot.
    doc = decode_snapshot((root / snapshot_key(snap_id)).read_bytes(), codec)
    objects_plain = {h: decode_object(b, codec, expected_hash=h)
                     for h, b in _read_objects(root).items()}
    fresh = _fresh_library(tmp_path / "fresh")
    report = apply_ops(fresh, materialize_ops(doc, objects_plain))
    assert not report.errors, report.details

    # A journal-tail edit stamped at a tiny wall time (1ms after epoch):
    # pre-bootstrap by wall-clock, post-snapshot by HLC order.
    conn = get_connection(fresh.db_path)
    try:
        row = conn.execute(
            "SELECT uid, markdown_path FROM articles WHERE url = ?",
            ("https://example.com/hello",)).fetchone()
    finally:
        conn.close()
    doc_path = fresh.articles_dir / Path(row["markdown_path"]).name
    current = doc_path.read_text()
    new_full = current + "\nAppended by a pre-bootstrap journal tail.\n"
    op = FilePut(op_id=new_ulid(), hlc=HLC(1, 0, "dev-a"), device="dev-a",
                 uid=row["uid"], path_hint=f"articles/{doc_path.name}",
                 object_hash=content_hash(new_full),
                 base_hash=content_hash(frontmatter.loads(current).content),
                 body=new_full)
    report2 = apply_ops(fresh, [op])
    assert report2.skipped_stale == 0, report2.details
    assert report2.applied >= 1, report2.details
    assert "Appended by a pre-bootstrap journal tail." in doc_path.read_text()
