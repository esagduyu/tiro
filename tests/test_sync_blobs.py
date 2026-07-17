"""Sync S3: spec-para-5 key layout + object/segment blob (de)serialization."""
import base64

import pytest

from tiro.anchors import content_hash
from tiro.sync.crypto import CryptoError, KdfParams, PlainCodec, codec_from_passphrase
from tiro.sync.journal import HLC, FilePut, JournalError, Meta
from tiro.sync.snapshot import (
    FORMAT_KEY,
    LOCK_KEY,
    QUARANTINE_ERRORS,
    SnapshotError,
    decode_object,
    decode_segment,
    device_key,
    encode_object,
    encode_segment,
    journal_key,
    object_key,
    parse_journal_key,
    parse_object_key,
    snapshot_key,
)

KDF = KdfParams(salt_b64=base64.b64encode(b"\x03" * 16).decode(), m=8, t=1, p=1)


@pytest.fixture(params=["plain", "age"])
def codec(request):
    return PlainCodec() if request.param == "plain" else codec_from_passphrase("pw", KDF)


def _ops():
    hlc = HLC(1720000000000, 0, "dev-a")
    body = "# Hello\n\nBody text éà.\n"
    return [
        FilePut(op_id="01OP0000000000000000000001", hlc=hlc, device="dev-a",
                uid="01ART0000000000000000000A1",
                path_hint="articles/hello.md",
                object_hash=content_hash(body), base_hash=None, body=body),
        Meta(op_id="01OP0000000000000000000002", hlc=hlc, device="dev-a",
             uid="01ART0000000000000000000A1",
             field="rating", value=2, ts="2026-07-11T00:00:00Z"),
    ]


class TestLayoutKeys:
    """Spec para 5 layout, byte-for-byte FROZEN."""

    def test_all_keys(self):
        h = "ab" + "0" * 62
        assert FORMAT_KEY == "format.json"
        assert LOCK_KEY == "locks/sync.lock"
        assert device_key("dev-a") == "devices/dev-a.json"
        assert journal_key("dev-a", 7) == "journal/dev-a/000000000007.age"
        assert object_key(h) == f"objects/ab/{h}.age"
        assert snapshot_key("01SNAP000000000000000000X1") == \
            "snapshots/01SNAP000000000000000000X1/manifest.age"

    def test_journal_key_roundtrip(self):
        assert parse_journal_key(journal_key("my-laptop", 42)) == ("my-laptop", 42)
        with pytest.raises(SnapshotError):
            parse_journal_key("journal/dev/notanumber.age")
        with pytest.raises(SnapshotError):
            parse_journal_key("objects/ab/cd.age")

    def test_object_key_roundtrip(self):
        h = content_hash("x")
        assert parse_object_key(object_key(h)) == h
        with pytest.raises(SnapshotError):
            parse_object_key("journal/dev/000000000001.age")


class TestObjects:
    def test_roundtrip_and_plaintext_hash_naming(self, codec):
        body = "content — with unicode\n"
        h, blob = encode_object(body, codec)
        assert h == content_hash(body)  # plaintext hash (equality leakage accepted, spec para 5)
        assert decode_object(blob, codec, expected_hash=h) == body

    def test_hash_mismatch_is_snapshot_error(self, codec):
        _, blob = encode_object("real body", codec)
        with pytest.raises(SnapshotError):
            decode_object(blob, codec, expected_hash="0" * 64)


class TestSegments:
    def test_roundtrip(self, codec):
        ops = _ops()
        seg, objects = encode_segment(ops, codec)
        assert set(objects) == {ops[0].object_hash}
        assert decode_segment(seg, codec, objects) == ops

    def test_body_never_plaintext_in_encrypted_segment(self):
        codec = codec_from_passphrase("pw", KDF)
        seg, objects = encode_segment(_ops(), codec)
        assert b"Body text" not in seg
        assert all(b"Body text" not in blob for blob in objects.values())

    def test_missing_object_blob_is_journal_error(self, codec):
        seg, _ = encode_segment(_ops(), codec)
        with pytest.raises(JournalError):
            decode_segment(seg, codec, {})

    def test_quarantine_errors_tuple(self):
        assert SnapshotError in QUARANTINE_ERRORS
        assert CryptoError in QUARANTINE_ERRORS
        assert JournalError in QUARANTINE_ERRORS
