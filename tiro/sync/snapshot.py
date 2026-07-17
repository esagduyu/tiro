"""Sync backend blob layer (S3): spec-para-5 layout, snapshots, GC planning.

Backend layout (FROZEN, byte-for-byte):
    format.json                      plaintext (crypto.py owns its content)
    devices/{device_id}.json         plaintext device registry docs
    journal/{device_id}/{seq:012}.age  append-only op segments (JSONL inside)
    objects/{h2}/{sha256}.age        content-addressed file bodies
                                     (PLAINTEXT sha256 in the name — single-user
                                     bucket, equality leakage accepted, spec para 5)
    snapshots/{ulid}/manifest.age    full-state snapshot manifests
    locks/sync.lock                  advisory lock (S5 territory)

PURE module: bytes in, bytes out. No adapter abstraction here (S4 owns the
async put/get/list/delete/lock/unlock contract); no engine loop (S5).
Corruption of any kind raises one of QUARANTINE_ERRORS — S5 quarantines the
cycle (needs_attention), never half-applies (spec para 6.7).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field  # noqa: F401  (Tasks 5-6 use these)
from datetime import UTC, datetime, timedelta  # noqa: F401  (Tasks 5-6 use these)
from typing import Any  # noqa: F401  (Tasks 5-6 use this)

from tiro.anchors import content_hash
from tiro.sync.crypto import CryptoError, SyncFormatError
from tiro.sync.journal import (
    SYNC_FORMAT,  # noqa: F401  (Tasks 5-6 use this)
    FilePut,  # noqa: F401  (Tasks 5-6 use this)
    JournalError,
    Op,
    canonical_json,  # noqa: F401  (Tasks 5-6 use this)
    ops_from_jsonl,
    ops_to_jsonl,
)


class SnapshotError(ValueError):
    """Snapshot/object/device blob that cannot be read faithfully."""


#: Any of these during a pull/apply => quarantine the cycle (spec para 6.7).
QUARANTINE_ERRORS = (CryptoError, SyncFormatError, JournalError, SnapshotError)


# --- Key layout (spec para 5, FROZEN) ---------------------------------------

FORMAT_KEY = "format.json"
LOCK_KEY = "locks/sync.lock"

_JOURNAL_KEY_RE = re.compile(r"^journal/([^/]+)/(\d{12})\.age$")
_OBJECT_KEY_RE = re.compile(r"^objects/([0-9a-f]{2})/([0-9a-f]{64})\.age$")


def device_key(device_id: str) -> str:
    return f"devices/{device_id}.json"


def journal_key(device_id: str, seq: int) -> str:
    return f"journal/{device_id}/{seq:012d}.age"


def object_key(sha256_hex: str) -> str:
    return f"objects/{sha256_hex[:2]}/{sha256_hex}.age"


def snapshot_key(snapshot_id: str) -> str:
    return f"snapshots/{snapshot_id}/manifest.age"


def parse_journal_key(key: str) -> tuple[str, int]:
    m = _JOURNAL_KEY_RE.match(key)
    if not m:
        raise SnapshotError(f"not a journal segment key: {key!r}")
    if m.group(1) in (".", ".."):
        # Traversal hardening (S3.4 review Minor #3): "." / ".." round-trip
        # the regex cleanly but would escape the backend root if an adapter
        # joined keys naively. Device ids are locally generated ULID-ish
        # strings; adapters must still sanitize their own key joins.
        raise SnapshotError(f"not a journal segment key: {key!r}")
    return m.group(1), int(m.group(2))


def parse_object_key(key: str) -> str:
    m = _OBJECT_KEY_RE.match(key)
    if not m or m.group(2)[:2] != m.group(1):
        raise SnapshotError(f"not an object key: {key!r}")
    return m.group(2)


# --- Object blobs ------------------------------------------------------------


def encode_object(body: str, codec) -> tuple[str, bytes]:
    """-> (plaintext sha256 hex, encrypted blob). The hash names the blob
    (content addressing); it is the hash of the PLAINTEXT (spec para 5)."""
    return content_hash(body), codec.encrypt(body.encode("utf-8"))


def decode_object(blob: bytes, codec, *, expected_hash: str | None = None) -> str:
    try:
        body = codec.decrypt(blob).decode("utf-8")
    except CryptoError:
        raise
    except UnicodeDecodeError as e:
        raise SnapshotError(f"object blob is not valid UTF-8: {e}") from e
    if expected_hash is not None and content_hash(body) != expected_hash:
        raise SnapshotError("object content does not match its content-address hash")
    return body


# --- Journal segment blobs ----------------------------------------------------


def encode_segment(ops: list[Op], codec) -> tuple[bytes, dict[str, bytes]]:
    """-> (encrypted segment blob, encrypted object blobs by plaintext hash).
    S5 uploads the objects FIRST, then the segment (spec para 6.4 —
    crash between the two leaves unreferenced objects, harmless, GC'd)."""
    text, objects = ops_to_jsonl(ops)
    return (
        codec.encrypt(text.encode("utf-8")),
        {h: codec.encrypt(body.encode("utf-8")) for h, body in objects.items()},
    )


def decode_segment(blob: bytes, codec, objects: Mapping[str, bytes]) -> list[Op]:
    """Decrypt + parse a segment, decrypting exactly the object blobs its
    file_put ops reference. Raises a QUARANTINE_ERRORS member on ANY
    corruption — callers must treat that as quarantine, never skip-a-line."""
    try:
        text = codec.decrypt(blob).decode("utf-8")
    except CryptoError:
        raise
    except UnicodeDecodeError as e:
        raise JournalError(f"segment blob is not valid UTF-8: {e}") from e

    needed: set[str] = set()
    # split("\n"), NEVER splitlines(): canonical_json(ensure_ascii=False)
    # legally emits U+0085/U+2028/U+2029 raw inside JSON strings, and
    # splitlines() shears such a line mid-string — the exact S2.8 bug
    # ops_from_jsonl already guards against (S3.4 review Blocker #1).
    for lineno, raw in enumerate(text.split("\n"), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except Exception as e:
            raise JournalError(f"invalid JSON at segment line {lineno}: {e}") from e
        # isinstance guards (S3.4 review Major #2): a valid-JSON line with a
        # malformed shape must fall through to ops_from_jsonl's JournalError,
        # never AttributeError/TypeError out of this pre-scan.
        if isinstance(d, dict) and d.get("kind") == "file_put":
            payload = d.get("payload")
            if isinstance(payload, dict):
                h = payload.get("object_hash")
                if isinstance(h, str) and h:
                    needed.add(h)

    plain: dict[str, str] = {}
    for h in sorted(needed):
        if h not in objects:
            raise JournalError(f"segment references missing object {h!r}")
        plain[h] = decode_object(objects[h], codec, expected_hash=h)
    return ops_from_jsonl(text, plain)
