"""Sync S5: migration 018 — sync_state device registry/watermarks."""
import sqlite3

from tiro.database import get_connection, init_db, migrate_db
from tiro.migrations import LATEST_VERSION
from tiro.sync.engine import (
    device_short,
    get_or_create_device,
    read_sync_state,
    update_self_state,
    upsert_remote_device,
)


def _cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_xinfo({table})")}


def test_migration_018_creates_sync_state(initialized_library):
    conn = get_connection(initialized_library.db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
        assert LATEST_VERSION == 18
        assert _cols(conn, "sync_state") == {
            "device_id", "name", "is_self", "last_seq",
            "watermarks_json", "last_cycle_json", "last_seen", "last_wall_ms",
        }
    finally:
        conn.close()


def test_migration_018_idempotent(initialized_library):
    from tiro.migrations import MIGRATIONS
    fn = next(f for v, _, f in MIGRATIONS if v == 18)
    conn = sqlite3.connect(initialized_library.db_path)
    try:
        fn(conn)
        fn(conn)
    finally:
        conn.close()


def test_fresh_schema_matches_migrated(tmp_path, initialized_library):
    fresh = tmp_path / "fresh.db"
    init_db(fresh)
    migrate_db(fresh)
    a, b = get_connection(fresh), get_connection(initialized_library.db_path)
    try:
        assert _cols(a, "sync_state") == _cols(b, "sync_state")
    finally:
        a.close()
        b.close()


def test_get_or_create_device_mints_once(initialized_library):
    did1, name1 = get_or_create_device(initialized_library)
    did2, name2 = get_or_create_device(initialized_library)
    assert did1 == did2 and name1 == name2
    assert len(did1) == 26  # ULID
    assert device_short(did1) == did1[-6:].lower()
    state = read_sync_state(initialized_library)
    assert state["self"]["device_id"] == did1
    assert state["self"]["is_self"] == 1


def test_state_roundtrip(initialized_library):
    get_or_create_device(initialized_library)
    upsert_remote_device(initialized_library, "01REMOTEDEVICEULID0000000X",
                         name="laptop-b", last_seq=7, last_wall_ms=123456)
    update_self_state(initialized_library,
                      watermarks={"01REMOTEDEVICEULID0000000X": 7},
                      last_cycle={"result": "ok"})
    state = read_sync_state(initialized_library)
    assert state["watermarks"] == {"01REMOTEDEVICEULID0000000X": 7}
    assert state["last_cycle"] == {"result": "ok"}
    remote = [d for d in state["devices"] if d["is_self"] == 0][0]
    assert remote["name"] == "laptop-b" and remote["last_seq"] == 7
    assert remote["last_wall_ms"] == 123456
