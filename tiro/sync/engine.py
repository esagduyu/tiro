"""Sync engine — the IMPURE orchestrator for BYO sync (Phase 7a S5).

Pure logic lives in the sibling modules (journal/manifest/merge/snapshot/
crypto); this module owns device identity, sync_state persistence
(migration 018) and — in later S5 tasks — the push/pull cycle itself.
The cycle order is FROZEN by docs/plans/2026-07-06-sync-engine-spec.md §6.

This first slice holds only the sync_state helpers: device identity
(get_or_create_device / device_short) and the registry/watermark
read/update functions every later task consumes.
"""

import json
import logging
import platform
import sqlite3
from datetime import UTC, datetime

from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.migrations import new_ulid

logger = logging.getLogger(__name__)

LOCK_TTL_S = 120  # backend advisory-lock TTL, used by the S5.4 cycle


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
            try:
                watermarks = json.loads(self_row["watermarks_json"])
            except ValueError:
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
    head) with a possibly-stale remote-read value. An empty incoming name
    never wipes a previously-known one.
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
                last_seen = excluded.last_seen,
                last_wall_ms = COALESCE(excluded.last_wall_ms,
                                        sync_state.last_wall_ms)
            WHERE sync_state.is_self = 0
            """,
            (device_id, name, last_seq, last_seen, last_wall_ms),
        )
        conn.commit()
    finally:
        conn.close()
