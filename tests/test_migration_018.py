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


def _shape(conn, table):
    """Full column shape — name/type/notnull/default — for SCHEMA parity."""
    return {
        (r["name"], r["type"], r["notnull"], r["dflt_value"])
        for r in conn.execute(f"PRAGMA table_xinfo({table})")
    }


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
        assert _shape(a, "sync_state") == _shape(b, "sync_state")
        # The self-uniqueness index must exist on both paths too.
        q = ("SELECT COUNT(*) c FROM sqlite_master WHERE type = 'index' "
             "AND name = 'idx_sync_state_self'")
        assert a.execute(q).fetchone()["c"] == 1
        assert b.execute(q).fetchone()["c"] == 1
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


def test_upsert_remote_never_clobbers_self_row(initialized_library):
    """The backend's devices/ listing includes OUR own device doc; upserting
    it must be a no-op, never a last_seq clobber (S5.1 review Major #1)."""
    did, _name = get_or_create_device(initialized_library)
    update_self_state(initialized_library, last_seq=5)
    upsert_remote_device(initialized_library, did, name="imposter", last_seq=999)
    state = read_sync_state(initialized_library)
    assert state["self"]["device_id"] == did
    assert state["self"]["is_self"] == 1
    assert state["self"]["last_seq"] == 5
    assert state["self"]["name"] != "imposter"


def test_second_self_row_schema_blocked(initialized_library):
    """idx_sync_state_self: two is_self=1 rows are impossible by schema."""
    import pytest

    get_or_create_device(initialized_library)
    conn = get_connection(initialized_library.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sync_state (device_id, is_self) VALUES ('X2', 1)")
    finally:
        conn.close()


def test_read_sync_state_degrades_on_garbage_json(initialized_library):
    """'Never crash the server on bad input': a corrupt row reads as empty
    state (safe — an empty watermark just re-pulls; apply is idempotent)."""
    get_or_create_device(initialized_library)
    conn = get_connection(initialized_library.db_path)
    try:
        conn.execute(
            "UPDATE sync_state SET watermarks_json = '{not json', "
            "last_cycle_json = 'garbage{' WHERE is_self = 1")
        conn.commit()
    finally:
        conn.close()
    state = read_sync_state(initialized_library)
    assert state["watermarks"] == {}
    assert state["last_cycle"] is None


def test_remote_upsert_empty_name_keeps_known_name(initialized_library):
    upsert_remote_device(initialized_library, "01REMOTEDEVICEULID0000000Y",
                         name="phone", last_seq=1)
    upsert_remote_device(initialized_library, "01REMOTEDEVICEULID0000000Y",
                         last_seq=2)
    state = read_sync_state(initialized_library)
    remote = [d for d in state["devices"] if d["is_self"] == 0][0]
    assert remote["name"] == "phone" and remote["last_seq"] == 2
