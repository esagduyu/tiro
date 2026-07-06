"""M5: comment-preserving atomic config persistence + settings routes."""

from pathlib import Path

import pytest

from tiro.config import TiroConfig, load_config, persist_config


def _write_cfg(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "# Tiro configuration\n"
        'library_path: "./lib"  # keep me\n'
        "port: 8000\n"
    )
    return cfg_file


def test_persist_config_preserves_comments_and_merges(tmp_path):
    cfg_file = _write_cfg(tmp_path)
    cfg = load_config(cfg_file)
    persist_config(cfg, {"tts_voice": "fable", "port": 9000})
    text = cfg_file.read_text()
    assert "# Tiro configuration" in text
    assert "# keep me" in text
    assert "tts_voice: fable" in text
    assert "port: 9000" in text


def test_persist_config_atomic_and_0600(tmp_path):
    cfg_file = _write_cfg(tmp_path)
    cfg = load_config(cfg_file)
    persist_config(cfg, {"digest_email": "a@b.c"})
    assert (cfg_file.stat().st_mode & 0o777) == 0o600
    assert not cfg_file.with_suffix(".yaml.tmp").exists()


def test_persist_config_requires_config_path(tmp_path):
    cfg = TiroConfig(library_path=str(tmp_path / "lib"))  # config_path is None
    with pytest.raises(ValueError):
        persist_config(cfg, {"port": 9000})


def test_persist_config_creates_missing_file(tmp_path):
    cfg = TiroConfig(library_path=str(tmp_path / "lib"))
    cfg.config_path = str(tmp_path / "new-config.yaml")
    persist_config(cfg, {"theme_light": "papyrus"})
    reloaded = load_config(tmp_path / "new-config.yaml")
    assert reloaded.theme_light == "papyrus"


def _cfg_text(configured_library):
    return Path(configured_library.config_path).read_text()


def test_email_settings_persist_to_config_path(authenticated_client, configured_library):
    r = authenticated_client.post("/api/settings/email", json={
        "enable_send": True, "enable_receive": False,
        "gmail_address": "u@gmail.com", "app_password": "xxxx yyyy zzzz aaaa",
    })
    assert r.status_code == 200, r.text
    text = _cfg_text(configured_library)
    assert "u@gmail.com" in text
    assert configured_library.smtp_user == "u@gmail.com"


def test_tts_settings_persist_to_config_path(authenticated_client, configured_library):
    r = authenticated_client.post("/api/settings/tts", json={
        "openai_api_key": "sk-test", "tts_voice": "fable", "tts_model": "tts-1",
    })
    assert r.status_code == 200, r.text
    assert "fable" in _cfg_text(configured_library)
    assert configured_library.tts_voice == "fable"


def test_appearance_settings_persist_to_config_path(authenticated_client, configured_library):
    r = authenticated_client.post("/api/settings/appearance", json={
        "theme_light": "papyrus", "theme_dark": "roman-night", "inbox_page_size": 25,
    })
    assert r.status_code == 200, r.text
    assert "inbox_page_size: 25" in _cfg_text(configured_library)


def test_appearance_settings_rejects_unknown_theme_name(authenticated_client, configured_library):
    r = authenticated_client.post("/api/settings/appearance", json={
        "theme_light": "../evil",
    })
    assert r.status_code == 400, r.text


def test_appearance_settings_accepts_builtin_theme_name(authenticated_client, configured_library):
    r = authenticated_client.post("/api/settings/appearance", json={
        "theme_dark": "roman-night",
    })
    assert r.status_code == 200, r.text
    assert configured_library.theme_dark == "roman-night"


def test_appearance_settings_accepts_custom_theme_name(authenticated_client, configured_library):
    (configured_library.library / "themes").mkdir(parents=True, exist_ok=True)
    (configured_library.library / "themes" / "custom-x.css").write_text(":root{}")

    r = authenticated_client.post("/api/settings/appearance", json={
        "theme_light": "custom-x",
    })
    assert r.status_code == 200, r.text
    assert configured_library.theme_light == "custom-x"


def test_telemetry_settings_default_disabled(authenticated_client):
    r = authenticated_client.get("/api/settings/telemetry")
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"enabled": False}


def test_telemetry_settings_round_trip_persists_to_config_path(authenticated_client, configured_library):
    r = authenticated_client.post("/api/settings/telemetry", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"enabled": True}

    # Live config updated immediately.
    assert configured_library.reading_telemetry_enabled is True

    # And actually written to the config file, not just the in-memory object.
    assert "reading_telemetry_enabled: true" in _cfg_text(configured_library)
    from tiro.config import load_config
    reloaded = load_config(configured_library.config_path)
    assert reloaded.reading_telemetry_enabled is True

    r = authenticated_client.get("/api/settings/telemetry")
    assert r.json()["data"] == {"enabled": True}

    r = authenticated_client.post("/api/settings/telemetry", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert configured_library.reading_telemetry_enabled is False
    assert "reading_telemetry_enabled: false" in _cfg_text(configured_library)


def test_telemetry_settings_requires_auth(auth_client):
    r = auth_client.get("/api/settings/telemetry")
    assert r.status_code == 401
    r = auth_client.post("/api/settings/telemetry", json={"enabled": True})
    assert r.status_code == 401


def test_settings_page_has_telemetry_card(authenticated_client):
    """Pin for the M2.3 Task 2 "Reading Telemetry" settings card: status
    display + toggle button wired to GET/POST /api/settings/telemetry, with
    copy stating the local-only/opt-in posture."""
    r = authenticated_client.get("/settings")
    assert r.status_code == 200
    assert "Reading Telemetry" in r.text
    assert 'id="telemetry-status"' in r.text
    assert 'id="btn-toggle-telemetry"' in r.text
    assert "never leaves your machine" in r.text
    assert "Off by default" in r.text


def test_digest_schedule_persists_to_config_path(authenticated_client, configured_library):
    r = authenticated_client.post("/api/settings/digest-schedule", json={
        "enabled": False, "time": "08:30", "unread_only": True, "timezone_offset": -60,
    })
    assert r.status_code == 200, r.text
    assert "08:30" in _cfg_text(configured_library)


def test_email_settings_restart_imap_task(authenticated_client, configured_library):
    app = authenticated_client.app
    assert getattr(app.state, "imap_task", None) is None  # disabled at startup

    r = authenticated_client.post("/api/settings/email", json={
        "enable_send": False, "enable_receive": True,
        "gmail_address": "u@gmail.com", "app_password": "xxxx yyyy zzzz aaaa",
        "imap_label": "tiro", "imap_sync_interval": 15,
    })
    assert r.status_code == 200, r.text
    task = app.state.imap_task
    assert task is not None and not task.done()

    r = authenticated_client.post("/api/settings/email", json={
        "enable_send": False, "enable_receive": False,
        "gmail_address": "u@gmail.com", "app_password": "xxxx yyyy zzzz aaaa",
    })
    assert r.status_code == 200, r.text
    assert app.state.imap_task is None
    assert "imap_enabled: false" in _cfg_text(configured_library)


def test_email_settings_get_distinguishes_configured_from_enabled(authenticated_client, configured_library):
    """GET /api/settings/email must report imap_enabled separately from
    imap_configured (credential presence) so the UI can tell "receive is
    configured but disabled" apart from "receive is on"."""
    r = authenticated_client.post("/api/settings/email", json={
        "enable_send": False, "enable_receive": True,
        "gmail_address": "u@gmail.com", "app_password": "xxxx yyyy zzzz aaaa",
        "imap_label": "tiro", "imap_sync_interval": 15,
    })
    assert r.status_code == 200, r.text

    r = authenticated_client.get("/api/settings/email")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["imap_configured"] is True
    assert data["imap_enabled"] is True

    # Disable receive while keeping credentials (the "disable round-trip" case)
    r = authenticated_client.post("/api/settings/email", json={
        "enable_send": False, "enable_receive": False,
        "gmail_address": "u@gmail.com", "app_password": "xxxx yyyy zzzz aaaa",
    })
    assert r.status_code == 200, r.text

    r = authenticated_client.get("/api/settings/email")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["imap_configured"] is True  # creds kept
    assert data["imap_enabled"] is False  # but disabled


def test_no_cwd_relative_config_writes_left():
    import subprocess

    # Anchor to the repo root explicitly: the autouse _isolate_cwd fixture
    # chdirs the process into a per-test tmp dir, so a bare relative "tiro/"
    # here would silently resolve to a nonexistent directory and grep would
    # report no hits regardless of the real source tree.
    repo_root = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        ["grep", "-rn", 'Path("config.yaml")', "tiro/"],
        capture_output=True, text=True, cwd=repo_root,
    )
    assert out.stdout == "", f"CWD-relative config writes remain:\n{out.stdout}"


def test_raw_secrets_never_in_settings_responses(authenticated_client, configured_library):
    configured_library.smtp_password = "SUPER-secret-smtp"
    configured_library.imap_password = "SUPER-secret-imap"
    configured_library.openai_api_key = "sk-SUPER-secret-openai"

    for path in ("/api/settings/email", "/api/settings/tts"):
        r = authenticated_client.get(path)
        assert r.status_code == 200
        assert "SUPER-secret" not in r.text, f"raw secret leaked from {path}"


def test_cli_init_key_writes_preserve_comments_and_0600(tmp_path, monkeypatch):
    """cmd_init's API-key save must go through persist_config (atomic, 0600,
    comment-preserving) — it writes secrets."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("# my precious comments\nlibrary_path: ./lib\n")

    from tiro.config import load_config, persist_config

    cfg = load_config(cfg_file)
    persist_config(cfg, {"anthropic_api_key": "sk-ant-test"})
    text = cfg_file.read_text()
    assert "# my precious comments" in text
    assert "sk-ant-test" in text
    assert (cfg_file.stat().st_mode & 0o777) == 0o600


def test_no_naive_yaml_config_writes_left():
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        ["grep", "-rn", "write_text(yaml.dump", "tiro/"],
        capture_output=True, text=True, cwd=repo_root,
    )
    assert out.stdout == "", f"naive YAML config writes remain:\n{out.stdout}"


def test_mask_never_reveals_secret_characters():
    from tiro.api.routes_settings import _mask_password

    assert _mask_password(None) is None
    masked = _mask_password("abcd efgh ijkl mnop")
    assert masked == "********"
    for ch in "abcdefghijklmnop":
        assert ch not in masked
    assert _mask_password("xy") == "********"  # length not leaked either


def test_disable_email_requires_no_credentials(authenticated_client, configured_library):
    configured_library.smtp_user = "u@gmail.com"
    configured_library.smtp_password = "stored-app-password"
    configured_library.imap_user = "u@gmail.com"
    configured_library.imap_password = "stored-app-password"
    configured_library.imap_enabled = True

    r = authenticated_client.post("/api/settings/email", json={
        "enable_send": False, "enable_receive": False,
    })
    assert r.status_code == 200, r.text
    assert configured_library.imap_enabled is False


def test_enable_reuses_stored_password_when_not_resent(authenticated_client, configured_library):
    configured_library.smtp_user = "u@gmail.com"
    configured_library.smtp_password = "stored-app-password"

    r = authenticated_client.post("/api/settings/email", json={
        "enable_send": True, "enable_receive": False, "gmail_address": "u@gmail.com",
    })
    assert r.status_code == 200, r.text
    assert configured_library.smtp_password == "stored-app-password"


def test_enable_without_any_password_still_400(authenticated_client, configured_library):
    r = authenticated_client.post("/api/settings/email", json={
        "enable_send": True, "enable_receive": False, "gmail_address": "u@gmail.com",
    })
    assert r.status_code == 400
