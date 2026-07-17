"""Sync S5: engine unit tests — config plumbing, audited adapter, cycle."""
import json

import pytest

from tiro.sync.engine import (
    AuditedAdapter,
    SyncConfigError,
    adapter_for_config,
    resolve_encryption,
)


def _sync_audit_entries(config) -> list[dict]:
    """Read all service='sync' audit lines from {library}/audit/*.jsonl."""
    audit_dir = config.library / "audit"
    entries: list[dict] = []
    if not audit_dir.exists():
        return entries
    for path in sorted(audit_dir.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            entry = json.loads(line)
            if entry.get("service") == "sync":
                entries.append(entry)
    return entries


def test_sync_config_defaults(test_config):
    assert test_config.sync_enabled is False
    assert test_config.sync_backend == "filesystem"
    assert test_config.sync_interval_s == 300
    assert test_config.sync_encrypt == "auto"
    assert test_config.sync_identity == ""


@pytest.mark.parametrize("backend,encrypt,expected", [
    ("filesystem", "auto", False), ("s3", "auto", True), ("webdav", "auto", True),
    ("filesystem", "on", True), ("s3", "off", False),
])
def test_resolve_encryption(test_config, backend, encrypt, expected):
    test_config.sync_backend = backend
    test_config.sync_encrypt = encrypt
    assert resolve_encryption(test_config) is expected


def test_adapter_for_config_filesystem(initialized_library, tmp_path):
    from tiro.sync.adapters.filesystem import FilesystemAdapter
    from tiro.sync.engine import get_or_create_device

    initialized_library.sync_backend = "filesystem"
    initialized_library.sync_path = str(tmp_path / "backend")
    adapter = adapter_for_config(initialized_library)
    assert isinstance(adapter, AuditedAdapter)
    assert isinstance(adapter.inner, FilesystemAdapter)
    device_id, _name = get_or_create_device(initialized_library)
    assert adapter.inner.device_id == device_id


def test_adapter_for_config_unconfigured_raises(initialized_library):
    initialized_library.sync_backend = "filesystem"
    initialized_library.sync_path = ""
    with pytest.raises(SyncConfigError):
        adapter_for_config(initialized_library)

    initialized_library.sync_backend = "carrier-pigeon"
    with pytest.raises(SyncConfigError):
        adapter_for_config(initialized_library)


async def test_audited_adapter_logs_lines(initialized_library, tmp_path):
    initialized_library.sync_backend = "filesystem"
    initialized_library.sync_path = str(tmp_path / "backend")
    adapter = adapter_for_config(initialized_library)

    await adapter.put("objects/ab/cdef.age", b"hello")
    assert await adapter.get("objects/ab/cdef.age") == b"hello"
    assert await adapter.list("objects/") == ["objects/ab/cdef.age"]

    entries = _sync_audit_entries(initialized_library)
    assert [e["endpoint"] for e in entries] == ["put", "get", "list"]
    assert all(e["success"] for e in entries)
    assert entries[0]["bytes_out"] == 5
    assert entries[1]["bytes_in"] == 5
    assert entries[2]["count"] == 1


async def test_audited_adapter_logs_failure_and_reraises(
    initialized_library, tmp_path
):
    from tiro.sync.adapters.base import KeyMissing

    initialized_library.sync_backend = "filesystem"
    initialized_library.sync_path = str(tmp_path / "backend")
    adapter = adapter_for_config(initialized_library)

    with pytest.raises(KeyMissing):
        await adapter.get("missing/key.age")

    failures = [e for e in _sync_audit_entries(initialized_library)
                if e["endpoint"] == "get"]
    assert len(failures) == 1
    assert failures[0]["success"] is False
    assert failures[0]["error"]


async def test_audited_adapter_lock_contention_is_not_an_error(
    initialized_library, tmp_path
):
    initialized_library.sync_backend = "filesystem"
    initialized_library.sync_path = str(tmp_path / "backend")
    # Both adapters share the self device_id — fine here: the filesystem
    # lock is non-reentrant (O_EXCL file), so a second holder still loses.
    adapter_a = adapter_for_config(initialized_library)
    adapter_b = adapter_for_config(initialized_library)

    assert await adapter_a.lock(120) is True
    assert await adapter_b.lock(120) is False

    lock_entries = [e for e in _sync_audit_entries(initialized_library)
                    if e["endpoint"] == "lock"]
    assert len(lock_entries) == 2
    # A held lock is an answer, not a fault.
    assert all(e["success"] for e in lock_entries)

    await adapter_a.unlock()


def test_resolve_encryption_refuses_unknown_value(test_config):
    """TIRO_SYNC_ENCRYPT=true must never silently mean plaintext (S5.2
    review Major #2): unknown values refuse instead of falling to auto."""
    test_config.sync_backend = "filesystem"
    test_config.sync_encrypt = "true"
    with pytest.raises(SyncConfigError):
        resolve_encryption(test_config)


def test_adapter_factory_failure_is_side_effect_free(initialized_library):
    """A failing factory call must not mint a device identity (S5.2 review
    Major #1) — a status probe against a misconfigured library stays pure."""
    from tiro.sync.engine import read_sync_state

    initialized_library.sync_backend = "carrier-pigeon"
    with pytest.raises(SyncConfigError):
        adapter_for_config(initialized_library)
    initialized_library.sync_backend = "filesystem"
    initialized_library.sync_path = "   "
    with pytest.raises(SyncConfigError):
        adapter_for_config(initialized_library)
    assert read_sync_state(initialized_library)["self"] is None


def test_adapter_factory_s3_and_webdav_require_fields(initialized_library):
    initialized_library.sync_backend = "s3"
    initialized_library.sync_s3_endpoint = "https://s3.example"
    initialized_library.sync_s3_bucket = "b"
    # access/secret keys missing
    with pytest.raises(SyncConfigError, match="s3 backend requires"):
        adapter_for_config(initialized_library)
    initialized_library.sync_backend = "webdav"
    initialized_library.sync_webdav_url = "https://dav.example"
    with pytest.raises(SyncConfigError, match="webdav backend requires"):
        adapter_for_config(initialized_library)


async def test_audited_adapter_delete_logs_line(initialized_library, tmp_path):
    initialized_library.sync_backend = "filesystem"
    initialized_library.sync_path = str(tmp_path / "backend")
    adapter = adapter_for_config(initialized_library)
    await adapter.put("objects/ab/x.age", b"x")
    await adapter.delete("objects/ab/x.age")
    entries = [e for e in _sync_audit_entries(initialized_library)
               if e["endpoint"] == "delete"]
    assert len(entries) == 1 and entries[0]["success"] is True


# --- Task 3: pull path — watermarks, gap/quarantine, guard, alias remap ------


def _seed_segment(backend_root, device_id, seq, ops):
    """Seed a backend the way a real push would: objects first, then the
    segment, plus a device doc. PlainCodec — crypto-ON is Task 9's job."""
    from tiro.sync.crypto import PlainCodec
    from tiro.sync.snapshot import (
        DeviceInfo,
        device_key,
        encode_device_doc,
        encode_segment,
        journal_key,
        object_key,
    )

    codec = PlainCodec()
    blob, objects = encode_segment(ops, codec)
    for h, obj_blob in objects.items():
        p = backend_root / object_key(h)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(obj_blob)
    seg = backend_root / journal_key(device_id, seq)
    seg.parent.mkdir(parents=True, exist_ok=True)
    seg.write_bytes(blob)
    doc = backend_root / device_key(device_id)
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(encode_device_doc(DeviceInfo(
        device_id=device_id, name="remote",
        last_seen="2026-07-17T00:00:00Z", last_seq=seq)))


async def _run_pull(config, backend_root, *, accept_mass_delete=False):
    from tiro.sync.crypto import PlainCodec
    from tiro.sync.engine import (
        CycleReport,
        _pull,
        adapter_for_config,
        get_or_create_device,
    )
    from tiro.sync.journal import HLCClock

    config.sync_backend = "filesystem"
    config.sync_path = str(backend_root)
    adapter = adapter_for_config(config)
    device_id, _name = get_or_create_device(config)
    report = CycleReport()
    clock_state: dict = {}
    ok = await _pull(config, adapter, PlainCodec(), HLCClock(device_id),
                     clock_state, report,
                     accept_mass_delete=accept_mass_delete)
    return ok, report, clock_state


def _article_val(config, article_id, col):
    from tiro.database import get_connection

    conn = get_connection(config.db_path)
    try:
        return conn.execute(
            f"SELECT {col} AS v FROM articles WHERE id = ?", (article_id,)
        ).fetchone()["v"]
    finally:
        conn.close()


def _article_count(config):
    from tiro.database import get_connection

    conn = get_connection(config.db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
    finally:
        conn.close()


def _synced_article(config):
    """Ingest one article and mark it 'already synced' (shadow saved)."""
    from tests.test_reconcile import _ingest
    from tiro.sync.manifest import build_manifest, save_shadow

    art = _ingest(config)
    save_shadow(config, build_manifest(config))
    return art["id"], _article_val(config, art["id"], "uid")


REMOTE_DEV = "01REMOTEDEV00000000000000A"


def _remote_meta(uid, *, field="rating", value=2, seq_clock=None):
    from tiro.migrations import new_ulid
    from tiro.sync.journal import HLCClock, Meta

    clock = seq_clock or HLCClock(REMOTE_DEV)
    return Meta(op_id=new_ulid(), hlc=clock.tick(), device=REMOTE_DEV,
                uid=uid, field=field, value=value,
                ts="2026-07-17T09:00:00Z")


async def test_pull_applies_remote_meta_and_advances_watermark(
    initialized_library, tmp_path
):
    from tiro.sync.engine import read_sync_state

    article_id, uid = _synced_article(initialized_library)
    backend = tmp_path / "backend"
    _seed_segment(backend, REMOTE_DEV, 1, [_remote_meta(uid)])

    ok, report, _state = await _run_pull(initialized_library, backend)

    assert ok is True
    assert report.result == "ok"
    assert report.pulled_segments == 1
    assert report.applied >= 1
    assert _article_val(initialized_library, article_id, "rating") == 2
    state = read_sync_state(initialized_library)
    assert state["watermarks"] == {REMOTE_DEV: 1}


async def test_pull_quarantines_corrupt_segment(initialized_library, tmp_path):
    from tiro.sync.engine import read_sync_state
    from tiro.sync.snapshot import journal_key

    article_id, _uid = _synced_article(initialized_library)
    backend = tmp_path / "backend"
    seg = backend / journal_key(REMOTE_DEV, 1)
    seg.parent.mkdir(parents=True, exist_ok=True)
    seg.write_bytes(b"\xff\xfe not a segment at all")

    ok, report, _state = await _run_pull(initialized_library, backend)

    assert ok is False
    assert report.result == "needs_attention"
    assert REMOTE_DEV in report.reason
    assert read_sync_state(initialized_library)["watermarks"] == {}
    # Nothing half-applied.
    assert report.applied == 0
    assert _article_val(initialized_library, article_id, "rating") is None


async def test_pull_mass_delete_guard_and_acceptance(
    initialized_library, tmp_path
):
    from tests.test_reconcile import _ingest
    from tiro.database import get_connection
    from tiro.migrations import new_ulid
    from tiro.sync.journal import HLCClock, RowDel
    from tiro.sync.manifest import build_manifest, save_shadow

    for i in range(12):
        _ingest(initialized_library, title=f"Article {i}",
                url=f"https://example.com/a{i}")
    save_shadow(initialized_library, build_manifest(initialized_library))
    conn = get_connection(initialized_library.db_path)
    try:
        rows = conn.execute(
            "SELECT uid, body_hash FROM articles").fetchall()
    finally:
        conn.close()
    assert len(rows) == 12

    clock = HLCClock(REMOTE_DEV)
    ops = [RowDel(op_id=new_ulid(), hlc=clock.tick(), device=REMOTE_DEV,
                  uid=r["uid"], table="articles", observed=r["body_hash"])
           for r in rows]
    backend = tmp_path / "backend"
    _seed_segment(backend, REMOTE_DEV, 1, ops)

    ok, report, _state = await _run_pull(initialized_library, backend)
    assert ok is False
    assert report.result == "needs_attention"
    assert report.guard
    assert report.reason == "mass_delete_guard"
    assert _article_count(initialized_library) == 12

    ok2, report2, _state2 = await _run_pull(
        initialized_library, backend, accept_mass_delete=True)
    assert ok2 is True
    assert _article_count(initialized_library) == 0


async def test_pull_gap_detection(initialized_library, tmp_path):
    article_id, uid = _synced_article(initialized_library)
    backend = tmp_path / "backend"
    _seed_segment(backend, REMOTE_DEV, 3, [_remote_meta(uid)])

    ok, report, _state = await _run_pull(initialized_library, backend)

    assert ok is False
    assert report.result == "needs_attention"
    assert "gap" in report.reason
    assert REMOTE_DEV in report.reason
    assert report.applied == 0
    assert _article_val(initialized_library, article_id, "rating") is None


async def test_pull_remaps_aliased_uid_to_survivor(
    initialized_library, tmp_path
):
    from tiro.database import get_connection
    from tiro.migrations import new_ulid
    from tiro.sync.journal import canonical_json

    article_id, survivor_uid = _synced_article(initialized_library)
    old_uid = new_ulid()
    # Exactly the row shape merge.py::_record_alias writes.
    conn = get_connection(initialized_library.db_path)
    try:
        conn.execute(
            "INSERT INTO sync_shadow (kind, uid, hash, fields_json, hlc, "
            "deleted_at) VALUES ('alias', ?, NULL, ?, NULL, NULL)",
            (old_uid, canonical_json({"new_uid": survivor_uid})))
        conn.commit()
    finally:
        conn.close()

    backend = tmp_path / "backend"
    _seed_segment(backend, REMOTE_DEV, 1, [_remote_meta(old_uid)])

    ok, report, _state = await _run_pull(initialized_library, backend)

    assert ok is True
    assert report.applied >= 1
    assert _article_val(initialized_library, article_id, "rating") == 2


def test_remap_alias_uids_chain_cycle_and_scope():
    from dataclasses import replace as dc_replace

    from tiro.sync.engine import _remap_alias_uids
    from tiro.sync.journal import HLC, Alias, FileDel, FilePut, Meta, RowDel

    hlc = HLC(1, 0, "dev")
    meta = Meta(op_id="o1", hlc=hlc, device="dev", uid="a",
                field="rating", value=1, ts="t")

    # Chain a -> b -> c follows to the survivor.
    out = _remap_alias_uids([meta], {"a": "b", "b": "c"})
    assert out[0].uid == "c"

    # Cycle a -> b -> a stops safely, op untouched.
    out = _remap_alias_uids([meta], {"a": "b", "b": "a"})
    assert out[0].uid == "a"

    # Article-scoped ops remap; everything else passes through untouched.
    aliases = {"a": "z"}
    row_del_articles = RowDel(op_id="o2", hlc=hlc, device="dev", uid="a",
                              table="articles", observed=None)
    row_del_tags = dc_replace(row_del_articles, op_id="o3", table="tags")
    fp_article = FilePut(op_id="o4", hlc=hlc, device="dev", uid="a",
                         path_hint="articles/x.md", object_hash="h")
    fp_note = dc_replace(fp_article, op_id="o5", path_hint="notes/x.md")
    fd_article = FileDel(op_id="o6", hlc=hlc, device="dev", uid="a",
                         path_hint="articles/x.md")
    alias_op = Alias(op_id="o7", hlc=hlc, device="dev", uid="a",
                     new_uid="z")
    out = _remap_alias_uids(
        [row_del_articles, row_del_tags, fp_article, fp_note, fd_article,
         alias_op], aliases)
    assert out[0].uid == "z"      # RowDel(articles) remapped
    assert out[1].uid == "a"      # RowDel(tags) untouched
    assert out[2].uid == "z"      # FilePut under articles/ remapped
    assert out[3].uid == "a"      # FilePut under notes/ untouched
    assert out[4].uid == "z"      # FileDel under articles/ remapped
    assert out[5].uid == "a"      # Alias op itself untouched


async def test_pull_refreshes_remote_device_registry(
    initialized_library, tmp_path
):
    from tiro.sync.engine import get_or_create_device, read_sync_state

    _article_id, uid = _synced_article(initialized_library)
    backend = tmp_path / "backend"
    _seed_segment(backend, REMOTE_DEV, 1, [_remote_meta(uid)])

    ok, _report, _state = await _run_pull(initialized_library, backend)
    assert ok is True

    state = read_sync_state(initialized_library)
    remote = next(d for d in state["devices"]
                  if d["device_id"] == REMOTE_DEV)
    assert remote["is_self"] == 0
    assert remote["name"] == "remote"
    assert remote["last_seq"] == 1
    assert remote["last_wall_ms"]  # segment op walls were tracked
    self_id, _n = get_or_create_device(initialized_library)
    assert state["self"]["device_id"] == self_id
    assert state["self"]["is_self"] == 1


async def test_pull_per_op_errors_do_not_quarantine(
    initialized_library, tmp_path
):
    from tiro.sync.engine import read_sync_state
    from tiro.sync.journal import HLCClock

    article_id, uid = _synced_article(initialized_library)
    clock = HLCClock(REMOTE_DEV)
    good = _remote_meta(uid, seq_clock=clock)
    bad = _remote_meta(uid, field="title", value="nope", seq_clock=clock)
    backend = tmp_path / "backend"
    _seed_segment(backend, REMOTE_DEV, 1, [good, bad])

    ok, report, _state = await _run_pull(initialized_library, backend)

    assert ok is True
    assert report.errors == 1
    assert report.applied >= 1
    assert _article_val(initialized_library, article_id, "rating") == 2
    assert read_sync_state(initialized_library)["watermarks"] == {
        REMOTE_DEV: 1}


# --- S5.3-fix review wave: B1 / M1 / M2 / M3 / m1 + named gap tests ----------


async def test_pull_quarantines_malformed_object_hash(
    initialized_library, tmp_path
):
    """B1: a traversal-shaped object_hash must never reach adapter.get —
    left unfetched, decode_segment's JournalError quarantines the segment
    instead of an AdapterError escaping _pull."""
    from tiro.sync.engine import read_sync_state
    from tiro.sync.snapshot import journal_key

    _synced_article(initialized_library)
    # Hand-built JSONL (honest with PlainCodec: blob bytes == text bytes) —
    # the pre-scan only needs kind + payload.object_hash.
    line = json.dumps({
        "op": "OP00000000000000000000000001", "hlc": "0000000000001-000000-x",
        "device": REMOTE_DEV, "kind": "file_put", "uid": "u1",
        "base_hash": None,
        "payload": {"path_hint": "articles/x.md",
                    "object_hash": "../../secrets"},
    })
    backend = tmp_path / "backend"
    seg = backend / journal_key(REMOTE_DEV, 1)
    seg.parent.mkdir(parents=True, exist_ok=True)
    seg.write_bytes((line + "\n").encode("utf-8"))

    # Must not raise (no AdapterError escapes) — quarantine instead.
    ok, report, _state = await _run_pull(initialized_library, backend)

    assert ok is False
    assert report.result == "needs_attention"
    assert "missing object" in report.reason
    assert read_sync_state(initialized_library)["watermarks"] == {}


async def test_pull_alias_in_segment_reaches_later_segment_ops(
    initialized_library, tmp_path
):
    """M1: an Alias applied in segment 1 must remap segment 2's ops in the
    SAME pull — the alias map is reloaded per segment, not once per run."""
    from tiro.migrations import new_ulid
    from tiro.sync.engine import read_sync_state
    from tiro.sync.journal import Alias, HLCClock

    article_id, survivor_uid = _synced_article(initialized_library)
    old_uid = new_ulid()
    clock = HLCClock(REMOTE_DEV)
    alias_op = Alias(op_id=new_ulid(), hlc=clock.tick(), device=REMOTE_DEV,
                     uid=old_uid, new_uid=survivor_uid)
    backend = tmp_path / "backend"
    _seed_segment(backend, REMOTE_DEV, 1, [alias_op])
    _seed_segment(backend, REMOTE_DEV, 2,
                  [_remote_meta(old_uid, seq_clock=clock)])

    ok, report, _state = await _run_pull(initialized_library, backend)

    assert ok is True
    assert report.pulled_segments == 2
    # Pre-fix this was deferred_unknown_article + watermark advance —
    # permanent meta loss on the surviving article.
    assert _article_val(initialized_library, article_id, "rating") == 2
    assert read_sync_state(initialized_library)["watermarks"] == {
        REMOTE_DEV: 2}


async def test_pull_operational_error_propagates_and_holds_watermark(
    initialized_library, tmp_path, monkeypatch
):
    """M2 (engine level): a transient sqlite3.OperationalError out of apply
    must propagate out of _pull with the watermark NOT advanced — never be
    folded into report.errors and paved over by a watermark advance."""
    import sqlite3

    from tiro.sync.engine import read_sync_state

    _article_id, uid = _synced_article(initialized_library)
    backend = tmp_path / "backend"
    _seed_segment(backend, REMOTE_DEV, 1, [_remote_meta(uid)])

    def boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("tiro.sync.merge.apply_ops", boom)
    with pytest.raises(sqlite3.OperationalError):
        await _run_pull(initialized_library, backend)
    assert read_sync_state(initialized_library)["watermarks"] == {}


async def test_pull_mass_delete_acceptance_is_one_shot(
    initialized_library, tmp_path
):
    """M3: accept_mass_delete covers exactly ONE guard trip per run — a
    second mass-delete segment in the same run trips the guard again and
    holds its watermark."""
    from tests.test_reconcile import _ingest
    from tiro.database import get_connection
    from tiro.migrations import new_ulid
    from tiro.sync.engine import read_sync_state
    from tiro.sync.journal import HLCClock, RowDel
    from tiro.sync.manifest import build_manifest, save_shadow

    for i in range(24):
        _ingest(initialized_library, title=f"Article {i}",
                url=f"https://example.com/a{i}")
    save_shadow(initialized_library, build_manifest(initialized_library))
    conn = get_connection(initialized_library.db_path)
    try:
        rows = conn.execute(
            "SELECT uid, body_hash FROM articles ORDER BY id").fetchall()
    finally:
        conn.close()
    assert len(rows) == 24

    clock = HLCClock(REMOTE_DEV)

    def _dels(chunk):
        return [RowDel(op_id=new_ulid(), hlc=clock.tick(), device=REMOTE_DEV,
                       uid=r["uid"], table="articles", observed=r["body_hash"])
                for r in chunk]

    backend = tmp_path / "backend"
    _seed_segment(backend, REMOTE_DEV, 1, _dels(rows[:12]))
    _seed_segment(backend, REMOTE_DEV, 2, _dels(rows[12:]))

    ok, report, _state = await _run_pull(
        initialized_library, backend, accept_mass_delete=True)

    assert ok is False
    assert report.result == "needs_attention"
    assert report.guard
    assert report.reason == "mass_delete_guard"
    # Segment 1 consumed the acceptance and applied; segment 2 held.
    assert report.pulled_segments == 1
    assert _article_count(initialized_library) == 12
    assert read_sync_state(initialized_library)["watermarks"] == {
        REMOTE_DEV: 1}


def test_read_sync_state_type_validates_watermarks(initialized_library):
    """m1: watermarks_json must be a dict of str->int — anything else
    degrades to empty (re-pull) without raising."""
    from tiro.database import get_connection
    from tiro.sync.engine import get_or_create_device, read_sync_state

    get_or_create_device(initialized_library)

    def _set(raw):
        conn = get_connection(initialized_library.db_path)
        try:
            conn.execute("UPDATE sync_state SET watermarks_json = ? "
                         "WHERE is_self = 1", (raw,))
            conn.commit()
        finally:
            conn.close()

    _set("[1,2]")
    assert read_sync_state(initialized_library)["watermarks"] == {}
    _set('{"dev":"abc"}')
    assert read_sync_state(initialized_library)["watermarks"] == {}
    # Mixed dict keeps only the valid str->int entries.
    _set('{"dev": 3, "bad": "x", "flag": true}')
    assert read_sync_state(initialized_library)["watermarks"] == {"dev": 3}


async def test_pull_persists_watermark_of_applied_segment_before_quarantine(
    initialized_library, tmp_path
):
    """Named gap test 1: segment 1 applies, segment 2 quarantines — the
    PERSISTED watermark must record segment 1 (per-segment advance)."""
    from tiro.sync.engine import read_sync_state
    from tiro.sync.snapshot import journal_key

    article_id, uid = _synced_article(initialized_library)
    backend = tmp_path / "backend"
    _seed_segment(backend, REMOTE_DEV, 1, [_remote_meta(uid)])
    seg2 = backend / journal_key(REMOTE_DEV, 2)
    seg2.write_bytes(b"\xff\xfe not a segment at all")

    ok, report, _state = await _run_pull(initialized_library, backend)

    assert ok is False
    assert report.result == "needs_attention"
    assert report.pulled_segments == 1
    assert _article_val(initialized_library, article_id, "rating") == 2
    assert read_sync_state(initialized_library)["watermarks"] == {
        REMOTE_DEV: 1}


def _remote_file_put(body, *, uid=None, path_hint="articles/synced-post.md",
                     clock=None):
    from tiro.anchors import content_hash
    from tiro.migrations import new_ulid
    from tiro.sync.journal import FilePut, HLCClock

    clock = clock or HLCClock(REMOTE_DEV)
    return FilePut(op_id=new_ulid(), hlc=clock.tick(), device=REMOTE_DEV,
                   uid=uid or new_ulid(), path_hint=path_hint,
                   object_hash=content_hash(body), body=body)


FILE_PUT_BODY = ("---\ntitle: Synced Post\n"
                 "url: https://example.com/synced-post\n---\n\n"
                 "Hello from the remote device.\n")


async def test_pull_file_put_end_to_end_materializes_article(
    initialized_library, tmp_path
):
    """Named gap test 2a: a FilePut driven through _pull with its object
    blob on the backend materializes a local article."""
    from tiro.database import get_connection
    from tiro.sync.engine import read_sync_state

    op = _remote_file_put(FILE_PUT_BODY)
    backend = tmp_path / "backend"
    _seed_segment(backend, REMOTE_DEV, 1, [op])

    ok, report, _state = await _run_pull(initialized_library, backend)

    assert ok is True
    assert report.applied >= 1
    conn = get_connection(initialized_library.db_path)
    try:
        row = conn.execute(
            "SELECT title, ingestion_method, markdown_path FROM articles "
            "WHERE uid = ?", (op.uid,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["title"] == "Synced Post"
    assert row["ingestion_method"] == "sync"
    assert (initialized_library.library / op.path_hint).exists()
    assert read_sync_state(initialized_library)["watermarks"] == {
        REMOTE_DEV: 1}


async def test_pull_file_put_missing_object_blob_quarantines(
    initialized_library, tmp_path
):
    """Named gap test 2b: the object blob deleted from the backend →
    quarantine, watermark held, no article materialized."""
    from tiro.anchors import content_hash
    from tiro.database import get_connection
    from tiro.sync.engine import read_sync_state
    from tiro.sync.snapshot import object_key

    op = _remote_file_put(FILE_PUT_BODY)
    backend = tmp_path / "backend"
    _seed_segment(backend, REMOTE_DEV, 1, [op])
    (backend / object_key(content_hash(FILE_PUT_BODY))).unlink()

    ok, report, _state = await _run_pull(initialized_library, backend)

    assert ok is False
    assert report.result == "needs_attention"
    assert "missing object" in report.reason
    assert read_sync_state(initialized_library)["watermarks"] == {}
    conn = get_connection(initialized_library.db_path)
    try:
        row = conn.execute("SELECT 1 FROM articles WHERE uid = ?",
                           (op.uid,)).fetchone()
    finally:
        conn.close()
    assert row is None


# --- segment_object_refs (S3 pre-scan single-sourcing) -----------------------


def test_segment_object_refs_file_put():
    from tiro.anchors import content_hash
    from tiro.sync.crypto import PlainCodec
    from tiro.sync.journal import HLC, FilePut
    from tiro.sync.snapshot import encode_segment, segment_object_refs

    body = "# Hello\n\nbody\n"
    op = FilePut(op_id="o1", hlc=HLC(1, 0, "dev"), device="dev", uid="u1",
                 path_hint="articles/x.md", object_hash=content_hash(body),
                 body=body)
    blob, objects = encode_segment([op], PlainCodec())
    refs = segment_object_refs(blob, PlainCodec())
    assert refs == {content_hash(body)}
    assert refs == set(objects)


def test_segment_object_refs_meta_only_segment():
    from tiro.sync.crypto import PlainCodec
    from tiro.sync.journal import HLC, Meta
    from tiro.sync.snapshot import encode_segment, segment_object_refs

    op = Meta(op_id="o1", hlc=HLC(1, 0, "dev"), device="dev", uid="u1",
              field="rating", value=1, ts="2026-07-17T00:00:00Z")
    blob, objects = encode_segment([op], PlainCodec())
    assert objects == {}
    assert segment_object_refs(blob, PlainCodec()) == set()


def test_segment_object_refs_garbage_raises_journal_error():
    from tiro.sync.crypto import PlainCodec
    from tiro.sync.journal import JournalError
    from tiro.sync.snapshot import segment_object_refs

    with pytest.raises(JournalError):
        segment_object_refs(b"{not json\n", PlainCodec())
    with pytest.raises(JournalError):
        segment_object_refs(b"\xff\xfe not utf-8", PlainCodec())


# --- codec_for_config --------------------------------------------------------


def test_codec_for_config_plain_and_identity_required(test_config):
    from tiro.sync.crypto import PlainCodec
    from tiro.sync.engine import codec_for_config

    test_config.sync_backend = "filesystem"
    test_config.sync_encrypt = "auto"
    assert isinstance(codec_for_config(test_config), PlainCodec)

    test_config.sync_encrypt = "on"
    test_config.sync_identity = ""
    with pytest.raises(SyncConfigError, match="sync_identity"):
        codec_for_config(test_config)


# --- Task 4: full sync_cycle — push ordering, format pin, compaction/GC ------


def _cycle_cfg(config, backend_root):
    config.sync_backend = "filesystem"
    config.sync_path = str(backend_root)
    return config


async def test_full_cycle_pushes_local_state(initialized_library, tmp_path):
    from tests.test_reconcile import _ingest
    from tiro.sync.crypto import parse_format_json
    from tiro.sync.engine import (
        get_or_create_device,
        read_sync_state,
        sync_cycle,
    )
    from tiro.sync.snapshot import device_key, parse_device_doc

    backend = tmp_path / "backend"
    cfg = _cycle_cfg(initialized_library, backend)
    _ingest(cfg)

    report = await sync_cycle(cfg)

    assert report.result == "ok"
    assert report.pushed_ops > 0
    assert report.pushed_objects > 0
    # Plaintext auto-init wrote format.json.
    fmt = parse_format_json((backend / "format.json").read_text())
    assert fmt.encryption == "none"
    device_id, _name = get_or_create_device(cfg)
    doc = parse_device_doc(
        device_id, (backend / device_key(device_id)).read_text())
    assert doc.last_seq == 1
    assert doc.acked == {}
    state = read_sync_state(cfg)
    assert state["self"]["last_seq"] == 1
    assert state["last_cycle"]["result"] == "ok"
    # HONEST first-cycle contract: the no-snapshot-yet rule takes a snapshot
    # immediately, and its GC deletes the just-pushed, fully-acked segment —
    # so journal/ is EMPTY and one snapshot manifest exists.
    assert len(list(backend.glob("snapshots/*/manifest.age"))) == 1
    assert list(backend.glob("journal/**/*.age")) == []

    # Second cycle: idempotent — nothing pushed, no new segment or snapshot.
    report2 = await sync_cycle(cfg)
    assert report2.result == "ok"
    assert report2.pushed_ops == 0
    assert report2.pushed_objects == 0
    assert list(backend.glob("journal/**/*.age")) == []
    assert len(list(backend.glob("snapshots/*/manifest.age"))) == 1
    assert read_sync_state(cfg)["self"]["last_seq"] == 1


async def test_cycle_skips_when_backend_lock_held(initialized_library,
                                                  tmp_path):
    from tests.test_reconcile import _ingest
    from tiro.sync.engine import sync_cycle

    backend = tmp_path / "backend"
    cfg = _cycle_cfg(initialized_library, backend)
    _ingest(cfg)
    holder = adapter_for_config(cfg)
    assert await holder.lock(120) is True
    try:
        report = await sync_cycle(cfg)
    finally:
        await holder.unlock()

    assert report.result == "skipped_lock"
    assert "backend" in report.reason
    assert report.pushed_ops == 0
    assert list(backend.glob("journal/**/*.age")) == []


async def test_cycle_in_process_lock(initialized_library, tmp_path):
    from tiro.sync import engine

    backend = tmp_path / "backend"
    cfg = _cycle_cfg(initialized_library, backend)

    assert engine._CYCLE_LOCK.acquire(blocking=False)
    try:
        assert engine.sync_cycle_running() is True
        report = await engine.sync_cycle(cfg)
    finally:
        engine._CYCLE_LOCK.release()

    assert engine.sync_cycle_running() is False
    assert report.result == "skipped_lock"
    assert "in this process" in report.reason
    # The backend was never touched (no adapter was even constructed).
    assert not backend.exists()


def _fail_journal_puts(monkeypatch):
    """Patch AuditedAdapter.put to fail on journal/ keys until disarmed."""
    orig_put = AuditedAdapter.put
    armed = {"on": True}

    async def flaky_put(self, key, data):
        if armed["on"] and key.startswith("journal/"):
            raise OSError("disk full")
        return await orig_put(self, key, data)

    monkeypatch.setattr(AuditedAdapter, "put", flaky_put)
    return armed


async def test_push_failure_leaves_shadow_unsaved(initialized_library,
                                                  tmp_path, monkeypatch):
    from tests.test_reconcile import _ingest
    from tiro.sync.engine import sync_cycle

    backend = tmp_path / "backend"
    cfg = _cycle_cfg(initialized_library, backend)
    _ingest(cfg)
    armed = _fail_journal_puts(monkeypatch)

    report = await sync_cycle(cfg)
    assert report.result == "error"

    # The shadow was NOT saved on the failed cycle: the next cycle
    # re-derives the same diff and pushes it.
    armed["on"] = False
    report2 = await sync_cycle(cfg)
    assert report2.result == "ok"
    assert report2.pushed_ops > 0


async def test_cycle_survives_lock_absence(initialized_library, tmp_path,
                                           monkeypatch):
    from tests.test_reconcile import _ingest
    from tiro.sync.engine import sync_cycle

    backend = tmp_path / "backend"
    cfg = _cycle_cfg(initialized_library, backend)
    _ingest(cfg)

    async def broken_lock(self, ttl_s):
        raise OSError("lock backend down")

    monkeypatch.setattr(AuditedAdapter, "lock", broken_lock)
    report = await sync_cycle(cfg)

    assert report.result == "ok"
    assert report.pushed_ops > 0


def _age_format_json():
    """A syntactically-valid AGE-mode format.json (weak kdf, cheap)."""
    import base64

    from tiro.migrations import new_ulid
    from tiro.sync.crypto import (
        AgeCodec,
        KdfParams,
        build_format_json,
        derive_recovery_code,
    )

    kdf = KdfParams(salt_b64=base64.b64encode(b"0123456789abcdef").decode(),
                    m=8, t=1, p=1)
    recipient = AgeCodec(derive_recovery_code("x", kdf)).recipient
    return build_format_json(new_ulid(), kdf=kdf, age_recipient=recipient)


async def test_cycle_encryption_pin_mismatch(initialized_library, tmp_path):
    from tests.test_reconcile import _ingest
    from tiro.migrations import new_ulid
    from tiro.sync.crypto import build_format_json
    from tiro.sync.engine import sync_cycle

    # (a) Backend says AGE, local pin resolves OFF (filesystem + auto).
    backend_a = tmp_path / "backend-a"
    backend_a.mkdir(parents=True)
    (backend_a / "format.json").write_text(_age_format_json())
    cfg = _cycle_cfg(initialized_library, backend_a)
    _ingest(cfg)

    report = await sync_cycle(cfg)
    assert report.result == "needs_attention"
    assert "encryption mode mismatch" in report.reason
    assert report.pushed_ops == 0
    assert list(backend_a.glob("journal/**/*.age")) == []
    assert list(backend_a.glob("devices/*.json")) == []

    # (b) Backend says plaintext, local pin is ON.
    backend_b = tmp_path / "backend-b"
    backend_b.mkdir(parents=True)
    (backend_b / "format.json").write_text(build_format_json(new_ulid()))
    cfg.sync_path = str(backend_b)
    cfg.sync_encrypt = "on"

    report_b = await sync_cycle(cfg)
    assert report_b.result == "needs_attention"
    assert "encryption mode mismatch" in report_b.reason
    assert report_b.pushed_ops == 0
    assert list(backend_b.glob("journal/**/*.age")) == []
    assert list(backend_b.glob("devices/*.json")) == []


async def test_cycle_records_last_cycle_always(initialized_library, tmp_path,
                                               monkeypatch):
    from tests.test_reconcile import _ingest
    from tiro.sync.engine import read_sync_state, sync_cycle

    backend = tmp_path / "backend"
    cfg = _cycle_cfg(initialized_library, backend)
    _ingest(cfg)
    _fail_journal_puts(monkeypatch)

    report = await sync_cycle(cfg)

    assert report.result == "error"
    last = read_sync_state(cfg)["last_cycle"]
    assert last["result"] == "error"
    assert last["finished_at"]


async def test_compaction_snapshot_and_gc(initialized_library, tmp_path):
    from tests.test_reconcile import _ingest
    from tiro.sync.crypto import PlainCodec
    from tiro.sync.engine import get_or_create_device, sync_cycle
    from tiro.sync.snapshot import decode_snapshot, journal_key

    backend = tmp_path / "backend"
    cfg = _cycle_cfg(initialized_library, backend)
    _ingest(cfg)
    device_id, _name = get_or_create_device(cfg)

    report = await sync_cycle(cfg)
    assert report.result == "ok"

    # Exactly one snapshot; it covers our seq-1 segment.
    snaps = list(backend.glob("snapshots/*/manifest.age"))
    assert len(snaps) == 1
    doc = decode_snapshot(snaps[0].read_bytes(), PlainCodec())
    assert doc.covers == {device_id: 1}
    # objects/ holds EXACTLY the snapshot's referenced addresses (the pushed
    # segment referenced the same blobs; object GC removed nothing live and
    # left nothing extra).
    on_disk = {p.name[: -len(".age")]
               for p in backend.glob("objects/*/*.age")}
    assert on_disk == set(doc.objects.values())
    # The seq-1 segment was snapshot-covered and self-acked -> GC'd.
    assert list(backend.glob("journal/**/*.age")) == []

    # A second article: pushed as segment seq 2, and the cadence rule
    # (1 op-segment since a fresh snapshot) takes NO second snapshot, so
    # the segment survives.
    _ingest(cfg, title="Second Article", url="https://example.com/second")
    report2 = await sync_cycle(cfg)
    assert report2.result == "ok"
    assert report2.pushed_ops > 0
    assert (backend / journal_key(device_id, 2)).exists()
    assert len(list(backend.glob("snapshots/*/manifest.age"))) == 1


async def test_cycle_unconfigured_is_clean_error(initialized_library):
    from tiro.sync.engine import sync_cycle

    # sync_path defaults to "" — filesystem backend unconfigured.
    report = await sync_cycle(initialized_library)

    assert report.result == "error"
    assert report.reason.startswith("not configured")


# --- S5.4-fix review wave: B1/M1 append-only pin, M2/M3, m1 ------------------


async def test_push_crash_after_segment_never_overwrites_seq(
    initialized_library, tmp_path, monkeypatch
):
    """B1/M1 executable pin: a crash AFTER the journal segment landed but
    BEFORE any local record (device-doc put fails) must never lead the next
    push to overwrite that seq with different bytes — the journal is
    append-only (spec §6.4). Pre-fix, the purely-local seq counter re-minted
    the SAME seq for the now-larger diff and the unconditional put
    overwrote the original segment."""
    from tests.test_reconcile import _ingest
    from tiro.sync import engine
    from tiro.sync.engine import sync_cycle
    from tiro.sync.snapshot import parse_journal_key

    backend = tmp_path / "backend"
    cfg = _cycle_cfg(initialized_library, backend)
    _ingest(cfg)

    orig_put = AuditedAdapter.put
    armed = {"on": True}

    async def flaky_put(self, key, data):
        if armed["on"] and key.startswith("devices/"):
            raise OSError("disk full")
        return await orig_put(self, key, data)

    monkeypatch.setattr(AuditedAdapter, "put", flaky_put)

    report = await sync_cycle(cfg)
    assert report.result == "error"

    segs = list(backend.glob("journal/**/*.age"))
    assert len(segs) == 1
    orig_seg = segs[0]
    orig_bytes = orig_seg.read_bytes()
    _dev, orig_seq = parse_journal_key(
        orig_seg.relative_to(backend).as_posix())

    # Local state changes between the crash and the retry — the re-derived
    # diff has DIFFERENT bytes than the stranded segment.
    _ingest(cfg, title="Second Article", url="https://example.com/second")
    armed["on"] = False

    # Compaction disabled for the retry cycle: its GC legitimately collects
    # fully-acked segments — the pin here is push-side overwrite, not GC.
    async def no_compact(*_args, **_kwargs):
        return None

    monkeypatch.setattr(engine, "_maybe_compact", no_compact)

    report2 = await sync_cycle(cfg)
    assert report2.result == "ok"
    assert report2.pushed_ops > 0

    # The original segment survives BYTE-IDENTICAL; the retry landed higher.
    assert orig_seg.exists()
    assert orig_seg.read_bytes() == orig_bytes
    seqs = sorted(parse_journal_key(p.relative_to(backend).as_posix())[1]
                  for p in backend.glob("journal/**/*.age"))
    assert orig_seq in seqs
    assert max(seqs) > orig_seq


async def test_heartbeat_refreshes_device_doc_without_journal_writes(
    initialized_library, tmp_path, monkeypatch
):
    """A no-changes cycle still refreshes the device doc's last_seen (GC
    ack progression depends on it) while last_seq holds and the journal is
    untouched."""
    from tests.test_reconcile import _ingest
    from tiro.sync import engine
    from tiro.sync.engine import get_or_create_device, sync_cycle
    from tiro.sync.snapshot import device_key, parse_device_doc

    backend = tmp_path / "backend"
    cfg = _cycle_cfg(initialized_library, backend)
    _ingest(cfg)
    report = await sync_cycle(cfg)
    assert report.result == "ok" and report.pushed_ops > 0

    device_id, _name = get_or_create_device(cfg)
    doc_path = backend / device_key(device_id)
    bytes1 = doc_path.read_bytes()
    doc1 = parse_device_doc(device_id, bytes1.decode("utf-8"))
    journal1 = sorted(p.relative_to(backend).as_posix()
                      for p in backend.glob("journal/**/*.age"))

    # Deterministic clock advance instead of a >=1s sleep.
    monkeypatch.setattr(engine, "_now_iso", lambda: "2099-01-01T00:00:00Z")

    report2 = await sync_cycle(cfg)
    assert report2.result == "ok"
    assert report2.pushed_ops == 0

    bytes2 = doc_path.read_bytes()
    doc2 = parse_device_doc(device_id, bytes2.decode("utf-8"))
    assert bytes2 != bytes1  # heartbeat rewrote the doc...
    assert doc2.last_seen != doc1.last_seen
    assert doc2.last_seq == doc1.last_seq == 1  # ...but the head held
    journal2 = sorted(p.relative_to(backend).as_posix()
                      for p in backend.glob("journal/**/*.age"))
    assert journal2 == journal1  # journal untouched


async def test_lockless_mode_skips_compaction(initialized_library, tmp_path,
                                              monkeypatch):
    """M2: with the backend lock unavailable the cycle still pushes, but
    compaction/GC is skipped — despite the no-snapshot-yet rule that would
    otherwise snapshot immediately after the first push."""
    from tests.test_reconcile import _ingest
    from tiro.sync.engine import sync_cycle

    backend = tmp_path / "backend"
    cfg = _cycle_cfg(initialized_library, backend)
    _ingest(cfg)

    async def broken_lock(self, ttl_s):
        raise OSError("lock backend down")

    monkeypatch.setattr(AuditedAdapter, "lock", broken_lock)
    report = await sync_cycle(cfg)

    assert report.result == "ok"
    assert report.pushed_ops > 0
    assert not (backend / "snapshots").exists()
    # The push itself landed (lockless push/pull stays safe).
    assert len(list(backend.glob("journal/**/*.age"))) == 1


async def test_auto_init_refused_on_nonempty_backend(initialized_library,
                                                     tmp_path):
    """m1: a backend holding sync data but NO format.json must never be
    plaintext-auto-initialized (a deleted age format.json would otherwise
    downgrade the next push to cleartext)."""
    from tiro.sync.engine import sync_cycle

    _article_id, uid = _synced_article(initialized_library)
    backend = tmp_path / "backend"
    _seed_segment(backend, REMOTE_DEV, 1, [_remote_meta(uid)])
    assert not (backend / "format.json").exists()
    cfg = _cycle_cfg(initialized_library, backend)

    report = await sync_cycle(cfg)

    assert report.result == "needs_attention"
    assert "refusing to initialize" in report.reason
    assert not (backend / "format.json").exists()


async def test_engine_built_adapter_closed_exactly_once(
    initialized_library, tmp_path, monkeypatch
):
    """M3: the cycle closes the adapter it constructed itself (exactly
    once), and NEVER closes a caller-provided one."""
    from tests.test_reconcile import _ingest
    from tiro.sync.engine import sync_cycle

    backend = tmp_path / "backend"
    cfg = _cycle_cfg(initialized_library, backend)
    _ingest(cfg)

    calls = []
    orig_aclose = AuditedAdapter.aclose

    async def recording_aclose(self):
        calls.append(self)
        await orig_aclose(self)

    monkeypatch.setattr(AuditedAdapter, "aclose", recording_aclose)

    report = await sync_cycle(cfg)  # engine builds its own adapter
    assert report.result == "ok"
    assert len(calls) == 1

    calls.clear()
    provided = adapter_for_config(cfg)
    report2 = await sync_cycle(cfg, provided)
    assert report2.result == "ok"
    assert calls == []  # caller-provided adapter left open
