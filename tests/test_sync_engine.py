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
