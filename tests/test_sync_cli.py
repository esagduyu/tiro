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
