"""Sync S5.7: the FROZEN sync routes — GET/POST /api/settings/sync (masked
secrets, typed UNENCRYPTED confirm, yaml_quote round-trip pin, dynamic
scheduler restart), POST /api/sync/now (409 while a cycle runs), and
POST /api/sync/repair (typed REPAIR confirm).

Route auth is covered by test_auth.py's route-walk automatically (the sync
router is in create_app's protected list) — no allowlist entries here.
"""

from pathlib import Path

import yaml

MASK = "********"


def _fs(config, tmp_path):
    """Point config at a filesystem backend under tmp_path."""
    backend = tmp_path / "backend"
    config.sync_backend = "filesystem"
    config.sync_path = str(backend)
    return backend


def _s3_fields(config):
    config.sync_backend = "s3"
    config.sync_s3_endpoint = "https://s3.example"
    config.sync_s3_bucket = "bkt"
    config.sync_s3_access_key = "AKIARAWACCESS"
    config.sync_s3_secret_key = "raw-secret-material"


# --- GET /api/settings/sync --------------------------------------------------


def test_get_sync_settings_unconfigured(authenticated_client):
    r = authenticated_client.get("/api/settings/sync")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["configured"] is False
    assert data["enabled"] is False
    assert data["interval_s"] == 300
    assert data["identity_masked"] is None
    assert data["dot"] == "ok"
    assert data["last_cycle"] is None
    assert data["last_synced_at"] is None


def test_get_sync_settings_masks_secrets(authenticated_client,
                                         configured_library):
    cfg = configured_library
    _s3_fields(cfg)
    cfg.sync_identity = "AGE-SECRET-KEY-1SUPERSECRETX"

    r = authenticated_client.get("/api/settings/sync")
    assert r.status_code == 200
    # Raw secret material must appear NOWHERE in the response.
    assert "raw-secret-material" not in r.text
    assert "AKIARAWACCESS" not in r.text
    assert "AGE-SECRET-KEY-1SUPERSECRETX" not in r.text
    data = r.json()["data"]
    assert data["configured"] is True
    assert data["s3_access_key_masked"] == MASK
    assert data["s3_secret_key_masked"] == MASK
    assert data["identity_masked"] == MASK
    assert data["s3_bucket"] == "bkt"  # non-secret fields echo raw


def test_non_dict_last_cycle_json_degrades_to_none(authenticated_client,
                                                   configured_library):
    """Review M2: last_cycle_json that parses to a non-dict ('"boom"',
    '[1]') must degrade to None (mirroring the watermarks type guard) —
    status consumers call .get() on it and must never crash."""
    from tiro.database import get_connection
    from tiro.sync.engine import get_or_create_device, read_sync_state

    cfg = configured_library
    get_or_create_device(cfg)
    for garbage in ('"boom"', "[1]"):
        conn = get_connection(cfg.db_path)
        try:
            conn.execute(
                "UPDATE sync_state SET last_cycle_json = ? WHERE is_self = 1",
                (garbage,))
            conn.commit()
        finally:
            conn.close()
        assert read_sync_state(cfg)["last_cycle"] is None
        r = authenticated_client.get("/api/settings/sync")
        assert r.status_code == 200
        assert r.json()["data"]["last_cycle"] is None


# --- POST /api/settings/sync: validation -------------------------------------


def test_post_sync_settings_validates_inputs(authenticated_client):
    c = authenticated_client
    r = c.post("/api/settings/sync", json={"backend": "carrier-pigeon"})
    assert r.status_code == 400
    r = c.post("/api/settings/sync", json={"encrypt": "sometimes"})
    assert r.status_code == 400
    r = c.post("/api/settings/sync", json={"interval_s": -5})
    assert r.status_code == 400


def test_post_enable_without_required_fields_names_them(authenticated_client):
    r = authenticated_client.post("/api/settings/sync", json={"enabled": True})
    assert r.status_code == 400
    assert "sync_path" in r.json()["detail"]


def test_post_enable_filesystem_persists(authenticated_client,
                                         configured_library, tmp_path):
    backend = tmp_path / "backend"
    r = authenticated_client.post("/api/settings/sync", json={
        "enabled": True, "backend": "filesystem",
        "path": str(backend), "interval_s": 120,
    })
    assert r.status_code == 200
    assert r.json()["data"] == {"enabled": True, "interval_s": 120}
    cfg = configured_library
    assert cfg.sync_enabled is True
    assert cfg.sync_path == str(backend)
    assert cfg.sync_interval_s == 120
    persisted = yaml.safe_load(Path(cfg.config_path).read_text())
    assert persisted["sync_enabled"] is True
    assert persisted["sync_interval_s"] == 120
    assert persisted["sync_path"] == str(backend)


def test_post_encrypt_off_on_network_needs_typed_confirm(
        authenticated_client, configured_library):
    from tiro.config import load_config

    cfg = configured_library
    _s3_fields(cfg)  # sync_encrypt stays "auto" -> currently resolves ON

    r = authenticated_client.post("/api/settings/sync",
                                  json={"encrypt": "off"})
    assert r.status_code == 400
    assert "UNENCRYPTED" in r.json()["detail"]
    assert cfg.sync_encrypt == "auto"  # nothing applied

    r = authenticated_client.post("/api/settings/sync", json={
        "encrypt": "off", "confirm_unencrypted": "UNENCRYPTED"})
    assert r.status_code == 200
    assert cfg.sync_encrypt == "off"
    # THE yaml_quote PIN: the persisted value must round-trip through
    # pyyaml (YAML 1.1) as the STRING "off", never the boolean False.
    assert load_config(cfg.config_path).sync_encrypt == "off"


def test_single_post_to_plaintext_network_needs_typed_confirm(
        authenticated_client, configured_library):
    """Review M1: the ceremony guards the END STATE (plaintext on a network
    backend), not just the ON->off downgrade — one POST from the default
    filesystem/auto config straight to {backend: s3, encrypt: off} must not
    bypass it."""
    from tiro.config import load_config

    cfg = configured_library  # defaults: backend filesystem, encrypt auto
    assert cfg.sync_backend == "filesystem"
    body = {
        "backend": "s3", "encrypt": "off",
        "s3_endpoint": "https://s3.example", "s3_bucket": "bkt",
        "s3_access_key": "ak", "s3_secret_key": "sk",
    }
    r = authenticated_client.post("/api/settings/sync", json=body)
    assert r.status_code == 400
    assert "UNENCRYPTED" in r.json()["detail"]
    assert cfg.sync_backend == "filesystem"  # nothing applied
    assert cfg.sync_encrypt == "auto"

    r = authenticated_client.post("/api/settings/sync", json={
        **body, "confirm_unencrypted": "UNENCRYPTED"})
    assert r.status_code == 200
    assert cfg.sync_backend == "s3"
    assert cfg.sync_encrypt == "off"
    # Persisted as the STRING "off" (yaml_quote pin).
    assert load_config(cfg.config_path).sync_encrypt == "off"

    # Idempotent re-save of the already-plaintext network config stays
    # ceremony-free (the end state is unchanged).
    r = authenticated_client.post("/api/settings/sync", json={
        "encrypt": "off", "s3_bucket": "bkt2"})
    assert r.status_code == 200
    assert cfg.sync_s3_bucket == "bkt2"


def test_post_quotes_yaml_bool_lookalike_values(authenticated_client,
                                                configured_library):
    """Review m5: ANY posted string pyyaml would re-read as a YAML 1.1
    boolean must round-trip as a string — an S3 bucket named "no"."""
    from tiro.config import load_config

    cfg = configured_library
    r = authenticated_client.post("/api/settings/sync", json={
        "backend": "s3", "s3_endpoint": "https://s3.example",
        "s3_bucket": "no", "s3_access_key": "ak", "s3_secret_key": "sk",
    })
    assert r.status_code == 200
    assert cfg.sync_s3_bucket == "no"
    persisted = load_config(cfg.config_path)
    assert persisted.sync_s3_bucket == "no"
    assert isinstance(persisted.sync_s3_bucket, str)


def test_post_mask_leaves_secret_unchanged(authenticated_client,
                                           configured_library):
    cfg = configured_library
    r = authenticated_client.post("/api/settings/sync", json={
        "backend": "s3", "s3_endpoint": "https://s3.example",
        "s3_bucket": "bkt", "s3_access_key": "ak",
        "s3_secret_key": "the-real-secret",
    })
    assert r.status_code == 200
    assert cfg.sync_s3_secret_key == "the-real-secret"

    # The UI posts the mask back on save — the stored secret must survive.
    r = authenticated_client.post("/api/settings/sync", json={
        "s3_secret_key": MASK, "s3_access_key": "", "s3_bucket": "bkt2"})
    assert r.status_code == 200
    assert cfg.sync_s3_secret_key == "the-real-secret"
    assert cfg.sync_s3_access_key == "ak"
    assert cfg.sync_s3_bucket == "bkt2"


def test_post_enable_encrypted_requires_identity(authenticated_client,
                                                 configured_library,
                                                 tmp_path):
    r = authenticated_client.post("/api/settings/sync", json={
        "enabled": True, "backend": "filesystem",
        "path": str(tmp_path / "backend"), "encrypt": "on",
    })
    assert r.status_code == 400
    assert "tiro sync setup" in r.json()["detail"]
    assert configured_library.sync_enabled is False


# --- POST /api/settings/sync: scheduler restart ------------------------------


def test_post_sync_settings_restarts_scheduler(authenticated_client,
                                               configured_library, tmp_path):
    app = authenticated_client.app
    assert getattr(app.state, "sync_task", None) is None  # off at startup

    r = authenticated_client.post("/api/settings/sync", json={
        "enabled": True, "backend": "filesystem",
        "path": str(tmp_path / "backend"), "interval_s": 300,
    })
    assert r.status_code == 200
    assert app.state.sync_task is not None
    assert "sync" in app.state.scheduler.periodic_status()

    r = authenticated_client.post("/api/settings/sync",
                                  json={"enabled": False})
    assert r.status_code == 200
    assert app.state.sync_task is None


# --- POST /api/sync/now ------------------------------------------------------


def test_sync_now_unconfigured_400(authenticated_client):
    r = authenticated_client.post("/api/sync/now", json={})
    assert r.status_code == 400
    assert "sync_path" in r.json()["detail"]


def test_sync_now_runs_cycle(authenticated_client, configured_library,
                             tmp_path):
    backend = _fs(configured_library, tmp_path)
    r = authenticated_client.post("/api/sync/now", json={})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["result"] == "ok"
    # The cycle really ran: the device registry doc landed on the backend.
    assert any((backend / "devices").iterdir())


def test_sync_now_409_while_cycle_running(authenticated_client,
                                          configured_library, tmp_path):
    from tiro.sync import engine

    _fs(configured_library, tmp_path)
    assert engine._CYCLE_LOCK.acquire(blocking=False)
    try:
        r = authenticated_client.post("/api/sync/now", json={})
    finally:
        engine._CYCLE_LOCK.release()
    assert r.status_code == 409
    assert r.json()["detail"] == "sync_running"


# --- POST /api/sync/repair ---------------------------------------------------


def test_sync_repair_requires_typed_confirm(authenticated_client,
                                            configured_library, tmp_path):
    backend = _fs(configured_library, tmp_path)
    r = authenticated_client.post("/api/sync/repair",
                                  json={"confirm": "yes"})
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "confirm_required"
    assert "REPAIR" in body["detail"]
    # Backend untouched — no snapshot was uploaded.
    assert not (backend / "snapshots").exists()


def test_sync_repair_confirmed_reseeds(authenticated_client,
                                       configured_library, tmp_path):
    from tests.test_reconcile import _ingest

    backend = _fs(configured_library, tmp_path)
    _ingest(configured_library)
    r = authenticated_client.post("/api/sync/now", json={})
    assert r.status_code == 200
    assert r.json()["data"]["result"] == "ok"
    # The first cycle pushes AND immediately compacts (first-snapshot rule),
    # so the journal segment is already GC'd — the snapshot is the state.
    # Compare FILE-backed snapshot ids: the filesystem adapter deletes keys,
    # not directories, so a wiped snapshot leaves an empty dir behind.
    def _snapshot_ids():
        return {p.parent.name
                for p in (backend / "snapshots").rglob("*") if p.is_file()}

    before = _snapshot_ids()
    assert before

    r = authenticated_client.post("/api/sync/repair",
                                  json={"confirm": "REPAIR"})
    assert r.status_code == 200
    assert r.json()["data"]["result"] == "ok"
    # Repair really wiped and re-seeded: a NEW snapshot epoch, no journal
    # files, and the pre-repair snapshot ids are gone.
    after = _snapshot_ids()
    assert after and not (after & before)
    assert not any(p.is_file() for p in (backend / "journal").rglob("*"))
    assert any(p.is_file() for p in (backend / "snapshots").rglob("*"))


def test_sync_repair_409_while_cycle_running(authenticated_client,
                                             configured_library, tmp_path):
    """Review m3(a): the repair route gets the same in-process pre-check
    as /now — never start a wipe-and-reseed while a cycle runs."""
    from tiro.sync import engine

    backend = _fs(configured_library, tmp_path)
    assert engine._CYCLE_LOCK.acquire(blocking=False)
    try:
        r = authenticated_client.post("/api/sync/repair",
                                      json={"confirm": "REPAIR"})
    finally:
        engine._CYCLE_LOCK.release()
    assert r.status_code == 409
    assert r.json()["detail"] == "sync_running"
    assert not (backend / "snapshots").exists()  # backend untouched


def test_repair_engine_skips_when_cycle_lock_held(configured_library,
                                                  tmp_path):
    """Review m3(b): engine.repair itself takes the non-blocking cycle
    lock like bootstrap — a raced-in call degrades to skipped_lock."""
    import asyncio

    from tiro.sync import engine

    cfg = configured_library
    _fs(cfg, tmp_path)

    async def _run():
        adapter = engine.adapter_for_config(cfg)
        try:
            return await engine.repair(cfg, adapter)
        finally:
            await adapter.aclose()

    assert engine._CYCLE_LOCK.acquire(blocking=False)
    try:
        report = asyncio.run(_run())
    finally:
        engine._CYCLE_LOCK.release()
    assert report.result == "skipped_lock"
    assert "in this process" in report.reason


# --- m4: cancelled cycles record nothing, release the lock -------------------


def test_cancelled_cycle_records_nothing_and_releases_lock(
        configured_library, tmp_path):
    """Review m4: a cancelled half-cycle must NOT clobber last_cycle with
    a meaningless report (the report's default result is "ok"!) — and the
    in-process lock must still release so the next cycle runs."""
    import asyncio

    from tiro.sync import engine

    cfg = configured_library
    _fs(cfg, tmp_path)

    async def _scenario():
        # Seed a real prior last_cycle to prove cancellation preserves it.
        first = await engine.sync_cycle(cfg)
        assert first.result == "ok"
        before = engine.read_sync_state(cfg)["last_cycle"]
        assert before is not None

        adapter = engine.adapter_for_config(cfg)
        started = asyncio.Event()

        async def blocking_lock(ttl_s):
            started.set()
            await asyncio.Event().wait()  # blocks until cancelled

        adapter.lock = blocking_lock
        try:
            task = asyncio.create_task(engine.sync_cycle(cfg, adapter))
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cycle survived cancellation")
        finally:
            await adapter.aclose()

        # Nothing recorded: the seeded last_cycle is byte-identical.
        assert engine.read_sync_state(cfg)["last_cycle"] == before
        # The in-process lock released: a follow-up cycle runs for real.
        assert not engine._CYCLE_LOCK.locked()
        second = await engine.sync_cycle(cfg)
        assert second.result == "ok"

    asyncio.run(_scenario())


# --- S5.8: settings card + sidebar status anchor (template surface) ----------


def test_settings_page_has_sync_section(authenticated_client):
    r = authenticated_client.get("/settings")
    assert r.status_code == 200
    assert 'id="sync-section"' in r.text
    assert "tiro sync setup" in r.text


def test_base_sidebar_has_sync_status_anchor(authenticated_client):
    r = authenticated_client.get("/inbox")
    assert r.status_code == 200
    assert 'id="sync-status"' in r.text
    # Rendered hidden until sidebar.js unhides on configured: true — the
    # `hidden` attribute sits on the same anchor tag as the id.
    anchor_tag = r.text.split('id="sync-status"')[1].split(">")[0]
    assert "hidden" in anchor_tag
