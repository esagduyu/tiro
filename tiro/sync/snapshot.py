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
from dataclasses import dataclass, field
from dataclasses import replace as _dc_replace
from datetime import UTC, datetime, timedelta  # noqa: F401  (Task 6 uses timedelta)
from typing import Any  # noqa: F401  (Task 6 uses this)

from tiro.anchors import content_hash
from tiro.sync.crypto import CryptoError, SyncFormatError
from tiro.sync.journal import (
    SYNC_FORMAT,
    FilePut,
    JournalError,
    Op,
    canonical_json,
    ops_from_jsonl,
    ops_to_jsonl,
)
from tiro.sync.manifest import Manifest, ManifestEntry, Shadow, diff


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


# --- Snapshot manifest docs (spec para 5: snapshots/{ulid}/manifest.age) -----

#: Manifest kinds whose entries carry a content-addressed file body.
#: Must match S2 manifest.py's kind strings (S2 header decision #4).
FILE_KINDS = ("article", "note", "wiki", "pathfile")

_EMPTY_SHADOW = Shadow(entries={}, tombstones={}, aliases={})


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SnapshotDoc:
    snapshot_id: str
    created_at: str
    created_by: str
    covers: dict[str, int]  # device_id -> journal seq this snapshot subsumes
    manifest: Manifest
    objects: dict[str, str]  # path_hint -> blob address (objects/ key hash)


def build_snapshot(
    manifest: Manifest,
    *,
    snapshot_id: str,
    created_by: str,
    covers: dict[str, int],
    now: str | None = None,
    object_hashes: Mapping[str, str] | None = None,
) -> tuple[str, set[str]]:
    """-> (plaintext canonical-JSON doc, object ADDRESSES the doc references).
    Full state per spec para 5: every entity row + the object-address map.
    The caller must ensure every returned address exists in objects/ before
    uploading the snapshot (same objects-first ordering as segments).

    HASH SPACES (D-S3, mirrors manifest.hydrate_bodies): for kind="article"
    the manifest entry's `hash` is BODY-space (frontmatter-stripped,
    = articles.body_hash) while objects/ blobs are keyed by the FULL-file
    plaintext sha256 — so `object_hashes` maps path_hint -> blob address and
    is REQUIRED for article entries (never defaulted from entry.hash). For
    note/wiki/pathfile the two spaces coincide, so a missing map entry
    defaults to entry.hash. Keyed by path_hint, not uid — notes share their
    article's uid."""
    entries = []
    hashes: set[str] = set()
    for (kind, _uid), e in sorted(manifest.entries.items()):
        wire = {"kind": e.kind, "uid": e.uid, "hash": e.hash,
                "fields": e.fields, "hlc": e.hlc}
        if kind in FILE_KINDS and e.hash:
            path_hint = e.fields["path_hint"]
            address = object_hashes.get(path_hint) if object_hashes else None
            if address is None:
                if kind == "article":
                    raise SnapshotError(
                        f"no object address for {path_hint!r} — article entry "
                        "hashes are body-space, the caller must supply the "
                        "blob address")
                address = e.hash  # note/wiki/pathfile: spaces coincide
            wire["object"] = address
            hashes.add(address)
        entries.append(wire)
    doc = {
        "sync_format": SYNC_FORMAT,
        "snapshot": snapshot_id,
        "created_at": now or _now_iso(),
        "created_by": created_by,
        "covers": covers,
        "entries": entries,
    }
    return canonical_json(doc) + "\n", hashes


def parse_snapshot(text: str) -> SnapshotDoc:
    try:
        d = json.loads(text)
        if not isinstance(d, dict):
            raise SnapshotError("snapshot doc is not a JSON object")
        version = int(d["sync_format"])
        if version > SYNC_FORMAT:
            raise SyncFormatError(
                f"snapshot uses sync_format {version}, this build understands "
                f"{SYNC_FORMAT} — upgrade Tiro before syncing"
            )
        entries = {}
        objects: dict[str, str] = {}
        for e in d["entries"]:
            entry = ManifestEntry(kind=e["kind"], uid=e["uid"], hash=e.get("hash"),
                                  fields=e.get("fields") or {}, hlc=e.get("hlc"))
            entries[(entry.kind, entry.uid)] = entry
            if entry.kind in FILE_KINDS and entry.hash:
                address = e.get("object")
                if address is None:
                    if entry.kind == "article":
                        # An article entry's hash is body-space, never a blob
                        # address — a snapshot that cannot hydrate its
                        # articles is unreadable.
                        raise SnapshotError(
                            f"article entry {entry.uid!r} has no object "
                            "address — snapshot cannot hydrate its articles")
                    address = entry.hash  # note/wiki/pathfile: spaces coincide
                objects[entry.fields["path_hint"]] = address
        return SnapshotDoc(
            snapshot_id=d["snapshot"],
            created_at=d.get("created_at", ""),
            created_by=d.get("created_by", ""),
            covers={k: int(v) for k, v in (d.get("covers") or {}).items()},
            manifest=Manifest(entries=entries),
            objects=objects,
        )
    except (SnapshotError, SyncFormatError):
        raise
    except Exception as e:
        raise SnapshotError(f"unreadable snapshot doc: {e}") from e


def encode_snapshot(doc_text: str, codec) -> bytes:
    return codec.encrypt(doc_text.encode("utf-8"))


def decode_snapshot(blob: bytes, codec) -> SnapshotDoc:
    try:
        text = codec.decrypt(blob).decode("utf-8")
    except CryptoError:
        raise
    except UnicodeDecodeError as e:
        raise SnapshotError(f"snapshot blob is not valid UTF-8: {e}") from e
    return parse_snapshot(text)


def materialize_ops(doc: SnapshotDoc, objects_plain: Mapping[str, str]) -> list:
    """Snapshot -> ops, via S2's diff against an EMPTY shadow (decision #9 —
    bootstrap reuses the one merge path; no second materializer exists).
    `objects_plain` maps blob ADDRESS -> decrypted body for every address
    `build_snapshot` reported. S5's bootstrap feeds the result to apply_ops.

    diff emits article FilePuts with object_hash = the BODY-space manifest
    hash; hydration here resolves the blob address via doc.objects (keyed by
    path_hint) and rewrites object_hash to it — the hydrated-op shape
    apply_ops expects (it treats object_hash as a blob address only)."""
    ops = diff(doc.manifest, _EMPTY_SHADOW)
    out = []
    for op in ops:
        if isinstance(op, FilePut) and op.body is None:
            addr = doc.objects.get(op.path_hint, op.object_hash)
            if addr not in objects_plain:
                raise SnapshotError(
                    f"snapshot references missing object {addr!r}")
            op = _dc_replace(op, body=objects_plain[addr], object_hash=addr)
        out.append(op)
    return out


# --- Device registry docs (spec para 5: devices/{device_id}.json, plaintext) -


@dataclass(frozen=True)
class DeviceInfo:
    device_id: str
    name: str = ""
    last_seen: str = ""
    last_seq: int = 0
    app_version: str = ""
    acked: dict[str, int] = field(default_factory=dict)


def encode_device_doc(info: DeviceInfo) -> str:
    return canonical_json({
        "name": info.name, "last_seen": info.last_seen,
        "last_seq": info.last_seq, "app_version": info.app_version,
        "acked": info.acked,
    }) + "\n"


def parse_device_doc(device_id: str, text: str) -> DeviceInfo:
    try:
        d = json.loads(text)
        if not isinstance(d, dict):
            raise SnapshotError("device doc is not a JSON object")
        return DeviceInfo(
            device_id=device_id,
            name=d.get("name", ""),
            last_seen=d.get("last_seen", ""),
            last_seq=int(d.get("last_seq", 0)),
            app_version=d.get("app_version", ""),
            acked={k: int(v) for k, v in (d.get("acked") or {}).items()},
        )
    except SnapshotError:
        raise
    except Exception as e:
        raise SnapshotError(f"unreadable device doc for {device_id!r}: {e}") from e
