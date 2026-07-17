"""Sync S3: corrupted-blob matrix — every corruption raises a
QUARANTINE_ERRORS member, never partial output (spec para 6.7)."""
import base64
from pathlib import Path

import pytest

import tiro.sync
from tiro.sync.crypto import CryptoError, KdfParams, PlainCodec, codec_from_passphrase
from tiro.sync.journal import HLC, FilePut, JournalError
from tiro.sync.snapshot import QUARANTINE_ERRORS, decode_segment, encode_segment

KDF = KdfParams(salt_b64=base64.b64encode(b"\x05" * 16).decode(), m=8, t=1, p=1)


def _seg(codec):
    from tiro.anchors import content_hash
    body = "# B\n"
    op = FilePut(op_id="01OP0000000000000000000001",
                 hlc=HLC(1720000000000, 0, "dev-a"), device="dev-a",
                 uid="01ART0000000000000000000A1", path_hint="articles/b.md",
                 object_hash=content_hash(body), body=body)
    return encode_segment([op], codec)


class TestCorruptionMatrix:
    def test_bitflipped_encrypted_segment(self):
        codec = codec_from_passphrase("pw", KDF)
        seg, objects = _seg(codec)
        bad = bytearray(seg)
        bad[len(bad) // 2] ^= 0xFF
        with pytest.raises(QUARANTINE_ERRORS):  # age is authenticated: CryptoError
            decode_segment(bytes(bad), codec, objects)

    def test_truncated_encrypted_segment(self):
        codec = codec_from_passphrase("pw", KDF)
        seg, objects = _seg(codec)
        with pytest.raises(QUARANTINE_ERRORS):
            decode_segment(seg[: len(seg) // 2], codec, objects)

    def test_wrong_key(self):
        seg, objects = _seg(codec_from_passphrase("pw", KDF))
        with pytest.raises(CryptoError):
            decode_segment(seg, codec_from_passphrase("other", KDF), objects)

    def test_bitflipped_plaintext_segment_is_journal_error(self):
        codec = PlainCodec()
        seg, objects = _seg(codec)
        bad = seg.replace(b'"kind"', b'"k!nd"', 1)
        with pytest.raises(JournalError):
            decode_segment(bad, codec, objects)

    def test_valid_encryption_of_garbage_jsonl(self):
        codec = codec_from_passphrase("pw", KDF)
        with pytest.raises(JournalError):
            decode_segment(codec.encrypt(b"not json\n"), codec, {})

    def test_corrupted_object_blob(self):
        codec = codec_from_passphrase("pw", KDF)
        seg, objects = _seg(codec)
        h = next(iter(objects))
        bad = bytearray(objects[h])
        bad[len(bad) // 2] ^= 0xFF
        with pytest.raises(QUARANTINE_ERRORS):
            decode_segment(seg, codec, {h: bytes(bad)})

    def test_object_swapped_for_wrong_content(self):
        """Blob decrypts fine but content doesn't match its hash name
        (decision #14) — still quarantine, never a silently wrong body."""
        codec = PlainCodec()
        seg, objects = _seg(codec)
        h = next(iter(objects))
        with pytest.raises(QUARANTINE_ERRORS):
            decode_segment(seg, codec, {h: b"totally different body"})

    def test_no_partial_ops_on_midstream_corruption(self):
        """A bad line ANYWHERE means zero ops returned — quarantine is
        all-or-nothing, never skip-the-bad-line."""
        codec = PlainCodec()
        seg, objects = _seg(codec)
        two = seg + b'{"broken": \n'
        with pytest.raises(JournalError):
            decode_segment(two, codec, objects)


def test_pure_sync_modules_have_no_network_imports():
    """Zero-I/O gate over the S3 modules (S2's gate covers journal/manifest/
    merge in tests/test_sync_properties.py; keep both lists in sync)."""
    import re
    banned = re.compile(
        r"^\s*(import|from)\s+(httpx|requests|socket|urllib|anthropic|openai)\b",
        re.M,
    )
    pkg = Path(tiro.sync.__file__).parent
    for mod in ("crypto", "snapshot"):
        src = (pkg / f"{mod}.py").read_text()
        assert not banned.search(src), f"tiro/sync/{mod}.py imports a network module"
