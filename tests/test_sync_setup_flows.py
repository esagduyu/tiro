"""Sync S5.5: setup flows — init_backend / verify_passphrase, bootstrap,
the empty-library auto-bootstrap (D-S5-3), and repair + epoch detection."""
import pytest

from tiro.sync.engine import (
    SyncConfigError,
    adapter_for_config,
    bootstrap,
    get_or_create_device,
    init_backend,
    read_sync_state,
    repair,
    sync_cycle,
    verify_passphrase,
)

WEAK_KDF = {"m": 8, "t": 1, "p": 1}   # honest Argon2id, test-speed


def _second_library(tmp_path, name):
    """Second throwaway library on the same tmp_path. No vectorstore init:
    apply-side materialization and reconcile treat ChromaDB as best-effort
    (vector_status='pending' + retry task), same recipe as _mini_lib in
    test_sync_properties.py."""
    from tiro.config import TiroConfig
    from tiro.database import init_db, migrate_db

    cfg = TiroConfig(library_path=str(tmp_path / name))
    cfg.articles_dir.mkdir(parents=True, exist_ok=True)
    init_db(cfg.db_path)
    migrate_db(cfg.db_path)
    return cfg


def _sync_cfg(cfg, backend_root, *, encrypt="on", identity=""):
    cfg.sync_backend = "filesystem"
    cfg.sync_path = str(backend_root)
    cfg.sync_encrypt = encrypt
    cfg.sync_identity = identity
    return cfg


def _rows(config, sql, params=()):
    from tiro.database import get_connection

    conn = get_connection(config.db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _titles(config):
    return {r["title"] for r in _rows(config, "SELECT title FROM articles")}


def _count(config):
    return _rows(config, "SELECT COUNT(*) AS n FROM articles")[0]["n"]


async def test_init_backend_writes_format_and_returns_recovery(
    initialized_library, tmp_path
):
    from tiro.sync.crypto import parse_format_json

    backend = tmp_path / "backend"
    cfg = _sync_cfg(initialized_library, backend, encrypt="on")
    adapter = adapter_for_config(cfg)

    recovery = await init_backend(cfg, adapter, "hunter2",
                                  kdf_params=WEAK_KDF)

    assert recovery.startswith("AGE-SECRET-KEY-1")
    fmt = parse_format_json((backend / "format.json").read_text())
    assert fmt.sync_format == 1
    assert fmt.encryption == "age"
    assert fmt.kdf.algo == "argon2id"
    assert fmt.age_recipient.startswith("age1")

    with pytest.raises(SyncConfigError, match="already initialized"):
        await init_backend(cfg, adapter, "hunter2", kdf_params=WEAK_KDF)


async def test_verify_passphrase_right_and_wrong(initialized_library,
                                                 tmp_path):
    backend = tmp_path / "backend"
    cfg = _sync_cfg(initialized_library, backend, encrypt="on")
    adapter = adapter_for_config(cfg)
    recovery = await init_backend(cfg, adapter, "hunter2",
                                  kdf_params=WEAK_KDF)

    assert await verify_passphrase(cfg, adapter, "hunter2") == recovery
    # Wrong passphrase: clean refusal — None, no exception.
    assert await verify_passphrase(cfg, adapter, "wrong") is None


async def test_verify_passphrase_plaintext_backend_returns_empty(
    initialized_library, tmp_path
):
    backend = tmp_path / "backend"
    cfg = _sync_cfg(initialized_library, backend, encrypt="off")
    adapter = adapter_for_config(cfg)
    assert await init_backend(cfg, adapter, "") == ""
    # "" = no identity needed (callers distinguish it from None = refusal).
    assert await verify_passphrase(cfg, adapter, "anything") == ""


async def test_verify_passphrase_loud_on_newer_format(initialized_library,
                                                      tmp_path):
    """Version refusal is LOUD (SyncFormatError), never disguised as a
    wrong passphrase (None)."""
    import json

    from tiro.sync.crypto import SyncFormatError

    backend = tmp_path / "backend"
    cfg = _sync_cfg(initialized_library, backend, encrypt="on")
    adapter = adapter_for_config(cfg)
    await init_backend(cfg, adapter, "pw", kdf_params=WEAK_KDF)
    doc = json.loads((backend / "format.json").read_text())
    doc["sync_format"] = 99
    (backend / "format.json").write_text(json.dumps(doc))

    with pytest.raises(SyncFormatError):
        await verify_passphrase(cfg, adapter, "pw")


async def test_verify_passphrase_loud_on_corrupt_kdf(initialized_library,
                                                     tmp_path):
    """S5.5-fix minor #3: unusable KDF params (garbage salt) are CORRUPTION
    — SyncFormatError, never a quiet None that reads as 'wrong passphrase'
    and sends the user retyping a passphrase forever."""
    import json

    from tiro.sync.crypto import SyncFormatError

    backend = tmp_path / "backend"
    cfg = _sync_cfg(initialized_library, backend, encrypt="on")
    adapter = adapter_for_config(cfg)
    await init_backend(cfg, adapter, "pw", kdf_params=WEAK_KDF)
    doc = json.loads((backend / "format.json").read_text())
    doc["kdf"]["salt"] = "!!!notb64!!!"
    (backend / "format.json").write_text(json.dumps(doc))

    with pytest.raises(SyncFormatError, match="kdf params unusable"):
        await verify_passphrase(cfg, adapter, "pw")


async def test_init_backend_refuses_orphan_sync_data(initialized_library,
                                                     tmp_path):
    """S5.5-fix minor #4: journal/snapshot data without a format.json is a
    tamper or partial copy — init refuses (mirrors _open_backend's guard)."""
    backend = tmp_path / "backend"
    cfg = _sync_cfg(initialized_library, backend, encrypt="off")
    adapter = adapter_for_config(cfg)
    (backend / "journal" / "somedevice").mkdir(parents=True)
    (backend / "journal" / "somedevice" / "000000000001.age").write_bytes(b"x")

    with pytest.raises(SyncConfigError, match="refusing to initialize"):
        await init_backend(cfg, adapter, "")

    assert not (backend / "format.json").exists()


def test_clear_shadow_preserves_alias_and_metats(initialized_library):
    """The epoch-reset surgical scope: alias rows (permanent uid mappings)
    and metats rows (per-field meta LWW clocks) survive; everything else —
    live entries AND tombstones — is wiped."""
    from tiro.database import get_connection
    from tiro.sync.manifest import clear_shadow

    cfg = initialized_library
    conn = get_connection(cfg.db_path)
    try:
        conn.execute(
            "INSERT INTO sync_shadow (kind, uid, hash, fields_json, hlc, "
            "deleted_at) VALUES ('alias', 'dead-uid', NULL, "
            "'{\"new_uid\": \"live-uid\"}', NULL, NULL)")
        conn.execute(
            "INSERT INTO sync_shadow (kind, uid, hash, fields_json, hlc, "
            "deleted_at) VALUES ('metats', 'art-uid:rating', NULL, '{}', "
            "'0000000000000001-0000-dev', NULL)")
        conn.execute(
            "INSERT INTO sync_shadow (kind, uid, hash, fields_json, hlc, "
            "deleted_at) VALUES ('article', 'art-uid', 'abc123', '{}', "
            "NULL, NULL)")
        conn.execute(
            "INSERT INTO sync_shadow (kind, uid, hash, fields_json, hlc, "
            "deleted_at) VALUES ('note', 'gone-uid', NULL, '{}', NULL, "
            "'2026-01-01T00:00:00Z')")
        conn.commit()
    finally:
        conn.close()

    clear_shadow(cfg)

    rows = _rows(cfg, "SELECT kind, uid FROM sync_shadow ORDER BY kind")
    assert [(r["kind"], r["uid"]) for r in rows] == [
        ("alias", "dead-uid"), ("metats", "art-uid:rating")]


async def test_bootstrap_restores_articles(initialized_library, tmp_path):
    from pathlib import Path

    from tests.test_reconcile import _ingest
    from tiro.annotations import write_note

    backend = tmp_path / "backend"
    cfg_a = _sync_cfg(initialized_library, backend, encrypt="on")
    adapter_a = adapter_for_config(cfg_a)
    recovery = await init_backend(cfg_a, adapter_a, "hunter2",
                                  kdf_params=WEAK_KDF)
    cfg_a.sync_identity = recovery

    art = _ingest(cfg_a, title="Synced Article",
                  body="# Hello\n\nBody from device A.")
    md_path = _rows(cfg_a, "SELECT markdown_path FROM articles WHERE id = ?",
                    (art["id"],))[0]["markdown_path"]
    stem = Path(md_path).stem
    write_note(cfg_a, stem, "My article note.\n")
    report_a = await sync_cycle(cfg_a, adapter_a)
    assert report_a.result == "ok"

    cfg_b = _second_library(tmp_path, "lib-b")
    _sync_cfg(cfg_b, backend, encrypt="on")
    adapter_b = adapter_for_config(cfg_b)
    identity = await verify_passphrase(cfg_b, adapter_b, "hunter2")
    assert identity == recovery
    cfg_b.sync_identity = identity

    report_b = await bootstrap(cfg_b, adapter_b)

    assert report_b.result == "ok"
    row = _rows(cfg_b, "SELECT title, vector_status FROM articles")[0]
    assert row["title"] == "Synced Article"
    assert row["vector_status"] in ("pending", "indexed")
    note_path = cfg_b.library / "notes" / f"{stem}.md"
    assert note_path.exists()
    assert note_path.read_text() == "My article note.\n"


async def test_bootstrap_refuses_non_empty_library(initialized_library,
                                                   tmp_path):
    from tests.test_reconcile import _ingest

    backend = tmp_path / "backend"
    cfg = _sync_cfg(initialized_library, backend, encrypt="off")
    _ingest(cfg)

    report = await bootstrap(cfg, adapter_for_config(cfg))

    assert report.result == "error"
    assert "empty" in report.reason
    assert _count(cfg) == 1  # nothing touched


async def test_bootstrap_all_or_nothing_on_missing_object(
    initialized_library, tmp_path
):
    """Backend lost an object the snapshot references: honest quarantine
    (needs_attention, S5.5-fix minor #1) and NO partial library — zero
    articles materialized."""
    from tests.test_reconcile import _ingest

    backend = tmp_path / "backend"
    cfg = _sync_cfg(initialized_library, backend, encrypt="off")
    adapter = adapter_for_config(cfg)
    assert await init_backend(cfg, adapter, "") == ""
    _ingest(cfg, title="One", url="https://example.com/1")
    _ingest(cfg, title="Two", url="https://example.com/2")
    assert (await sync_cycle(cfg, adapter)).result == "ok"
    objs = list(backend.glob("objects/*/*.age"))
    assert objs
    objs[0].unlink()  # backend "loses" one object

    cfg_b = _second_library(tmp_path, "lib-b")
    _sync_cfg(cfg_b, backend, encrypt="off")

    report = await bootstrap(cfg_b, adapter_for_config(cfg_b))

    assert report.result == "needs_attention"
    assert "missing object" in report.reason
    assert _count(cfg_b) == 0  # all-or-nothing: no partial library


async def test_bootstrap_skipped_when_backend_lock_held(initialized_library,
                                                        tmp_path):
    """S5.5-fix M1: bootstrap is conservative, never lockless — a held
    backend advisory lock refuses (skipped_lock), nothing materialized."""
    from tests.test_reconcile import _ingest

    backend = tmp_path / "backend"
    cfg_a = _sync_cfg(initialized_library, backend, encrypt="off")
    adapter_a = adapter_for_config(cfg_a)
    assert await init_backend(cfg_a, adapter_a, "") == ""
    _ingest(cfg_a, title="From A", url="https://example.com/a")
    assert (await sync_cycle(cfg_a, adapter_a)).result == "ok"

    cfg_b = _second_library(tmp_path, "lib-b")
    _sync_cfg(cfg_b, backend, encrypt="off")
    holder = adapter_for_config(cfg_a)
    assert await holder.lock(120) is True
    try:
        report = await bootstrap(cfg_b, adapter_for_config(cfg_b))
    finally:
        await holder.unlock()

    assert report.result == "skipped_lock"
    assert "backend lock held" in report.reason
    assert _count(cfg_b) == 0


async def test_bootstrap_skipped_when_cycle_running(initialized_library,
                                                    tmp_path):
    """S5.5-fix M1, in-process half: bootstrap and a running cycle never
    interleave in one process."""
    from tiro.sync.engine import _CYCLE_LOCK

    backend = tmp_path / "backend"
    cfg = _sync_cfg(initialized_library, backend, encrypt="off")
    adapter = adapter_for_config(cfg)
    assert await init_backend(cfg, adapter, "") == ""

    assert _CYCLE_LOCK.acquire(blocking=False)
    try:
        report = await bootstrap(cfg, adapter)
    finally:
        _CYCLE_LOCK.release()

    assert report.result == "skipped_lock"
    assert "another cycle" in report.reason


async def test_cycle_auto_bootstraps_empty_library(initialized_library,
                                                   tmp_path):
    """THE D-S5-3 pin: a plain sync_cycle on an empty never-synced library
    materializes the backend's snapshot instead of gap-refusing GC'd
    history, and the journal round-trip works afterwards."""
    from tests.test_reconcile import _ingest

    backend = tmp_path / "backend"
    cfg_a = _sync_cfg(initialized_library, backend, encrypt="on")
    adapter_a = adapter_for_config(cfg_a)
    recovery = await init_backend(cfg_a, adapter_a, "pw", kdf_params=WEAK_KDF)
    cfg_a.sync_identity = recovery
    _ingest(cfg_a, title="From A", url="https://example.com/a")
    assert (await sync_cycle(cfg_a, adapter_a)).result == "ok"
    # First-cycle compaction GC'd the fully-acked segment: snapshot only —
    # a naive pull would find covers it can never journal-replay.
    assert list(backend.glob("journal/**/*.age")) == []
    assert len(list(backend.glob("snapshots/*/manifest.age"))) == 1

    cfg_b = _second_library(tmp_path, "lib-b")
    _sync_cfg(cfg_b, backend, encrypt="on", identity=recovery)

    report_b = await sync_cycle(cfg_b)

    assert report_b.result == "ok"
    assert report_b.applied > 0
    assert _titles(cfg_b) == {"From A"}

    # Round-trip through the journal post-bootstrap: B's own article
    # reaches A on A's next cycle.
    _ingest(cfg_b, title="From B", url="https://example.com/b")
    assert (await sync_cycle(cfg_b)).result == "ok"
    report_a2 = await sync_cycle(cfg_a, adapter_a)
    assert report_a2.result == "ok"
    assert _titles(cfg_a) == {"From A", "From B"}


async def test_zero_article_synced_device_not_resurrected(initialized_library,
                                                          tmp_path):
    """S5.5-fix minor #2: a single device (watermarks always {}) that has
    PUSHED before (last_seq > 0) and then deletes every article is a
    legitimate zero-article steady state — auto-bootstrap must NOT
    re-materialize the snapshot on every subsequent cycle."""
    from tests.test_reconcile import _ingest
    from tiro.lifecycle import delete_article

    backend = tmp_path / "backend"
    cfg = _sync_cfg(initialized_library, backend, encrypt="off")
    adapter = adapter_for_config(cfg)
    assert await init_backend(cfg, adapter, "") == ""
    a1 = _ingest(cfg, title="One", url="https://example.com/1")
    a2 = _ingest(cfg, title="Two", url="https://example.com/2")
    assert (await sync_cycle(cfg, adapter)).result == "ok"
    # Premise: first-cycle compaction left a snapshot on the backend.
    assert list(backend.glob("snapshots/*/manifest.age"))

    delete_article(cfg, a1["id"])
    delete_article(cfg, a2["id"])
    assert _count(cfg) == 0

    report2 = await sync_cycle(cfg, adapter)
    assert report2.result == "ok"
    assert report2.applied == 0  # nothing re-downloaded, nothing applied
    assert _count(cfg) == 0

    report3 = await sync_cycle(cfg, adapter)
    assert report3.result == "ok"
    assert report3.applied == 0
    assert _count(cfg) == 0


async def test_repair_wipes_cloud_keeps_format(initialized_library,
                                               tmp_path):
    from tests.test_reconcile import _ingest
    from tiro.sync.snapshot import device_key, parse_device_doc

    backend = tmp_path / "backend"
    cfg = _sync_cfg(initialized_library, backend, encrypt="on")
    adapter = adapter_for_config(cfg)
    recovery = await init_backend(cfg, adapter, "pw", kdf_params=WEAK_KDF)
    cfg.sync_identity = recovery
    _ingest(cfg)
    assert (await sync_cycle(cfg, adapter)).result == "ok"
    fmt_before = (backend / "format.json").read_bytes()
    snaps_before = {p.as_posix()
                    for p in backend.glob("snapshots/*/manifest.age")}

    report = await repair(cfg, adapter)

    assert report.result == "ok"
    assert report.pushed_objects > 0
    # format.json KEPT byte-identical — other devices keep decrypting.
    assert (backend / "format.json").read_bytes() == fmt_before
    assert list(backend.glob("journal/**/*.age")) == []
    snaps = {p.as_posix() for p in backend.glob("snapshots/*/manifest.age")}
    assert len(snaps) == 1
    assert snaps != snaps_before  # a FRESH snapshot, not the old one
    device_id, _name = get_or_create_device(cfg)
    doc = parse_device_doc(
        device_id, (backend / device_key(device_id)).read_text())
    assert doc.last_seq == 0
    assert read_sync_state(cfg)["self"]["last_seq"] == 0

    # Nothing pending after repair: the next cycle pushes no ops.
    report2 = await sync_cycle(cfg, adapter)
    assert report2.result == "ok"
    assert report2.pushed_ops == 0


async def test_repair_epoch_detection_on_other_device(initialized_library,
                                                      tmp_path):
    from tests.test_reconcile import _ingest
    from tiro.sync.snapshot import journal_key

    backend = tmp_path / "backend"
    cfg_a = _sync_cfg(initialized_library, backend, encrypt="on")
    adapter_a = adapter_for_config(cfg_a)
    recovery = await init_backend(cfg_a, adapter_a, "pw", kdf_params=WEAK_KDF)
    cfg_a.sync_identity = recovery
    _ingest(cfg_a, title="From A", url="https://example.com/a")
    assert (await sync_cycle(cfg_a, adapter_a)).result == "ok"

    cfg_b = _second_library(tmp_path, "lib-b")
    _sync_cfg(cfg_b, backend, encrypt="on", identity=recovery)
    assert (await sync_cycle(cfg_b)).result == "ok"  # auto-bootstraps
    _ingest(cfg_b, title="From B", url="https://example.com/b")
    assert (await sync_cycle(cfg_b)).result == "ok"  # pushes B seq 1
    count_before = _count(cfg_b)
    assert count_before == 2

    assert (await repair(cfg_a, adapter_a)).result == "ok"

    report = await sync_cycle(cfg_b)

    assert report.result == "ok"
    assert any("repair epoch" in w for w in report.warnings)
    assert report.pushed_ops > 0  # full re-push after the shadow reset
    device_b, _name = get_or_create_device(cfg_b)
    assert (backend / journal_key(device_b, 1)).exists()
    # LOCAL DATA UNTOUCHED across the epoch.
    assert _count(cfg_b) == count_before
    assert _titles(cfg_b) == {"From A", "From B"}

    # Next cycle is idempotent — the reset re-push happened exactly once.
    report2 = await sync_cycle(cfg_b)
    assert report2.result == "ok"
    assert report2.pushed_ops == 0


async def test_repair_skipped_when_lock_held(initialized_library, tmp_path):
    from tests.test_reconcile import _ingest

    backend = tmp_path / "backend"
    cfg = _sync_cfg(initialized_library, backend, encrypt="off")
    adapter = adapter_for_config(cfg)
    assert await init_backend(cfg, adapter, "") == ""
    _ingest(cfg)
    assert (await sync_cycle(cfg, adapter)).result == "ok"
    # A second article's segment survives compaction cadence — real journal
    # content the refused repair must leave untouched.
    _ingest(cfg, title="Second", url="https://example.com/second")
    assert (await sync_cycle(cfg, adapter)).result == "ok"
    segs_before = sorted(p.as_posix()
                         for p in backend.glob("journal/**/*.age"))
    assert segs_before

    holder = adapter_for_config(cfg)
    assert await holder.lock(120) is True
    try:
        report = await repair(cfg, adapter)
    finally:
        await holder.unlock()

    assert report.result == "skipped_lock"
    assert sorted(p.as_posix()
                  for p in backend.glob("journal/**/*.age")) == segs_before
    assert len(list(backend.glob("snapshots/*/manifest.age"))) == 1


async def test_repair_aborts_before_wipe_on_underivable_state(
    initialized_library, tmp_path, monkeypatch
):
    """THE M2 pin (S5.5-fix): repair derives BEFORE it destroys — an
    underivable local state (hashless/unreadable entry -> SnapshotError)
    aborts with the backend fully intact: journal, snapshots, objects and
    device docs all still present."""
    import tiro.sync.snapshot as snapshot_mod
    from tests.test_reconcile import _ingest

    backend = tmp_path / "backend"
    cfg = _sync_cfg(initialized_library, backend, encrypt="off")
    adapter = adapter_for_config(cfg)
    assert await init_backend(cfg, adapter, "") == ""
    _ingest(cfg)
    assert (await sync_cycle(cfg, adapter)).result == "ok"
    _ingest(cfg, title="Second", url="https://example.com/second")
    assert (await sync_cycle(cfg, adapter)).result == "ok"

    def _backend_files():
        return sorted(p.as_posix() for p in backend.rglob("*")
                      if p.is_file() and "lock" not in p.name)

    files_before = _backend_files()
    assert any("journal/" in f for f in files_before)
    assert any("snapshots/" in f for f in files_before)
    assert any("objects/" in f for f in files_before)
    assert any("devices/" in f for f in files_before)

    def _boom(*args, **kwargs):
        raise snapshot_mod.SnapshotError(
            "no hash for entry ('article', 'broken')")

    monkeypatch.setattr(snapshot_mod, "build_snapshot", _boom)

    report = await repair(cfg, adapter)

    assert report.result == "needs_attention"
    assert "no hash" in report.reason
    # The backend was never touched: every pre-repair file still present.
    assert _backend_files() == files_before


async def test_repair_epoch_detection_pull_only_device(initialized_library,
                                                       tmp_path):
    """Whole-branch review Major #1: a freshly-bootstrapped PULL-ONLY device
    (last_seq == 0, non-empty watermarks — the normal post-bootstrap state)
    must still epoch-detect after a repair elsewhere. Pre-fix, the
    last_seq-only gate skipped detection, the new epoch's low-seq segments
    sat below the stale watermarks and were silently skipped, and the
    device's heartbeat re-published the stale watermarks as acked —
    licensing GC to cement the divergence."""
    from tests.test_reconcile import _ingest
    from tiro.sync.engine import read_sync_state

    backend = tmp_path / "backend"
    cfg_a = _sync_cfg(initialized_library, backend, encrypt="on")
    adapter_a = adapter_for_config(cfg_a)
    recovery = await init_backend(cfg_a, adapter_a, "pw", kdf_params=WEAK_KDF)
    cfg_a.sync_identity = recovery
    _ingest(cfg_a, title="First", url="https://example.com/first")
    assert (await sync_cycle(cfg_a, adapter_a)).result == "ok"
    # A pushes a SECOND article so B accrues a real watermark for A beyond
    # the snapshot covers (segment 2 survives compaction cadence).
    _ingest(cfg_a, title="Second", url="https://example.com/second")
    assert (await sync_cycle(cfg_a, adapter_a)).result == "ok"

    cfg_b = _second_library(tmp_path, "lib-b")
    _sync_cfg(cfg_b, backend, encrypt="on", identity=recovery)
    assert (await sync_cycle(cfg_b)).result == "ok"  # auto-bootstrap + pull
    # Construct the pull-only state explicitly: the normal bootstrap cycle
    # pushes the documented one-cycle meta echo (last_seq 1), but a failed
    # first push / echo-free bootstrap leaves last_seq 0 WITH watermarks --
    # the exact state whose epoch detection the old last_seq-only gate
    # skipped. Forcing it keeps the pin honest without depending on echo
    # mechanics.
    from tiro.sync.engine import update_self_state
    update_self_state(cfg_b, last_seq=0)
    state_b = read_sync_state(cfg_b)
    assert (state_b["self"]["last_seq"] or 0) == 0
    assert state_b["watermarks"], "precondition: B holds watermarks for A"
    assert _titles(cfg_b) == {"First", "Second"}

    assert (await repair(cfg_a, adapter_a)).result == "ok"

    report = await sync_cycle(cfg_b)
    assert report.result == "ok"
    assert any("repair epoch" in w for w in report.warnings)

    # Post-epoch convergence proof: A pushes NEW content at low seq numbers
    # (post-repair journal restarts at 1); B must pick it up, not silently
    # skip it below stale watermarks.
    _ingest(cfg_a, title="Post Repair", url="https://example.com/post")
    assert (await sync_cycle(cfg_a, adapter_a)).result == "ok"
    assert (await sync_cycle(cfg_b)).result == "ok"
    assert "Post Repair" in _titles(cfg_b)
    assert _titles(cfg_b) == {"First", "Second", "Post Repair"}
