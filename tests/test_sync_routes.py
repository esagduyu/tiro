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
