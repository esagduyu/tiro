"""Sync S3: snapshot manifest doc + device registry docs + materialization."""
import base64
import json

import pytest

from tiro.anchors import content_hash
from tiro.sync.crypto import KdfParams, SyncFormatError, codec_from_passphrase
from tiro.sync.journal import FilePut
from tiro.sync.manifest import Manifest, ManifestEntry
from tiro.sync.snapshot import (
    DeviceInfo,
    SnapshotError,
    build_snapshot,
    decode_snapshot,
    encode_device_doc,
    encode_snapshot,
    materialize_ops,
    parse_device_doc,
    parse_snapshot,
)

KDF = KdfParams(salt_b64=base64.b64encode(b"\x04" * 16).decode(), m=8, t=1, p=1)
BODY = "# Hello\n\nSnapshot body.\n"

# D-S3 hash-space adaptation: article manifest entries carry BODY-space hashes
# while objects/ blobs are keyed by FULL-file addresses, so build_snapshot
# takes an explicit {path_hint: blob address} map. In this test BODY serves as
# the whole file, so the two spaces coincide NUMERICALLY — the map is still
# mandatory for article entries (the space distinction is structural).
OBJECTS = {"articles/hello.md": content_hash(BODY)}


def _manifest():
    """One file entry + one row entry — enough to exercise both branches.
    NOTE: construct entries the way S2's build_manifest does; if ManifestEntry
    field names differ as landed, adapt HERE and in snapshot.py, nowhere else."""
    e_file = ManifestEntry(kind="article", uid="01ART0000000000000000000A1",
                           hash=content_hash(BODY),
                           fields={"path_hint": "articles/hello.md", "rating": None,
                                   "is_read": 0, "snoozed_until": None,
                                   "opened_count": 0, "source_uid": "01SRC01",
                                   "url": "https://example.com/hello"},
                           # HLC string format: {wall_ms:013d}-{counter:06d}-{device}
                           hlc="1720000000000-000000-dev-a")
    e_row = ManifestEntry(kind="row:sources", uid="01SRC01", hash=None,
                          fields={"uid": "01SRC01", "name": "Example",
                                  "domain": "example.com", "email_sender": None,
                                  "source_type": "web", "is_vip": 0,
                                  "created_at": "2026-07-01 00:00:00"},
                          hlc="1720000000000-000001-dev-a")
    return Manifest(entries={(e.kind, e.uid): e for e in (e_file, e_row)})


class TestSnapshotDoc:
    def test_build_parse_roundtrip(self):
        text, hashes = build_snapshot(
            _manifest(), snapshot_id="01SNAPX", created_by="dev-a",
            covers={"dev-a": 3, "dev-b": 1}, now="2026-07-11T00:00:00Z",
            object_hashes=OBJECTS)
        d = json.loads(text)
        assert d["sync_format"] == 1
        assert d["covers"] == {"dev-a": 3, "dev-b": 1}
        assert [e["kind"] for e in d["entries"]] == sorted(e["kind"] for e in d["entries"])
        # The article entry's wire dict carries the blob ADDRESS alongside its
        # body-space hash; non-file entries carry no "object" key.
        by_kind = {e["kind"]: e for e in d["entries"]}
        assert by_kind["article"]["object"] == content_hash(BODY)
        assert "object" not in by_kind["row:sources"]
        doc = parse_snapshot(text)
        assert doc.snapshot_id == "01SNAPX" and doc.created_by == "dev-a"
        assert doc.manifest.entries.keys() == _manifest().entries.keys()
        assert doc.objects == {"articles/hello.md": content_hash(BODY)}
        # Only file-kind entries contribute object hashes — the ADDRESSES.
        assert hashes == {content_hash(BODY)}

    def test_encode_decode_encrypted(self):
        codec = codec_from_passphrase("pw", KDF)
        text, _ = build_snapshot(_manifest(), snapshot_id="01SNAPX",
                                 created_by="dev-a", covers={},
                                 now="2026-07-11T00:00:00Z",
                                 object_hashes=OBJECTS)
        blob = encode_snapshot(text, codec)
        assert b"example.com" not in blob
        assert decode_snapshot(blob, codec).snapshot_id == "01SNAPX"

    def test_newer_format_refused(self):
        text, _ = build_snapshot(_manifest(), snapshot_id="01SNAPX",
                                 created_by="dev-a", covers={},
                                 object_hashes=OBJECTS)
        d = json.loads(text)
        d["sync_format"] = 99
        with pytest.raises(SyncFormatError):
            parse_snapshot(json.dumps(d))

    def test_garbage_is_snapshot_error(self):
        with pytest.raises(SnapshotError):
            parse_snapshot("not json")

    def test_article_without_object_hashes_refused(self):
        # Article entry hashes are BODY-space, never a blob address — the
        # caller MUST supply the address; there is no fallback.
        with pytest.raises(SnapshotError):
            build_snapshot(_manifest(), snapshot_id="01SNAPX",
                           created_by="dev-a", covers={})

    def test_parse_article_entry_without_object_refused(self):
        text, _ = build_snapshot(_manifest(), snapshot_id="01SNAPX",
                                 created_by="dev-a", covers={},
                                 object_hashes=OBJECTS)
        d = json.loads(text)
        for e in d["entries"]:
            e.pop("object", None)
        # A snapshot that cannot hydrate its articles is unreadable.
        with pytest.raises(SnapshotError):
            parse_snapshot(json.dumps(d))

    def test_parse_note_entry_without_object_falls_back_to_hash(self):
        # note/wiki/pathfile hash spaces coincide — a missing "object" key
        # tolerably defaults to the entry hash.
        note_hash = content_hash("a note body\n")
        d = {
            "sync_format": 1,
            "snapshot": "01SNAPY",
            "created_at": "2026-07-11T00:00:00Z",
            "created_by": "dev-a",
            "covers": {},
            "entries": [{"kind": "note", "uid": "01ART0000000000000000000A1",
                         "hash": note_hash,
                         "fields": {"path_hint": "notes/hello.md"},
                         "hlc": "1720000000000-000002-dev-a"}],
        }
        doc = parse_snapshot(json.dumps(d))
        assert doc.objects == {"notes/hello.md": note_hash}


class TestMaterialize:
    def test_materialize_hydrates_file_bodies(self):
        text, hashes = build_snapshot(_manifest(), snapshot_id="01SNAPX",
                                      created_by="dev-a", covers={},
                                      object_hashes=OBJECTS)
        doc = parse_snapshot(text)
        ops = materialize_ops(doc, {h: BODY for h in hashes})
        file_puts = [op for op in ops if isinstance(op, FilePut)]
        assert len(file_puts) == 1 and file_puts[0].body == BODY
        # object_hash is rewritten to the blob ADDRESS while hydrating —
        # the hydrated-op shape apply_ops expects.
        assert file_puts[0].object_hash == content_hash(BODY)
        assert len(ops) >= 2  # file op + at least the row op

    def test_missing_object_is_snapshot_error(self):
        text, _ = build_snapshot(_manifest(), snapshot_id="01SNAPX",
                                 created_by="dev-a", covers={},
                                 object_hashes=OBJECTS)
        with pytest.raises(SnapshotError):
            materialize_ops(parse_snapshot(text), {})


class TestDeviceDocs:
    def test_roundtrip(self):
        info = DeviceInfo(device_id="dev-a", name="Ege's MBP",
                          last_seen="2026-07-11T00:00:00Z", last_seq=42,
                          app_version="0.7.0", acked={"dev-b": 7})
        back = parse_device_doc("dev-a", encode_device_doc(info))
        assert back == info

    def test_parse_tolerates_missing_optional_keys(self):
        back = parse_device_doc("dev-a", '{"name": "x"}')
        assert back.last_seq == 0 and back.acked == {}

    def test_garbage_is_snapshot_error(self):
        with pytest.raises(SnapshotError):
            parse_device_doc("dev-a", "][")
