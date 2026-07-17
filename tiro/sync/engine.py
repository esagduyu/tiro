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


def load_sync_status(config: TiroConfig) -> dict:
    """One status dict for every sync-status surface (GET /api/settings/sync,
    the sidebar dot, S6 doctor). Pure read, NEVER raises: a missing/unreadable
    DB degrades to empty defaults — no table minting, no device-identity
    side effects (unlike adapter_for_config).

    dot semantics: "err" when the last cycle errored; "warn" when it needs
    attention OR sync is enabled but has never completed a cycle; else "ok"
    (skipped_lock counts as ok — another device legitimately held the lock).
    """
    configured = bool(config.sync_path or config.sync_s3_bucket
                      or config.sync_webdav_url)
    last_cycle = None
    device_name = None
    devices: list[dict] = []
    try:
        if config.db_path.exists():
            state = read_sync_state(config)
            last_cycle = state["last_cycle"]
            self_row = state["self"]
            if self_row is not None:
                device_name = self_row.get("name") or None
            devices = [
                {
                    "device_id": r["device_id"],
                    "name": r["name"],
                    "is_self": bool(r["is_self"]),
                    "last_seq": r["last_seq"] or 0,
                    "last_seen": r["last_seen"],
                }
                for r in state["devices"]
            ]
    except Exception as e:  # read_sync_state already degrades; belt on top
        logger.warning("load_sync_status: unreadable sync state: %s", e)
    result = (last_cycle or {}).get("result")
    if result == "error":
        dot = "err"
    elif result == "needs_attention" or (config.sync_enabled
                                         and last_cycle is None):
        dot = "warn"
    else:
        dot = "ok"
    return {
        "configured": configured,
        "enabled": config.sync_enabled,
        "dot": dot,
        "last_cycle": last_cycle,
        "last_synced_at": (last_cycle or {}).get("finished_at"),
        "device_name": device_name,
        "devices": devices,
    }


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
    from tiro.sync.manifest import clear_shadow, load_shadow
    from tiro.sync.merge import apply_ops
    from tiro.sync.snapshot import (
        FORMAT_KEY,
        QUARANTINE_ERRORS,
        SnapshotError,
        decode_segment,
        device_key,
        object_key,
        parse_device_doc,
        parse_journal_key,
        segment_object_refs,
    )

    device_id, _name = get_or_create_device(config)
    state = read_sync_state(config)
    watermarks = dict(state["watermarks"])

    # 0. Repair-epoch detection (spec §6.6 / S5 plan decision #5): we have
    # pushed before (last_seq > 0) yet our own device doc is GONE while
    # format.json still exists — a repair wiped the backend elsewhere.
    # Reset the SYNC BOOKKEEPING only — shadow entries/tombstones
    # (clear_shadow preserves alias + metats rows), last_seq, watermarks —
    # so this cycle re-diffs and re-pushes the full local state. LOCAL DATA
    # UNTOUCHED. last_seq=0 is safe against seq collision: the repair wiped
    # journal/, and _push's backend-max allocation covers any stragglers.
    if ((state["self"] or {}).get("last_seq") or 0) > 0:
        own_doc = await _get_or_none(adapter, device_key(device_id))
        if own_doc is None and (
                await _get_or_none(adapter, FORMAT_KEY)) is not None:
            logger.warning(
                "Sync: repair epoch detected (own device doc gone, "
                "format.json present) — resetting shadow/last_seq/"
                "watermarks for a full re-diff/re-push; local data "
                "untouched")
            await asyncio.to_thread(clear_shadow, config)
            update_self_state(config, last_seq=0, watermarks={})
            watermarks = {}
            report.warnings.append(
                "repair epoch detected — full re-diff/re-push")

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
        # m1: auto-init ONLY an EMPTY backend. A backend that already holds
        # journal/snapshot data but no format.json is a tamper or partial
        # copy — in particular, a DELETED age-mode format.json must never
        # cause a plaintext re-init followed by a cleartext push
        # (confidentiality). Refuse -> needs_attention via the existing
        # quarantine taxonomy (SyncFormatError).
        if (await adapter.list("journal/")) or (
                await adapter.list("snapshots/")):
            raise SyncFormatError(
                "backend has sync data but no format.json — refusing to "
                "initialize (possible tamper or partial copy)")
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
    LOCAL state (last_seq then shadow) LAST. A crash anywhere leaves either
    unreferenced objects (harmless, GC'd) or an unacknowledged segment the
    next cycle's unchanged shadow re-derives — never a shadow that claims
    state the backend does not hold."""
    from tiro.sync.manifest import (
        build_manifest,
        diff,
        hydrate_bodies,
        load_shadow,
        save_shadow,
    )
    from tiro.sync.reconcile import reconcile_library
    from tiro.sync.snapshot import (
        SnapshotError,
        encode_segment,
        journal_key,
        object_key,
        parse_journal_key,
    )

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
    # Backend-aware seq allocation (review B1): a crash after the segment
    # upload but before ANY local record leaves last_seq lagging the backend;
    # a purely local counter would then re-mint the SAME seq for DIFFERENT
    # bytes, and the unconditional adapter put would overwrite the
    # append-only journal (any remote that already pulled the first version
    # at that watermark diverges silently forever). Listing our own journal
    # prefix and allocating past max(local, backend) converts that
    # crash-window seq reuse into idempotent duplication at a fresh seq.
    backend_max = 0
    for key in await adapter.list(f"journal/{device_id}/"):
        try:
            _dev, key_seq = parse_journal_key(key)
        except SnapshotError as e:
            logger.warning(
                "Sync push: skipping unparseable own journal key %s: %s",
                key, e)
            continue
        backend_max = max(backend_max, key_seq)
    local_last_seq = read_sync_state(config)["self"]["last_seq"]
    seq = max(local_last_seq, backend_max) + 1
    await adapter.put(journal_key(device_id, seq), seg_blob)  # 2. segment
    await _put_device_doc(config, adapter, device_id, name,  # 3. device doc
                          last_seq=seq)
    # 4. local state LAST — seq counter FIRST, shadow SECOND (review M1):
    # a crash between the two re-derives the same diff and re-pushes it as
    # seq+1 — duplicates are idempotently absorbed by S2 apply; losses are
    # not (append-only invariant, spec §6.4). The reverse order (shadow
    # first) would leave the shadow claiming pushed state while last_seq
    # lagged, and the next content change would reuse the SAME seq with
    # different bytes — an overwrite a remote puller can never detect.
    update_self_state(config, last_seq=seq)
    await asyncio.to_thread(save_shadow, config, manifest, clock=clock)
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


async def _derive_local_snapshot(config: TiroConfig, device_id: str,
                                 covers: dict):
    """Derive a snapshot of the CURRENT full local state WITHOUT touching
    the backend: build_manifest + hydrate + build_snapshot + the
    body-availability check — everything that can raise SnapshotError on a
    hashless/unreadable local entry happens HERE, so repair() can derive
    BEFORE it destroys (S5.5 review M2). Returns the upload plan
    (snapshot_id, doc_text, addresses, bodies_by_address, manifest).

    object_hashes comes from POST-hydration FilePuts (blob addresses,
    full-file space — build_snapshot's docstring contract for articles)."""
    from tiro.sync.manifest import Shadow, build_manifest, diff, hydrate_bodies
    from tiro.sync.snapshot import SnapshotError, build_snapshot

    def _derive():
        manifest = build_manifest(config)
        hydrated = hydrate_bodies(config, diff(manifest, Shadow()))
        return manifest, hydrated

    manifest, hydrated = await asyncio.to_thread(_derive)
    file_puts = [op for op in hydrated if isinstance(op, FilePut)]
    object_hashes = {op.path_hint: op.object_hash for op in file_puts}
    bodies_by_address = {op.object_hash: op.body for op in file_puts}
    snapshot_id = new_ulid()
    doc_text, addresses = build_snapshot(
        manifest, snapshot_id=snapshot_id, created_by=device_id,
        covers=covers, object_hashes=object_hashes)
    for address in sorted(addresses):
        if bodies_by_address.get(address) is None:
            raise SnapshotError(
                f"no body for snapshot object {address!r}")
    return snapshot_id, doc_text, addresses, bodies_by_address, manifest


async def _upload_snapshot(adapter, codec, snapshot_id: str, doc_text: str,
                           addresses, bodies_by_address: dict) -> int:
    """Upload a derived snapshot plan — objects FIRST, then the doc (the
    frozen crash-safety ordering). Returns the objects-uploaded count."""
    from tiro.sync.snapshot import (
        SnapshotError,
        encode_object,
        encode_snapshot,
        object_key,
        snapshot_key,
    )

    uploaded = 0
    for address in sorted(addresses):
        h, blob = encode_object(bodies_by_address[address], codec)
        if h != address:  # pragma: no cover - encode_object is deterministic
            raise SnapshotError(f"object hash drift for {address!r}")
        await adapter.put(object_key(h), blob)
        uploaded += 1
    await adapter.put(snapshot_key(snapshot_id),
                      encode_snapshot(doc_text, codec))
    return uploaded


async def _upload_local_snapshot(config: TiroConfig, adapter, codec,
                                 device_id: str, covers: dict):
    """Build a snapshot of the CURRENT full local state and upload it —
    derive then upload (see the two halves above). Returns
    (snapshot_id, objects_uploaded, manifest). Raises SnapshotError when a
    snapshot object's body is unavailable or drifts: _compact defers via
    _maybe_compact's catch-all (warning message shape preserved). Extracted
    from _compact in S5.5, split in S5.5-fix (M2) — behavior-identical for
    compaction; repair() calls the halves directly so derivation failures
    abort BEFORE the wipe."""
    snapshot_id, doc_text, addresses, bodies_by_address, manifest = (
        await _derive_local_snapshot(config, device_id, covers))
    uploaded = await _upload_snapshot(adapter, codec, snapshot_id, doc_text,
                                      addresses, bodies_by_address)
    return snapshot_id, uploaded, manifest


async def _compact(config: TiroConfig, adapter, codec,
                   report: CycleReport) -> None:
    from tiro.sync.snapshot import (
        decode_snapshot,
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

    # 3+4. Build + upload the snapshot from the CURRENT full state
    # (_upload_local_snapshot — objects first, then the doc; its
    # SnapshotError on an unreadable/drifting body defers the whole
    # compaction via _maybe_compact's catch).
    # m4: the snapshot's manifest may be NEWER than its covers — edits made
    # between this cycle's push and this compaction ride in the snapshot
    # while covers only records pushed seqs. Benign and understatement-safe:
    # the follow-up segment re-carries those edits, and the same-content
    # conflict dedupe absorbs the overlap on devices that bootstrapped from
    # the snapshot.
    state = read_sync_state(config)
    covers_new = {device_id: (state["self"] or {}).get("last_seq") or 0,
                  **state["watermarks"]}
    snapshot_id, _uploaded, _manifest = await _upload_local_snapshot(
        config, adapter, codec, device_id, covers_new)

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
    engine_built_adapter = adapter is None
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
                # Empty-library auto-bootstrap (D-S5-3): a fresh device
                # pointed at a populated backend materializes the latest
                # snapshot BEFORE the pull — the covers-derived watermarks
                # then prevent the journal-gap refusal on GC'd history, and
                # the normal pull folds the journal tails in. Restricted to
                # the ZERO-article, NEVER-SYNCED case deliberately: empty
                # watermarks AND last_seq == 0 (never pushed — S5.5 review
                # minor #2: a single-device library whose user deleted
                # every article is a legitimate zero-article steady state,
                # and re-materializing the snapshot each cycle would
                # re-download — and fight — the user's own deletes).
                # Folding a snapshot into a non-empty never-synced library
                # would mint a conflict file per differing article — that
                # merge-two-libraries flow stays behind the explicit setup
                # ceremony (bootstrap(), D-S5-3).
                state = read_sync_state(config)
                if (_count_articles(config) == 0
                        and not state["watermarks"]
                        and ((state["self"] or {}).get("last_seq") or 0) == 0
                        and await adapter.list("snapshots/")):
                    logger.info(
                        "Sync: empty library with no sync history and a "
                        "populated backend — auto-bootstrapping from the "
                        "latest snapshot (D-S5-3)")
                    await _materialize_latest_snapshot(
                        config, adapter, codec, report)
                ok = await _pull(config, adapter, codec, clock, clock_state,
                                 report,
                                 accept_mass_delete=accept_mass_delete)
                if ok:
                    await _push(config, adapter, codec, clock, clock_state,
                                report)
                    # M2: NEVER compact/GC in lockless mode. Per-device
                    # journals make push/pull lock-free-safe, but object GC
                    # computes liveness from a LIST snapshot — a concurrent
                    # pusher's objects-before-segment window would be
                    # misread as garbage and deleted. Compaction is
                    # best-effort anyway; skipping it under lock uncertainty
                    # (got_lock is None) costs nothing.
                    if got_lock is True:
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
        try:
            # m5: finalize + record BEFORE releasing the in-process lock —
            # two OS threads driving cycles via separate asyncio.run() calls
            # must never interleave their last_cycle_json writes (the lock
            # is a threading.Lock precisely because of that pattern). The
            # in-process-lock skip branch above stays unrecorded — it does
            # not own the state.
            report.finished_at = _now_iso()
            _record_cycle(config, report)
            # M3: close the adapter ONLY when this cycle constructed it —
            # a caller-provided adapter's lifecycle belongs to the caller.
            # Best-effort: a failing close never changes the report.
            if engine_built_adapter and adapter is not None:
                try:
                    await adapter.aclose()
                except Exception as e:
                    logger.warning("Sync adapter close failed: %s", e)
        finally:
            _CYCLE_LOCK.release()
    return report


# --- Setup flows (S5.5): init/verify, bootstrap, repair ----------------------


def _count_articles(config: TiroConfig) -> int:
    conn = get_connection(config.db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    finally:
        conn.close()


async def init_backend(config: TiroConfig, adapter, passphrase: str, *,
                       kdf_params: dict | None = None) -> str:
    """First-device backend initialization (setup ceremony, spec §9).

    Writes format.json and returns the recovery code ("" for a plaintext
    backend). The CALLER (setup CLI/route) persists sync_identity — this
    function never writes config. `kdf_params` is a TEST SEAM (weak Argon2id
    params for test speed; a fresh random salt is minted either way) —
    production callers never pass it."""
    import base64
    import os

    from tiro.sync.crypto import (
        AgeCodec,
        KdfParams,
        build_format_json,
        derive_recovery_code,
        new_kdf_params,
    )
    from tiro.sync.snapshot import FORMAT_KEY

    if await _get_or_none(adapter, FORMAT_KEY) is not None:
        raise SyncConfigError(
            "backend already initialized — join with the passphrase "
            "instead, or run repair")
    # Mirrors _open_backend's m1 guard (S5.5 review minor #4): journal or
    # snapshot data without a format.json is a tamper or partial copy —
    # initializing over it (possibly with a DIFFERENT recipient) would
    # orphan every existing blob. Refuse; repair is the explicit ceremony.
    if (await adapter.list("journal/")) or (
            await adapter.list("snapshots/")):
        raise SyncConfigError(
            "backend has sync data but no format.json — refusing to "
            "initialize (possible tamper or partial copy)")
    if not resolve_encryption(config):
        await adapter.put(FORMAT_KEY,
                          build_format_json(new_ulid()).encode("utf-8"))
        return ""
    if kdf_params is None:
        kdf = new_kdf_params()
    else:
        kdf = KdfParams(
            salt_b64=base64.b64encode(os.urandom(16)).decode("ascii"),
            **kdf_params)
    recovery = derive_recovery_code(passphrase, kdf)
    recipient = AgeCodec(recovery).recipient
    text = build_format_json(new_ulid(), kdf=kdf, age_recipient=recipient)
    await adapter.put(FORMAT_KEY, text.encode("utf-8"))
    return recovery


async def verify_passphrase(config: TiroConfig, adapter,
                            passphrase: str) -> str | None:
    """Joining-device passphrase check -> the recovery code (the caller
    persists it as sync_identity) on success, "" when the backend is
    plaintext (no identity needed), or None on a wrong passphrase or an
    uninitialized backend — callers distinguish None (refusal) from ""
    (no identity needed). Clean refusal (spec §9 scenario 6): no exception
    on a wrong passphrase, no partial state. SyncFormatError (e.g. a NEWER
    sync_format) deliberately PROPAGATES — version refusal must be LOUD,
    never disguised as "wrong passphrase"."""
    from tiro.sync.crypto import (
        AgeCodec,
        CryptoError,
        SyncFormatError,
        derive_recovery_code,
        parse_format_json,
    )
    from tiro.sync.snapshot import FORMAT_KEY

    raw = await _get_or_none(adapter, FORMAT_KEY)
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        # Corruption, not a wrong passphrase — loud, like the version check.
        raise SyncFormatError(f"format.json is not valid UTF-8: {e}") from e
    fmt = parse_format_json(text)  # SyncFormatError propagates (loud)
    if fmt.encryption == "none":
        return ""
    try:
        rc = derive_recovery_code(passphrase, fmt.kdf)
    except CryptoError as e:
        # Unusable KDF params (garbage salt b64, argon2 failure) are
        # CORRUPTION, not a wrong passphrase — loud, like the version
        # check (S5.5 review minor #3). A wrong passphrase never raises
        # here: derivation succeeds and the recipient simply mismatches
        # (the None below stays the refusal path).
        raise SyncFormatError(
            f"format.json kdf params unusable: {e}") from e
    if AgeCodec(rc).recipient == fmt.age_recipient:
        return rc
    return None


async def _materialize_latest_snapshot(config: TiroConfig, adapter, codec,
                                       report: CycleReport) -> bool:
    """Materialize the backend's newest snapshot into the LOCAL library —
    shared by bootstrap() and sync_cycle's empty-library auto-bootstrap
    (D-S5-3). Returns False when the backend holds no snapshot.

    Ops are stamped with materialize_ops' DEFAULT epoch-pinned clock
    (HLC(0,n,'snapshot')) — NEVER a wall-time clock, which would outrank
    and silently skip-as-stale every journal-tail op written before the
    bootstrap moment (snapshot.py's docstring — the S3 obligation).
    Watermarks come from the snapshot's covers with SELF filtered out: a
    re-bootstrapping device must not watermark its own journal.
    QUARANTINE_ERRORS propagate to the caller's taxonomy. Vector work:
    none — materialized articles ride vector_status='pending' and the
    existing vector-retry task."""
    from tiro.sync.adapters.base import KeyMissing
    from tiro.sync.merge import apply_ops
    from tiro.sync.snapshot import (
        SnapshotError,
        decode_object,
        decode_snapshot,
        materialize_ops,
        object_key,
        snapshot_key,
    )

    snap_ids = sorted({key.split("/")[1]
                       for key in await adapter.list("snapshots/")
                       if key.count("/") >= 2})
    if not snap_ids:
        return False
    newest = snap_ids[-1]  # ULIDs sort by creation time
    doc = decode_snapshot(await adapter.get(snapshot_key(newest)), codec)
    objects_plain: dict[str, str] = {}
    for addr in sorted(set(doc.objects.values())):
        try:
            blob = await adapter.get(object_key(addr))
        except KeyMissing as e:
            # Honest quarantine (S5.5 review minor #1): a snapshot whose
            # object is GONE is backend corruption/partial copy, and it is
            # detected HERE, before any op is applied — bootstrap stays
            # all-or-nothing. Raising SnapshotError routes it through the
            # QUARANTINE_ERRORS taxonomy (needs_attention), never a
            # bare-KeyMissing "error".
            raise SnapshotError(
                f"snapshot references missing object {addr!r}") from e
        objects_plain[addr] = decode_object(blob, codec, expected_hash=addr)
    ops = materialize_ops(doc, objects_plain)  # DEFAULT epoch-pinned clock
    device_id, _name = get_or_create_device(config)
    apply_report = await asyncio.to_thread(
        apply_ops, config, ops, guard=False, clock=HLCClock(device_id))
    report.applied += apply_report.applied
    report.conflicts += apply_report.conflicts
    report.errors += apply_report.errors
    watermarks = {d: s for d, s in doc.covers.items() if d != device_id}
    update_self_state(config, watermarks=watermarks)
    logger.info(
        "Sync bootstrap: materialized snapshot %s (%d applied, "
        "%d conflicts, %d errors)", newest, apply_report.applied,
        apply_report.conflicts, apply_report.errors)
    return True


async def bootstrap(config: TiroConfig, adapter) -> CycleReport:
    """Explicit joining-device bootstrap (setup/CLI verb, spec §9):
    materialize the backend's latest snapshot into an EMPTY library, fold in
    the journal tails, then push. Refuses a NON-empty library (recorded as
    an error report, no exception) — folding a snapshot into existing data
    would mint a conflict file per differing article; that merge-two-
    libraries flow stays behind the explicit setup ceremony (D-S5-3).
    Takes BOTH locks (S5.5-fix M1) — the in-process cycle lock and the
    backend advisory lock, with repair's conservative posture (a lock
    fault is an error, NEVER lockless): bootstrap's own push plus a
    concurrent pusher could otherwise mint the SAME seq for DIFFERENT
    bytes, the append-only violation the S5.4-fix backend-aware seq
    allocation exists to prevent.
    The CALLER owns the adapter's lifecycle (no aclose here)."""
    from tiro.sync.snapshot import QUARANTINE_ERRORS

    report = CycleReport()
    try:
        n = _count_articles(config)
        if n > 0:
            report.result = "error"
            report.reason = ("bootstrap requires an empty library "
                             f"(this library has {n} articles)")
        elif not _CYCLE_LOCK.acquire(blocking=False):
            report.result = "skipped_lock"
            report.reason = "another cycle is running in this process"
        else:
            try:
                _fmt, codec = await _open_backend(config, adapter)
                # A lock fault -> error via the generic taxonomy below.
                got = await adapter.lock(LOCK_TTL_S)
                if not got:
                    report.result = "skipped_lock"
                    report.reason = "backend lock held — try again"
                else:
                    try:
                        await _materialize_latest_snapshot(
                            config, adapter, codec, report)
                        device_id, _name = get_or_create_device(config)
                        clock = HLCClock(device_id)
                        clock_state: dict = {}
                        # Bootstrap trusts the backend's state wholesale:
                        # the journal tail may legitimately mass-delete
                        # relative to the snapshot.
                        ok = await _pull(config, adapter, codec, clock,
                                         clock_state, report,
                                         accept_mass_delete=True)
                        if ok:
                            await _push(config, adapter, codec, clock,
                                        clock_state, report)
                    finally:
                        try:
                            await adapter.unlock()
                        except Exception as e:
                            logger.warning(
                                "Sync bootstrap unlock failed: %s", e)
            finally:
                _CYCLE_LOCK.release()
    except SyncConfigError as e:
        report.result = "error"
        report.reason = f"not configured: {e}"
    except QUARANTINE_ERRORS as e:
        report.result = "needs_attention"
        report.reason = str(e)[:300]
        logger.error("Sync bootstrap quarantine: %s", e)
    except Exception as e:
        logger.error("Sync bootstrap failed: %s", e, exc_info=True)
        report.result = "error"
        report.reason = str(e)[:300]
    finally:
        report.finished_at = _now_iso()
        _record_cycle(config, report)
    return report


async def repair(config: TiroConfig, adapter) -> CycleReport:
    """Wipe the backend's sync state and re-seed it from THIS device's
    local library (spec §6.6). The typed-confirm ceremony lives in the
    callers (CLI/route) — this is the engine act.

    format.json is KEPT — never rewritten, byte-identical — so every other
    device keeps decrypting; they detect the repair epoch on their next
    pull (own device doc gone + format.json present) and re-push. Repair
    is conservative: it NEVER proceeds without the backend lock (held ->
    skipped_lock; a lock fault -> error via the generic taxonomy), and it
    DERIVES BEFORE IT DESTROYS (S5.5-fix M2): the full local snapshot plan
    is built first, so a hashless/unreadable local entry aborts with the
    backend fully intact."""
    from tiro.sync.manifest import save_shadow
    from tiro.sync.reconcile import reconcile_library
    from tiro.sync.snapshot import QUARANTINE_ERRORS

    report = CycleReport()
    try:
        _fmt, codec = await _open_backend(config, adapter)
        got = await adapter.lock(LOCK_TTL_S)  # a fault -> error, never lockless
        if not got:
            report.result = "skipped_lock"
            report.reason = "backend lock held by another device"
        else:
            try:
                device_id, name = get_or_create_device(config)
                # Derivation FIRST (M2), reconcile before it (spec §6.3
                # parity with _push) so the epoch seed captures external
                # edits. covers={} deliberately: the journal will be empty
                # (nothing subsumed), and a bootstrapping device starts at
                # watermarks {} and pulls future segments from seq 1.
                await asyncio.to_thread(reconcile_library, config)
                (snapshot_id, doc_text, addresses, bodies_by_address,
                 manifest) = await _derive_local_snapshot(
                    config, device_id, {})
                # Only NOW is the backend touched.
                for prefix in ("journal/", "objects/", "snapshots/",
                               "devices/"):
                    for key in await adapter.list(prefix):
                        await adapter.delete(key)
                uploaded = await _upload_snapshot(
                    adapter, codec, snapshot_id, doc_text, addresses,
                    bodies_by_address)
                report.pushed_objects += uploaded
                update_self_state(config, last_seq=0, watermarks={})
                await _put_device_doc(config, adapter, device_id, name,
                                      last_seq=0)
                # Shadow = the exact manifest the snapshot was built from:
                # nothing is pending to push after a repair.
                await asyncio.to_thread(save_shadow, config, manifest)
            finally:
                try:
                    await adapter.unlock()
                except Exception as e:
                    logger.warning("Sync repair unlock failed: %s", e)
    except SyncConfigError as e:
        report.result = "error"
        report.reason = f"not configured: {e}"
    except QUARANTINE_ERRORS as e:
        report.result = "needs_attention"
        report.reason = str(e)[:300]
        logger.error("Sync repair quarantine: %s", e)
    except Exception as e:
        logger.error("Sync repair failed: %s", e, exc_info=True)
        report.result = "error"
        report.reason = str(e)[:300]
    finally:
        report.finished_at = _now_iso()
        _record_cycle(config, report)
    return report
