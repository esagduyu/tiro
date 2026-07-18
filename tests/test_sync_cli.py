"""Sync S5.6: `tiro sync` CLI — status (default, no network), --now,
setup (recovery-code ceremony), repair (typed confirmation).

Subprocess-free: cmd_sync is called directly with an Args stand-in carrying
`_config_override` (the same seam cmd_reconcile's tests use).
"""
import asyncio

import pytest

from tiro.cli import cmd_sync
from tiro.sync.engine import (
    adapter_for_config,
    get_or_create_device,
    init_backend,
    update_self_state,
)

WEAK_KDF = {"m": 8, "t": 1, "p": 1}   # honest Argon2id, test-speed


class Args:
    """argparse.Namespace stand-in for cmd_sync."""

    def __init__(self, config, *, now=False, status=False,
                 accept_mass_delete=False, sync_cmd=None):
        self._config_override = config
        self.config = "config.yaml"  # never read — the override wins
        self.now = now
        self.status = status
        self.accept_mass_delete = accept_mass_delete
        self.sync_cmd = sync_cmd


def _fs_backend(config, tmp_path, **overrides):
    config.sync_backend = "filesystem"
    config.sync_path = str(tmp_path / "backend")
    for key, value in overrides.items():
        setattr(config, key, value)
    return tmp_path / "backend"


def _write_config_yaml(config, tmp_path):
    """Give the fixture config a real config.yaml so persist_config has
    somewhere to write (mirrors test_remote_wizard's on-disk config)."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"library_path: {config.library_path}\n")
    config.config_path = str(config_path)
    return config_path


# --- status ------------------------------------------------------------------


def test_sync_status_unconfigured(initialized_library, capsys):
    cmd_sync(Args(initialized_library))
    out = capsys.readouterr().out
    assert "not configured" in out
    assert "tiro sync setup" in out


def test_sync_status_prints_last_cycle(initialized_library, tmp_path, capsys):
    cfg = initialized_library
    _fs_backend(cfg, tmp_path, sync_enabled=True)
    get_or_create_device(cfg)
    update_self_state(cfg, last_cycle={
        "result": "ok", "finished_at": "2026-07-17T00:00:00Z",
        "pushed_ops": 3, "applied": 0,
    })
    cmd_sync(Args(cfg))
    out = capsys.readouterr().out
    assert "ok" in out
    assert "3" in out
    assert "this device" in out


def test_sync_status_never_prints_identity(initialized_library, tmp_path,
                                           capsys):
    cfg = initialized_library
    _fs_backend(cfg, tmp_path, sync_enabled=True, sync_encrypt="on",
                sync_identity="AGE-SECRET-KEY-1SUPERSECRETX")
    get_or_create_device(cfg)
    cmd_sync(Args(cfg))
    out = capsys.readouterr().out
    assert "AGE-SECRET-KEY-1SUPERSECRETX" not in out


# --- --now -------------------------------------------------------------------


def test_sync_now_runs_cycle(initialized_library, tmp_path, capsys):
    cfg = initialized_library
    backend = _fs_backend(cfg, tmp_path)  # sync_encrypt auto = plaintext fs
    cmd_sync(Args(cfg, now=True))
    out = capsys.readouterr().out.lower()
    assert "ok" in out
    # The cycle really ran: the backend now holds this device's registry doc.
    assert list((backend / "devices").iterdir())


# --- repair ------------------------------------------------------------------


def test_sync_repair_requires_typed_confirm(initialized_library, tmp_path,
                                            capsys, monkeypatch):
    cfg = initialized_library
    backend = _fs_backend(cfg, tmp_path, sync_enabled=True)
    monkeypatch.setattr("builtins.input", lambda *a: "no thanks")
    cmd_sync(Args(cfg, sync_cmd="repair"))
    out = capsys.readouterr().out
    assert "Aborted" in out
    # Backend untouched — no snapshot was uploaded.
    assert not (backend / "snapshots").exists()


def test_sync_repair_confirmed_runs(initialized_library, tmp_path, capsys,
                                    monkeypatch):
    from tests.test_reconcile import _ingest

    cfg = initialized_library
    backend = _fs_backend(cfg, tmp_path, sync_enabled=True)
    _ingest(cfg)
    cmd_sync(Args(cfg, now=True))  # seed: pushes a journal segment
    assert any((backend / "journal").rglob("*"))
    capsys.readouterr()

    monkeypatch.setattr("builtins.input", lambda *a: "REPAIR")
    cmd_sync(Args(cfg, sync_cmd="repair"))
    out = capsys.readouterr().out.lower()
    assert "ok" in out
    # Journal empty (the adapter may keep empty per-device dirs), snapshot
    # present.
    journal = backend / "journal"
    assert not any(p for p in journal.rglob("*") if p.is_file())
    assert any(p for p in (backend / "snapshots").rglob("*") if p.is_file())


# --- setup -------------------------------------------------------------------


def test_sync_setup_filesystem_plaintext(initialized_library, tmp_path,
                                         capsys, monkeypatch):
    cfg = initialized_library
    config_path = _write_config_yaml(cfg, tmp_path)
    backend = tmp_path / "backend"
    # backend choice, path, "Encrypt blobs? [y/N]" -> default N. The empty
    # library + fresh backend means no bootstrap offer (fmt did not exist).
    answers = iter(["filesystem", str(backend), ""])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(
        "getpass.getpass",
        lambda *a: pytest.fail("plaintext setup must never prompt getpass"))

    cmd_sync(Args(cfg, sync_cmd="setup"))

    out = capsys.readouterr().out
    assert "Setup complete" in out
    text = config_path.read_text()
    assert "sync_enabled: true" in text
    assert str(backend) in text
    # The plaintext format.json was initialized on the backend.
    assert (backend / "format.json").exists()


def test_sync_setup_wrong_passphrase_changes_nothing(initialized_library,
                                                     tmp_path, capsys,
                                                     monkeypatch):
    cfg = initialized_library
    config_path = _write_config_yaml(cfg, tmp_path)
    backend = _fs_backend(cfg, tmp_path, sync_encrypt="on")
    # Pre-init an encrypted backend (weak Argon2id for test speed).
    adapter = adapter_for_config(cfg)
    try:
        asyncio.run(init_backend(cfg, adapter, "correct-horse",
                                 kdf_params=WEAK_KDF))
    finally:
        asyncio.run(adapter.aclose())
    cfg.sync_encrypt = "auto"  # setup re-collects it

    answers = iter(["filesystem", str(backend), "y"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda *a: "wrong-passphrase")

    cmd_sync(Args(cfg, sync_cmd="setup"))

    out = capsys.readouterr().out
    assert "Wrong passphrase" in out
    assert "Nothing was changed" in out
    text = config_path.read_text()
    assert "sync_enabled: true" not in text
    assert "sync_identity" not in text


def _weak_new_kdf_params(**_kw):
    """Stand-in for crypto.new_kdf_params: honest Argon2id at test speed,
    fresh random salt (init_backend resolves the name from tiro.sync.crypto
    at call time, so that is the patch point — not tiro.sync.engine)."""
    import base64
    import os

    from tiro.sync.crypto import KdfParams

    return KdfParams(
        salt_b64=base64.b64encode(os.urandom(16)).decode("ascii"),
        **WEAK_KDF)


def test_sync_setup_encrypted_init_ceremony(initialized_library, tmp_path,
                                            capsys, monkeypatch):
    """THE CEREMONY PIN: fresh backend + encrypt=y runs the REAL init —
    recovery code printed exactly once, identity + pin persisted, and the
    persisted config LOADS BACK as an encrypted, enabled sync config
    (a plain `sync_encrypt: on` would round-trip through pyyaml as the
    boolean True and poison resolve_encryption)."""
    from tiro.config import load_config
    from tiro.sync.crypto import parse_format_json

    cfg = initialized_library
    config_path = _write_config_yaml(cfg, tmp_path)
    backend = tmp_path / "backend"
    monkeypatch.setattr("tiro.sync.crypto.new_kdf_params",
                        _weak_new_kdf_params)
    answers = iter(["filesystem", str(backend), "y"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda *a: "correct-horse")

    cmd_sync(Args(cfg, sync_cmd="setup"))

    out = capsys.readouterr().out
    assert out.count("RECOVERY CODE") == 1
    codes = [line.strip() for line in out.splitlines()
             if line.strip().startswith("AGE-SECRET-KEY-1")]
    assert len(codes) == 1
    recovery = codes[0]
    assert "Setup complete" in out

    persisted = load_config(config_path)
    assert persisted.sync_enabled is True
    assert persisted.sync_encrypt == "on"
    assert persisted.sync_identity == recovery
    fmt = parse_format_json((backend / "format.json").read_text())
    assert fmt.encryption == "age"
    assert fmt.age_recipient.startswith("age1")


def test_sync_setup_passphrase_mismatch_changes_nothing(initialized_library,
                                                        tmp_path, capsys,
                                                        monkeypatch):
    cfg = initialized_library
    config_path = _write_config_yaml(cfg, tmp_path)
    backend = tmp_path / "backend"
    answers = iter(["filesystem", str(backend), "y"])
    passphrases = iter(["pw-one", "pw-two"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda *a: next(passphrases))

    cmd_sync(Args(cfg, sync_cmd="setup"))

    out = capsys.readouterr().out
    assert "do not match" in out
    assert "Nothing was changed" in out
    assert "sync_enabled: true" not in config_path.read_text()
    assert not (backend / "format.json").exists()


def _preinit_plaintext_backend(cfg, tmp_path):
    """Initialize a PLAINTEXT backend, then reset the pin so setup
    re-collects it (mirrors the wrong-passphrase test's recipe)."""
    backend = _fs_backend(cfg, tmp_path, sync_encrypt="off")
    adapter = adapter_for_config(cfg)
    try:
        asyncio.run(init_backend(cfg, adapter, ""))
    finally:
        asyncio.run(adapter.aclose())
    cfg.sync_encrypt = "auto"
    return backend


def test_sync_setup_plaintext_join_with_pin_on_requires_confirm(
    initialized_library, tmp_path, capsys, monkeypatch
):
    """THE M1 PIN: joining a PLAINTEXT backend while the local pin resolves
    ON must never persist a forever-quarantined config — typed UNENCRYPTED
    confirm flips the pin off, and NO passphrase is ever prompted (it would
    protect nothing)."""
    from tiro.config import load_config

    cfg = initialized_library
    config_path = _write_config_yaml(cfg, tmp_path)
    backend = _preinit_plaintext_backend(cfg, tmp_path)

    # backend, path, encrypt=y (pin ON), typed confirm, decline bootstrap.
    answers = iter(["filesystem", str(backend), "y", "UNENCRYPTED", "n"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(
        "getpass.getpass",
        lambda *a: pytest.fail("plaintext join must never prompt getpass"))

    cmd_sync(Args(cfg, sync_cmd="setup"))

    out = capsys.readouterr().out
    assert "UNENCRYPTED, but this device is set to encrypt" in out
    assert "Setup complete" in out
    persisted = load_config(config_path)
    assert persisted.sync_enabled is True
    assert persisted.sync_encrypt == "off"
    assert persisted.sync_identity == ""


def test_sync_setup_plaintext_join_refused_changes_nothing(
    initialized_library, tmp_path, capsys, monkeypatch
):
    cfg = initialized_library
    config_path = _write_config_yaml(cfg, tmp_path)
    backend = _preinit_plaintext_backend(cfg, tmp_path)

    answers = iter(["filesystem", str(backend), "y", "nah"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(
        "getpass.getpass",
        lambda *a: pytest.fail("plaintext join must never prompt getpass"))

    cmd_sync(Args(cfg, sync_cmd="setup"))

    out = capsys.readouterr().out
    assert "Aborted. Nothing was changed." in out
    text = config_path.read_text()
    assert "sync_enabled: true" not in text
    assert "sync_encrypt" not in text


def test_sync_requires_library(test_config, capsys):
    """THE M3 PIN: cmd_sync mirrors cmd_reconcile's preamble — no DB, no
    sync verb runs; exit 1 with the init hint."""
    with pytest.raises(SystemExit) as exc:
        cmd_sync(Args(test_config))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "tiro init" in out


def test_sync_setup_version_refusal_is_clean(initialized_library, tmp_path,
                                             capsys, monkeypatch):
    """A NEWER sync_format refuses with a printed message — no traceback
    (cmd_sync returns normally), nothing persisted, no passphrase prompt."""
    import json

    cfg = initialized_library
    config_path = _write_config_yaml(cfg, tmp_path)
    backend = tmp_path / "backend"
    backend.mkdir(parents=True)
    (backend / "format.json").write_text(json.dumps({
        "sync_format": 99,
        "library_id": "lib-from-the-future",
        "encryption": "none",
        "kdf": None,
        "age_recipient": None,
        "created_at": "2026-01-01T00:00:00Z",
    }))
    answers = iter(["filesystem", str(backend), "y"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(
        "getpass.getpass",
        lambda *a: pytest.fail("version refusal must precede any prompt"))

    cmd_sync(Args(cfg, sync_cmd="setup"))

    out = capsys.readouterr().out
    assert "sync_format 99" in out
    assert "Nothing was changed" in out
    assert "sync_enabled: true" not in config_path.read_text()
