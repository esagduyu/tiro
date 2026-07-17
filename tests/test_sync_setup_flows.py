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
