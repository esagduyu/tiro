"""Sync engine — the IMPURE orchestrator for BYO sync (Phase 7a S5).

Pure logic lives in the sibling modules (journal/manifest/merge/snapshot/
crypto); this module owns device identity, sync_state persistence
(migration 018) and — in later S5 tasks — the push/pull cycle itself.
The cycle order is FROZEN by docs/plans/2026-07-06-sync-engine-spec.md §6.

This first slice holds only the sync_state helpers: device identity
(get_or_create_device / device_short) and the registry/watermark
read/update functions every later task consumes.
"""

import asyncio
import json
import logging
import platform
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from dataclasses import replace as dc_replace
from datetime import UTC, datetime

from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.migrations import new_ulid
from tiro.sync.journal import FileDel, FilePut, HLCClock, Meta, RowDel

logger = logging.getLogger(__name__)

LOCK_TTL_S = 120  # backend advisory-lock TTL, used by the S5.4 cycle

#: Cap on alias-chain hops in _remap_alias_uids — a chain deeper than this
#: (or any cycle) leaves the op untouched rather than looping.
_ALIAS_CHAIN_CAP = 20

#: Content-address shape (S5.3 review B1): the ONLY object refs the pull
#: loop ever fetches. Anything else (e.g. a traversal like "../../secrets")
#: is left unfetched so decode_segment raises its honest JournalError and
#: the quarantine branch fires — a malformed ref must never reach
#: adapter.get, where it would escape as an AdapterError (not a
#: QUARANTINE_ERRORS member).
_OBJECT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class CycleReport:
    """One sync cycle's outcome — persisted as last_cycle_json and surfaced
    by status/CLI. `errors` counts per-op apply failures (the cycle still
    completes); `result` != "ok" means the cycle stopped early."""

    result: str = "ok"  # ok | needs_attention | skipped_lock | error
    pulled_segments: int = 0
    applied: int = 0
    conflicts: int = 0
    errors: int = 0  # per-op apply errors (cycle still completes)
    pushed_ops: int = 0
    pushed_objects: int = 0
    guard: str | None = None
    reason: str | None = None
    warnings: list = dc_field(default_factory=list)
    started_at: str = dc_field(default_factory=_now_iso)
    finished_at: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def device_short(device_id: str) -> str:
    """Short human-facing device handle (last 6 ULID chars, lowercased)."""
    return device_id[-6:].lower()


def get_or_create_device(config: TiroConfig) -> tuple[str, str]:
    """Return (device_id, name) for THIS device, minting the identity once.

    The is_self=1 row in sync_state is the durable identity; first call
    mints a ULID device_id named after the host (platform.node()).
    """
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT device_id, name FROM sync_state WHERE is_self = 1"
        ).fetchone()
        if row is not None:
            return (row["device_id"], row["name"])
        device_id = new_ulid()
        name = platform.node() or "device"
        try:
            conn.execute(
                "INSERT INTO sync_state (device_id, name, is_self, last_seen) "
                "VALUES (?, ?, 1, ?)",
                (device_id, name, _now_iso()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Lost the mint race (idx_sync_state_self partial unique index):
            # another connection created the identity first — adopt it.
            row = conn.execute(
                "SELECT device_id, name FROM sync_state WHERE is_self = 1"
            ).fetchone()
            if row is None:  # pragma: no cover - only on non-race corruption
                raise
            return (row["device_id"], row["name"])
        logger.info("Minted sync device identity %s (%s)", device_id, name)
        return (device_id, name)
    finally:
        conn.close()


def read_sync_state(config: TiroConfig) -> dict:
    """Read the whole sync_state registry.

    Returns {"self": row-dict | None, "devices": [row-dicts],
    "watermarks": dict (parsed from self's watermarks_json, {} if absent),
    "last_cycle": parsed last_cycle_json or None}.
    """
    conn = get_connection(config.db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM sync_state ORDER BY is_self DESC, device_id"
        )]
    finally:
        conn.close()
    self_row = next((r for r in rows if r["is_self"] == 1), None)
    watermarks: dict = {}
    last_cycle = None
    if self_row is not None:
        # Defensive parse ("never crash the server on bad input"): a garbage
        # row degrades to empty state — an empty watermark just re-pulls,
        # and S2 apply is idempotent, so this is semantically safe.
        if self_row.get("watermarks_json"):
            parsed: object = None
            try:
                parsed = json.loads(self_row["watermarks_json"])
            except ValueError:
                pass
            # Type-validate, not just parse (S5.3 review m1): watermarks
            # feed integer seq comparisons in the pull loop — only
            # str-key/int-value entries survive; anything else degrades
            # (an empty/short watermark just re-pulls, apply is idempotent).
            if isinstance(parsed, dict):
                watermarks = {
                    k: v for k, v in parsed.items()
                    if isinstance(k, str)
                    and isinstance(v, int) and not isinstance(v, bool)
                }
            if not isinstance(parsed, dict) or len(watermarks) != len(parsed):
                logger.warning("sync_state: unreadable watermarks_json — "
                               "treating as empty (will re-pull)")
        if self_row.get("last_cycle_json"):
            try:
                last_cycle = json.loads(self_row["last_cycle_json"])
            except ValueError:
                logger.warning("sync_state: unreadable last_cycle_json — "
                               "treating as no prior cycle")
    return {
        "self": self_row,
        "devices": rows,
        "watermarks": watermarks,
        "last_cycle": last_cycle,
    }


def update_self_state(
    config: TiroConfig,
    *,
    last_seq: int | None = None,
    watermarks: dict | None = None,
    last_cycle: dict | None = None,
) -> None:
    """Update THIS device's sync_state row (always refreshing last_seen)."""
    sets = ["last_seen = ?"]
    params: list = [_now_iso()]
    if last_seq is not None:
        sets.append("last_seq = ?")
        params.append(last_seq)
    if watermarks is not None:
        sets.append("watermarks_json = ?")
        params.append(json.dumps(watermarks))
    if last_cycle is not None:
        sets.append("last_cycle_json = ?")
        params.append(json.dumps(last_cycle))
    conn = get_connection(config.db_path)
    try:
        cur = conn.execute(
            f"UPDATE sync_state SET {', '.join(sets)} WHERE is_self = 1",
            params,
        )
        conn.commit()
        if cur.rowcount == 0:
            logger.warning("update_self_state: no self device row — "
                           "call get_or_create_device first")
    finally:
        conn.close()


def upsert_remote_device(
    config: TiroConfig,
    device_id: str,
    *,
    name: str = "",
    last_seq: int = 0,
    last_seen: str | None = None,
    last_wall_ms: int | None = None,
) -> None:
    """Upsert a REMOTE device's registry row (is_self stays 0).

    A conflict against the SELF row is a guarded no-op (`WHERE is_self = 0`):
    the backend's devices/ listing normally includes our own device doc, and
    blindly upserting it would clobber the self row's last_seq (the journal
    head) with a possibly-stale remote-read value. An empty incoming name or
    last_seen never wipes a previously-known one (S5.3 review m3). last_seq
    stays doc-authoritative on purpose — a repair legitimately REGRESSES a
    remote device's journal head, so no monotone guard there.
    """
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            """
            INSERT INTO sync_state
                (device_id, name, is_self, last_seq, last_seen, last_wall_ms)
            VALUES (?, ?, 0, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                name = CASE WHEN excluded.name != '' THEN excluded.name
                            ELSE sync_state.name END,
                last_seq = excluded.last_seq,
                last_seen = CASE WHEN excluded.last_seen != ''
                                 THEN excluded.last_seen
                                 ELSE sync_state.last_seen END,
                last_wall_ms = COALESCE(excluded.last_wall_ms,
                                        sync_state.last_wall_ms)
            WHERE sync_state.is_self = 0
            """,
            (device_id, name, last_seq, last_seen, last_wall_ms),
        )
        conn.commit()
    finally:
        conn.close()


class SyncConfigError(Exception):
    """Sync is not (fully) configured, or configured inconsistently."""


def resolve_encryption(config: TiroConfig) -> bool:
    """Local encryption pin (spec §5): explicit on/off wins; auto = ON for
    network backends, OFF for filesystem. The cycle refuses to run when the
    backend's format.json disagrees with this pin (downgrade resistance —
    see open_format's docstring).

    Unknown sync_encrypt values REFUSE rather than fall through to auto:
    TIRO_SYNC_ENCRYPT=true (a natural bool intuition, passed verbatim by
    the env overlay) must never silently mean plaintext on a filesystem
    backend (S5.2 review Major #2)."""
    if config.sync_encrypt == "on":
        return True
    if config.sync_encrypt == "off":
        return False
    if config.sync_encrypt != "auto":
        raise SyncConfigError(
            f"sync_encrypt must be auto/on/off, got {config.sync_encrypt!r}")
    return config.sync_backend in ("s3", "webdav")


class AuditedAdapter:
    """Uniform audit wrapper around any StorageAdapter (composition).

    Emits exactly ONE service="sync" audit line per verb call
    (endpoint=<verb>, duration_ms, success; put logs bytes_out, get logs
    bytes_in, list logs count). WHY composition + config=None inners:
    S4's s3/webdav adapters have their own built-in audit when constructed
    with a config — with divergent 4xx semantics between the two — so
    adapter_for_config constructs them with config=None (their audit
    no-ops) and this wrapper is the single audit surface for every
    backend, filesystem included. That uniformity also moots S4's 4xx
    audit-semantics divergence (recorded in sync-s4-progress.md).

    log_api_call swallows its own failures, so audit can never raise into
    the sync cycle; adapter exceptions are logged (success=False) and
    re-raised unchanged.
    """

    def __init__(self, inner, config: TiroConfig):
        self.inner = inner
        self._config = config

    async def _call(self, verb: str, coro_fn, *args, **fields):
        from time import monotonic

        from tiro.audit import log_api_call

        started = monotonic()
        try:
            result = await coro_fn(*args)
        except Exception as e:
            log_api_call(
                self._config, "sync", endpoint=verb,
                duration_ms=int((monotonic() - started) * 1000),
                success=False, error=str(e)[:200],
            )
            raise
        if verb == "get":
            fields["bytes_in"] = len(result)
        elif verb == "list":
            fields["count"] = len(result)
        log_api_call(
            self._config, "sync", endpoint=verb,
            duration_ms=int((monotonic() - started) * 1000),
            success=True, **fields,
        )
        return result

    async def put(self, key: str, data: bytes) -> None:
        return await self._call("put", self.inner.put, key, data,
                                bytes_out=len(data))

    async def get(self, key: str) -> bytes:
        return await self._call("get", self.inner.get, key)

    async def list(self, prefix: str) -> list[str]:
        return await self._call("list", self.inner.list, prefix)

    async def delete(self, key: str) -> None:
        return await self._call("delete", self.inner.delete, key)

    async def lock(self, ttl_s: int) -> bool:
        return await self._call("lock", self.inner.lock, ttl_s)

    async def unlock(self) -> None:
        return await self._call("unlock", self.inner.unlock)

    async def aclose(self) -> None:
        await self.inner.aclose()


def adapter_for_config(config: TiroConfig) -> AuditedAdapter:
    """Build the configured storage adapter, audit-wrapped.

    Inner adapters are deliberately constructed WITHOUT a config (their
    built-in audit no-ops) — AuditedAdapter is the one audit surface; see
    its docstring.

    Validation runs BEFORE get_or_create_device so a failing call is
    side-effect-free — a status probe against a misconfigured library must
    never mint a device identity (S5.2 review Major #1). Heavy adapter
    imports (boto3/httpx) also happen only after validation passes.
    """

    def _field(value: str) -> str:
        return value.strip() if isinstance(value, str) else value

    backend = config.sync_backend
    resolve_encryption(config)  # refuse unknown sync_encrypt values early
    if backend == "filesystem":
        if not _field(config.sync_path):
            raise SyncConfigError("sync_path is not configured")
    elif backend == "s3":
        if not (_field(config.sync_s3_endpoint) and _field(config.sync_s3_bucket)
                and _field(config.sync_s3_access_key)
                and _field(config.sync_s3_secret_key)):
            raise SyncConfigError(
                "s3 backend requires sync_s3_endpoint, sync_s3_bucket, "
                "sync_s3_access_key and sync_s3_secret_key"
            )
    elif backend == "webdav":
        if not (_field(config.sync_webdav_url) and _field(config.sync_webdav_user)
                and _field(config.sync_webdav_password)):
            raise SyncConfigError(
                "webdav backend requires sync_webdav_url, sync_webdav_user "
                "and sync_webdav_password"
            )
    else:
        raise SyncConfigError(f"unknown sync_backend: {backend!r}")

    device_id, _name = get_or_create_device(config)
    if backend == "filesystem":
        from pathlib import Path

        from tiro.sync.adapters.filesystem import FilesystemAdapter

        inner = FilesystemAdapter(Path(config.sync_path), device_id=device_id)
    elif backend == "s3":
        from tiro.sync.adapters.s3 import S3Adapter

        inner = S3Adapter(
            endpoint_url=config.sync_s3_endpoint,
            bucket=config.sync_s3_bucket,
            access_key=config.sync_s3_access_key,
            secret_key=config.sync_s3_secret_key,
            device_id=device_id,
        )
    else:
        from tiro.sync.adapters.webdav import WebDAVAdapter

        inner = WebDAVAdapter(
            config.sync_webdav_url,
            username=config.sync_webdav_user,
            password=config.sync_webdav_password,
            device_id=device_id,
        )
    return AuditedAdapter(inner, config)


def codec_for_config(config: TiroConfig):
    """Resolve the local codec from the encryption pin: PlainCodec when the
    pin resolves off, else an AgeCodec over sync_identity (the recovery
    code). Cross-checking the pin against the backend's format.json is the
    cycle's job (downgrade resistance) — this only builds the local half."""
    from tiro.sync.crypto import AgeCodec, PlainCodec

    if not resolve_encryption(config):
        return PlainCodec()
    identity = config.sync_identity
    if not (identity.strip() if isinstance(identity, str) else identity):
        raise SyncConfigError("sync_identity is not set — run tiro sync setup")
    return AgeCodec(config.sync_identity)


async def _get_or_none(adapter, key: str) -> bytes | None:
    """adapter.get with KeyMissing folded to None — a missing key is a
    normal answer during pull; every other fault propagates."""
    from tiro.sync.adapters.base import KeyMissing

    try:
        return await adapter.get(key)
    except KeyMissing:
        return None


def update_remote_wall(config: TiroConfig, device_id: str,
                       last_wall_ms: int) -> None:
    """UPDATE-only wall-clock tracker for a REMOTE device's registry row.

    Deliberately not upsert_remote_device: its other params default to
    name=""/last_seq=0 and the ON CONFLICT SET would clobber the row the
    registry refresh just wrote. Monotone (MAX) and a no-op when the row
    does not exist yet."""
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            "UPDATE sync_state SET last_wall_ms = "
            "MAX(COALESCE(last_wall_ms, 0), ?) "
            "WHERE device_id = ? AND is_self = 0",
            (last_wall_ms, device_id),
        )
        conn.commit()
    finally:
        conn.close()


def _remap_alias_uids(ops: list, aliases: dict[str, str]) -> list:
    """D25(a): remap pulled ops that target a DEAD (aliased-away) article
    uid onto the surviving uid, so late ops for deduped losers can't
    resurrect them. Only ops that carry an ARTICLE uid are remapped: Meta,
    RowDel(table='articles'), and FilePut/FileDel whose path_hint is under
    articles/. Alias ops themselves and line/link ops pass through
    untouched. Chains are followed with a visited-set cycle guard capped at
    _ALIAS_CHAIN_CAP hops; a cycle or over-deep chain leaves the op as-is."""
    if not aliases:
        return list(ops)
    out: list = []
    for op in ops:
        carries_article_uid = (
            isinstance(op, Meta)
            or (isinstance(op, RowDel) and op.table == "articles")
            or (isinstance(op, (FilePut, FileDel))
                and op.path_hint.startswith("articles/"))
        )
        if carries_article_uid and op.uid in aliases:
            target = op.uid
            seen: set[str] = set()
            while (target in aliases and target not in seen
                   and len(seen) < _ALIAS_CHAIN_CAP):
                seen.add(target)
                target = aliases[target]
            # A cycle or a chain cut by the cap leaves target still in the
            # alias map (a dead uid) — never remap onto one of those.
            if target and target not in aliases and target != op.uid:
                op = dc_replace(op, uid=target)
        out.append(op)
    return out


async def _pull(config: TiroConfig, adapter, codec, clock, clock_state: dict,
                report: CycleReport, *,
                accept_mass_delete: bool = False) -> bool:
    """Pull + apply every unseen remote segment (spec §6.2). Returns False
    when the cycle must NOT proceed to push (gap, quarantine, vanished
    segment, mass-delete guard) — report.result/reason say why. Watermarks
    persist PER SEGMENT (D19#2: minimizes the crash-replay window; the
    conflict-file same-content dedupe covers the residual window)."""
    from tiro.sync.manifest import load_shadow
    from tiro.sync.merge import apply_ops
    from tiro.sync.snapshot import (
        QUARANTINE_ERRORS,
        SnapshotError,
        decode_segment,
        object_key,
        parse_device_doc,
        parse_journal_key,
        segment_object_refs,
    )

    device_id, _name = get_or_create_device(config)
    watermarks = dict(read_sync_state(config)["watermarks"])

    # 1. Device-registry refresh from devices/*.json (skip self). The
    # registry is ADVISORY (S5.3 review n4): a garbage doc OR a transient
    # adapter fault on any single doc is a warning, never a stopped cycle —
    # unlike the journal loop below, which keeps strict propagation.
    for key in await adapter.list("devices/"):
        tail = key[len("devices/"):]
        if not tail.endswith(".json") or "/" in tail:
            continue
        remote_id = tail[: -len(".json")]
        if remote_id == device_id:
            continue
        try:
            raw = await _get_or_none(adapter, key)
            if raw is None:
                continue
            info = parse_device_doc(remote_id, raw.decode("utf-8"))
        except Exception as e:
            report.warnings.append(f"unreadable device doc {key}: {e}")
            continue
        upsert_remote_device(config, remote_id, name=info.name,
                             last_seq=info.last_seq, last_seen=info.last_seen)

    # 2. Enumerate unseen foreign segments.
    pending: list[tuple[str, int, str]] = []
    for key in await adapter.list("journal/"):
        try:
            dev, seq = parse_journal_key(key)
        except SnapshotError as e:
            report.warnings.append(f"unrecognized journal key {key}: {e}")
            continue
        if dev == device_id:
            continue
        if seq <= watermarks.get(dev, 0):
            continue
        pending.append((dev, seq, key))
    pending.sort()

    # 3. Gap detection BEFORE applying anything: per device, the run of new
    # seqs must start at watermark+1 and be contiguous throughout.
    expected: dict[str, int] = {}
    for dev, seq, _key in pending:
        want = expected.get(dev, watermarks.get(dev, 0) + 1)
        if seq != want:
            report.result = "needs_attention"
            report.reason = (
                f"journal gap for device {dev}: expected segment {want}, "
                f"found {seq} — NOTHING from this run was applied; segments "
                "were GC'd past this device's ack; re-bootstrap or repair")
            logger.error("Sync pull: %s", report.reason)
            return False
        expected[dev] = seq + 1

    # One-shot mass-delete consent (S5.3 review M3): --accept-mass-delete
    # covers exactly ONE guard trip per run, never every segment of the
    # re-run — a second guarded segment must stop the pull again.
    acceptance_available = accept_mass_delete

    # 4. Fetch + decode + apply, in (device, seq) order.
    for dev, seq, key in pending:
        raw = await _get_or_none(adapter, key)
        if raw is None:
            report.result = "needs_attention"
            report.reason = f"segment vanished during pull: {key}"
            logger.error("Sync pull: %s", report.reason)
            return False
        try:
            refs = segment_object_refs(raw, codec)
            objects: dict[str, bytes] = {}
            for h in sorted(refs):
                # Content-address boundary (S5.3 review B1): only refs
                # shaped like sha256 hex are ever fetched — see
                # _OBJECT_HASH_RE. A malformed ref stays unfetched so
                # decode_segment quarantines instead of an AdapterError
                # escaping the pull.
                if not _OBJECT_HASH_RE.fullmatch(h):
                    continue
                blob = await _get_or_none(adapter, object_key(h))
                if blob is not None:
                    objects[h] = blob
                # absent blob stays absent: decode_segment raises the honest
                # "segment references missing object" JournalError below.
            ops = decode_segment(raw, codec, objects)
        except QUARANTINE_ERRORS as e:
            report.result = "needs_attention"
            report.reason = (
                f"quarantined segment {key} from device {dev}: {e}")
            logger.error("Sync pull: %s", report.reason)
            return False
        # Alias map reloaded PER SEGMENT (S5.3 review M1): aliases are
        # created DURING apply (a remote Alias op, or a URL-dedupe inside
        # _materialize_article), so a map loaded once per pull would drop a
        # later segment's ops on freshly-deduped uids (deferred_unknown +
        # watermark advance = permanent meta loss). One cheap read/segment.
        aliases = (await asyncio.to_thread(load_shadow, config)).aliases
        ops = _remap_alias_uids(ops, aliases)
        # Passing the engine's device-labeled clock stamps emitted Alias
        # ops with the real device id (D25(b)). The guard is ALWAYS armed
        # on the first apply; a sqlite3.OperationalError out of apply_ops
        # (transient infra, review M2) deliberately propagates — the cycle
        # wrapper classifies it as a retryable cycle error and the
        # watermark stays put for an idempotent whole-segment re-apply.
        apply_report = await asyncio.to_thread(
            apply_ops, config, ops, guard=True, clock=clock)
        if apply_report.guard:
            if acceptance_available:
                acceptance_available = False  # consumed by THIS guard trip
                logger.warning(
                    "Sync pull: mass-delete guard on segment %s accepted "
                    "(one-shot): %s", key, apply_report.guard)
                apply_report = await asyncio.to_thread(
                    apply_ops, config, ops, guard=False, clock=clock)
            else:
                report.result = "needs_attention"
                report.guard = apply_report.guard
                report.reason = "mass_delete_guard"
                logger.warning(
                    "Sync pull: mass-delete guard on segment %s: %s "
                    "— watermark NOT advanced", key, apply_report.guard)
                return False
        # Wall-clock tracker only AFTER the successful (post-guard) apply
        # (S5.3 review n1); `if mx:` deliberately skips wall_ms == 0
        # (epoch-pinned snapshot stamps carry no real wall time).
        mx = max((op.hlc.wall_ms for op in ops), default=None)
        if mx:
            update_remote_wall(config, dev, mx)
        report.pulled_segments += 1
        report.applied += apply_report.applied
        report.conflicts += apply_report.conflicts
        report.errors += apply_report.errors
        clock_state.setdefault("emitted_ops", []).extend(
            apply_report.emitted_ops)
        # Persist per segment (D19#2): a crash after apply replays at most
        # the current segment, and the conflict-file dedupe absorbs that.
        watermarks[dev] = seq
        update_self_state(config, watermarks=watermarks)
    return True


# --- The cycle itself (S5.4): in-process lock, format pin, push, compaction --

#: One cycle at a time per process. A threading.Lock, NOT asyncio.Lock,
#: deliberately (recorded decision D-S5-1(b)): an asyncio.Lock binds its
#: event loop on first use and breaks under repeated asyncio.run() from
#: CLI/tests; threading.Lock is loop-agnostic and the cycle never awaits
#: while acquiring (non-blocking acquire only).
_CYCLE_LOCK = threading.Lock()


def sync_cycle_running() -> bool:
    """True while a cycle runs in this process (routes use this for 409)."""
    return _CYCLE_LOCK.locked()


async def _open_backend(config: TiroConfig, adapter) -> tuple:
    """Per-cycle format.json check -> (fmt_or_None, codec).

    This discharges the S3 downgrade-resistance obligation (open_format's
    docstring): format.json is plaintext and unauthenticated, so the LOCAL
    encryption pin (resolve_encryption) is the authority — a backend doc
    that disagrees is refused BEFORE any codec is constructed, so no
    identity/passphrase is ever needed just to detect the mismatch.

    A missing format.json on a plaintext-pinned backend is auto-initialized
    (plaintext form only — an encrypted backend is only ever initialized by
    the explicit setup ceremony, Task 5's init_backend)."""
    from tiro.sync.crypto import (
        PlainCodec,
        SyncFormatError,
        build_format_json,
        parse_format_json,
    )
    from tiro.sync.snapshot import FORMAT_KEY

    raw = await _get_or_none(adapter, FORMAT_KEY)
    if raw is None:
        if resolve_encryption(config):
            raise SyncConfigError(
                "backend has no format.json — run `tiro sync setup`")
        text = build_format_json(new_ulid())
        await adapter.put(FORMAT_KEY, text.encode("utf-8"))
        logger.info("Sync: auto-initialized plaintext backend format.json")
        return parse_format_json(text), PlainCodec()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SyncFormatError(f"format.json is not valid UTF-8: {e}") from e
    fmt = parse_format_json(text)
    # ENCRYPTION PIN — checked before any codec construction (order matters:
    # detecting a tampered/downgraded format.json must not require a local
    # identity to exist).
    pin = "age" if resolve_encryption(config) else "none"
    if fmt.encryption != pin:
        raise SyncFormatError(
            f"encryption mode mismatch: backend format.json says "
            f"{fmt.encryption!r} but this device is pinned {pin!r} — "
            "possible tamper/downgrade or wrong local sync_encrypt; "
            "refusing to sync")
    codec = codec_for_config(config)
    if fmt.encryption == "age" and codec.recipient != fmt.age_recipient:
        raise SyncFormatError(
            "sync identity does not match this backend's recipient")
    return fmt, codec


async def _put_device_doc(config: TiroConfig, adapter, device_id: str,
                          name: str, *, last_seq: int | None = None) -> None:
    """Write THIS device's registry doc — ALWAYS plaintext (spec §5).

    Refreshed every cycle even when nothing was pushed: last_seen keeps the
    device out of plan_gc's 90-day dead-device drop, and acked (= our
    watermarks, remote ids only — self is excluded by construction, since
    watermarks only ever hold remote device ids) is what lets OTHER devices'
    GC delete segments we have applied."""
    import tiro
    from tiro.sync.snapshot import DeviceInfo, device_key, encode_device_doc

    state = read_sync_state(config)
    if last_seq is None:
        last_seq = (state["self"] or {}).get("last_seq") or 0
    info = DeviceInfo(
        device_id=device_id,
        name=name,
        last_seen=_now_iso(),
        last_seq=last_seq,
        app_version=tiro.__version__,
        acked=state["watermarks"],
    )
    await adapter.put(device_key(device_id),
                      encode_device_doc(info).encode("utf-8"))


async def _push(config: TiroConfig, adapter, codec, clock, clock_state: dict,
                report: CycleReport) -> None:
    """Derive + push local changes (spec §6.4). The upload order is FROZEN
    crash-safety ordering: objects FIRST -> journal segment -> device doc ->
    LOCAL shadow+state LAST. A crash anywhere leaves either unreferenced
    objects (harmless, GC'd) or an unacknowledged segment the next cycle's
    unchanged shadow re-derives — never a shadow that claims state the
    backend does not hold."""
    from tiro.sync.manifest import (
        build_manifest,
        diff,
        hydrate_bodies,
        load_shadow,
        save_shadow,
    )
    from tiro.sync.reconcile import reconcile_library
    from tiro.sync.snapshot import encode_segment, journal_key, object_key

    device_id, name = get_or_create_device(config)

    def _derive():
        reconcile_library(config)  # spec §6.3: the S1 pass runs first
        manifest = build_manifest(config)
        shadow = load_shadow(config)
        ops = hydrate_bodies(config, diff(manifest, shadow, clock=clock))
        return manifest, ops

    manifest, local_ops = await asyncio.to_thread(_derive)
    all_ops = list(local_ops) + list(clock_state.get("emitted_ops", []))
    if not all_ops:
        # Heartbeat: last_seen + acked STILL refresh (GC ack progression
        # depends on it), and the shadow is re-saved (cheap, idempotent).
        await _put_device_doc(config, adapter, device_id, name)
        await asyncio.to_thread(save_shadow, config, manifest, clock=clock)
        return
    seg_blob, obj_blobs = encode_segment(all_ops, codec)
    for h in sorted(obj_blobs):  # 1. objects FIRST
        await adapter.put(object_key(h), obj_blobs[h])
        report.pushed_objects += 1
    seq = read_sync_state(config)["self"]["last_seq"] + 1
    await adapter.put(journal_key(device_id, seq), seg_blob)  # 2. segment
    await _put_device_doc(config, adapter, device_id, name,  # 3. device doc
                          last_seq=seq)
    await asyncio.to_thread(  # 4. local shadow + state LAST
        save_shadow, config, manifest, clock=clock)
    update_self_state(config, last_seq=seq)
    report.pushed_ops += len(all_ops)


async def _maybe_compact(config: TiroConfig, adapter, codec,
                         report: CycleReport) -> None:
    """Best-effort snapshot + GC (spec §6.5): ANY failure defers the whole
    compaction to a later cycle with a warning — including build_snapshot's
    SnapshotError on an unreadable entry (the S3 obligation 'defer the cycle
    or exclude consciously': we DEFER). Never fails the cycle."""
    try:
        await _compact(config, adapter, codec, report)
    except Exception as e:
        logger.warning("Sync compaction skipped: %s", e)
        report.warnings.append(f"compaction skipped: {e}")


async def _compact(config: TiroConfig, adapter, codec,
                   report: CycleReport) -> None:
    from tiro.sync.manifest import Shadow, build_manifest, diff, hydrate_bodies
    from tiro.sync.snapshot import (
        build_snapshot,
        decode_snapshot,
        encode_object,
        encode_snapshot,
        object_key,
        parse_device_doc,
        parse_journal_key,
        plan_gc,
        plan_object_gc,
        segment_object_refs,
        should_snapshot,
        snapshot_key,
    )

    device_id, _name = get_or_create_device(config)

    # 1. Latest snapshot doc (ULID ids sort by creation time).
    snap_ids = sorted({key.split("/")[1]
                       for key in await adapter.list("snapshots/")
                       if key.count("/") >= 2})
    covers: dict[str, int] = {}
    created_at: str | None = None
    if snap_ids:
        latest = decode_snapshot(
            await adapter.get(snapshot_key(snap_ids[-1])), codec)
        covers = latest.covers
        created_at = latest.created_at

    # 2. Cadence — ops-since proxy: journal segments past the latest covers.
    segment_keys = await adapter.list("journal/")
    ops_since = 0
    for key in segment_keys:
        try:
            dev, seq = parse_journal_key(key)
        except Exception as e:
            report.warnings.append(f"unrecognized journal key {key}: {e}")
            continue
        if seq > covers.get(dev, -1):
            ops_since += 1
    if not should_snapshot(ops_since, created_at):
        return

    # 3. Build the snapshot doc from the CURRENT full state. object_hashes
    # comes from POST-hydration FilePuts (blob addresses, full-file space —
    # build_snapshot's docstring contract for article entries).
    def _derive():
        manifest = build_manifest(config)
        hydrated = hydrate_bodies(config, diff(manifest, Shadow()))
        return manifest, hydrated

    manifest, hydrated = await asyncio.to_thread(_derive)
    file_puts = [op for op in hydrated if isinstance(op, FilePut)]
    object_hashes = {op.path_hint: op.object_hash for op in file_puts}
    bodies_by_address = {op.object_hash: op.body for op in file_puts}
    state = read_sync_state(config)
    covers_new = {device_id: (state["self"] or {}).get("last_seq") or 0,
                  **state["watermarks"]}
    snapshot_id = new_ulid()
    doc_text, addresses = build_snapshot(
        manifest, snapshot_id=snapshot_id, created_by=device_id,
        covers=covers_new, object_hashes=object_hashes)

    # 4. Upload: objects FIRST, then the snapshot doc (same crash-safety
    # ordering as segments).
    for address in sorted(addresses):
        body = bodies_by_address.get(address)
        if body is None:
            report.warnings.append(
                "compaction skipped: no body for snapshot object "
                f"{address!r}")
            return
        h, blob = encode_object(body, codec)
        if h != address:  # pragma: no cover - encode_object is deterministic
            report.warnings.append(
                f"compaction skipped: object hash drift for {address!r}")
            return
        await adapter.put(object_key(h), blob)
    await adapter.put(snapshot_key(snapshot_id),
                      encode_snapshot(doc_text, codec))

    # 5. Journal/snapshot GC — plan_gc is pure, the engine executes it and
    # SURFACES its warnings to status (S3 obligation #6).
    devices: dict = {}
    for key in await adapter.list("devices/"):
        tail = key[len("devices/"):]
        if not tail.endswith(".json") or "/" in tail:
            continue
        dev_id = tail[: -len(".json")]
        try:
            raw = await adapter.get(key)
            devices[dev_id] = parse_device_doc(dev_id, raw.decode("utf-8"))
        except Exception as e:
            report.warnings.append(f"unreadable device doc {key}: {e}")
    segment_keys = await adapter.list("journal/")
    snap_docs = {snapshot_id: None}  # id -> SnapshotDoc (new one included)
    for sid in snap_ids:
        snap_docs[sid] = None
    for sid in list(snap_docs):
        snap_docs[sid] = decode_snapshot(
            await adapter.get(snapshot_key(sid)), codec)
    plan = plan_gc(
        devices=devices,
        segment_keys=segment_keys,
        snapshot_covers={sid: d.covers for sid, d in snap_docs.items()},
    )
    for key in plan.delete_segments:
        await adapter.delete(key)
    for key in plan.delete_snapshots:
        await adapter.delete(key)
    report.warnings.extend(plan.warnings)
    if plan.dropped_devices:
        logger.info("Sync GC: dead devices no longer block journal GC: %s",
                    ", ".join(plan.dropped_devices))

    # 6. Object GC in ADDRESS space (S3 obligation #4): live = every
    # REMAINING snapshot doc's addresses ∪ every REMAINING segment's refs
    # (via the shared NEL-safe pre-scan). A fetch/decode failure aborts
    # object GC — never delete on partial knowledge.
    deleted_snapshots = set(plan.delete_snapshots)
    live: set[str] = set()
    for sid, doc in snap_docs.items():
        if snapshot_key(sid) in deleted_snapshots:
            continue
        live.update(doc.objects.values())
    deleted_segments = set(plan.delete_segments)
    for key in segment_keys:
        if key in deleted_segments:
            continue
        try:
            blob = await adapter.get(key)
            live.update(segment_object_refs(blob, codec))
        except Exception as e:
            report.warnings.append(
                f"object GC skipped: cannot read segment {key}: {e}")
            return
    for key in plan_object_gc(live, await adapter.list("objects/")):
        await adapter.delete(key)


def _record_cycle(config: TiroConfig, report: CycleReport) -> None:
    """Persist the report as last_cycle_json — best-effort, never raises."""
    try:
        get_or_create_device(config)
        update_self_state(config, last_cycle=report.as_dict())
    except Exception as e:
        logger.warning("Could not record sync cycle outcome: %s", e)


async def sync_cycle(config: TiroConfig, adapter=None, *,
                     accept_mass_delete: bool = False) -> CycleReport:
    """One full sync cycle (spec §6): open backend -> pull -> push ->
    compact. NEVER raises — every outcome is a CycleReport, persisted as
    last_cycle (except the in-process-lock skip, which does not own the
    state and returns unrecorded)."""
    from tiro.sync.snapshot import QUARANTINE_ERRORS

    report = CycleReport()
    if not _CYCLE_LOCK.acquire(blocking=False):
        report.result = "skipped_lock"
        report.reason = "another cycle is running in this process"
        report.finished_at = _now_iso()
        return report  # not recorded — we don't own the state
    try:
        if adapter is None:
            adapter = adapter_for_config(config)
        _fmt, codec = await _open_backend(config, adapter)
        got_lock: bool | None = False
        try:
            got_lock = await adapter.lock(LOCK_TTL_S)
        except Exception as e:
            logger.warning(
                "Sync lock unavailable (%s) — proceeding lockless "
                "(safe: per-device journals, spec §6.1)", e)
            got_lock = None
        if got_lock is False:
            report.result = "skipped_lock"
            report.reason = "backend lock held by another device"
        else:
            try:
                device_id, _ = get_or_create_device(config)
                clock = HLCClock(device_id)
                clock_state: dict = {}
                # S5.5 adds: repair-epoch detection + empty-library
                # auto-bootstrap here (D-S5-3)
                ok = await _pull(config, adapter, codec, clock, clock_state,
                                 report,
                                 accept_mass_delete=accept_mass_delete)
                if ok:
                    await _push(config, adapter, codec, clock, clock_state,
                                report)
                    await _maybe_compact(config, adapter, codec, report)
            finally:
                if got_lock:
                    try:
                        await adapter.unlock()
                    except Exception as e:
                        logger.warning("Sync unlock failed: %s", e)
    except SyncConfigError as e:
        report.result = "error"
        report.reason = f"not configured: {e}"
    except QUARANTINE_ERRORS as e:
        report.result = "needs_attention"
        report.reason = str(e)[:300]
        logger.error("Sync quarantine: %s", e)
    except Exception as e:
        # _pull may legitimately raise sqlite3.OperationalError (M2
        # semantics, Task 3-fix): this branch correctly classifies it as a
        # retryable "error" — the watermark was deliberately NOT advanced,
        # so the next cycle re-applies the whole segment idempotently.
        logger.error("Sync cycle failed: %s", e, exc_info=True)
        report.result = "error"
        report.reason = str(e)[:300]
    finally:
        _CYCLE_LOCK.release()
    report.finished_at = _now_iso()
    _record_cycle(config, report)
    return report
