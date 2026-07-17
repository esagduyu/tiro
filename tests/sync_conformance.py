"""Shared storage-adapter conformance suite (sync S4).

NOT collected directly (no test_ prefix). Each adapter's test file
subclasses AdapterConformance and provides a `make_adapter` fixture:

    @pytest.fixture
    def make_adapter(self, ...):
        def make(device_id: str) -> StorageAdapter: ...
        return make

make() must return a FRESH adapter instance over the SAME backing store on
every call (lock-contention tests need two devices sharing one backend).
Per-test isolation on shared real backends (MinIO bucket, Nextcloud) is the
fixture's job: hand out a fresh random prefix/collection per test.
"""
import random

import pytest

from tiro.sync.adapters.base import LOCK_KEY, KeyMissing, make_lock_payload

# Keys shaped exactly like the spec §5 backend layout.
SPEC_KEYS = [
    "format.json",
    "devices/dev-a.json",
    "journal/dev-a/000000000001.age",
    "journal/dev-a/000000000002.age",
    "journal/dev-b/000000000001.age",
    "objects/aa/" + "a" * 64 + ".age",
    "snapshots/01J00000000000000000000000/manifest.age",
]


class AdapterConformance:
    @pytest.fixture
    async def adapter(self, make_adapter):
        a = make_adapter("dev-a")
        yield a
        await a.aclose()

    # -- put/get/delete ------------------------------------------------

    async def test_put_get_roundtrip_binary(self, adapter):
        data = b"\x00\x01\xff age blob \xf0\x9f\x93\x9a\n" * 3
        await adapter.put("objects/ab/deadbeef.age", data)
        assert await adapter.get("objects/ab/deadbeef.age") == data

    async def test_get_missing_raises_keymissing(self, adapter):
        with pytest.raises(KeyMissing):
            await adapter.get("objects/xx/absent.age")

    async def test_put_overwrites(self, adapter):
        await adapter.put("format.json", b"v1")
        await adapter.put("format.json", b"v2-longer-body")
        assert await adapter.get("format.json") == b"v2-longer-body"

    async def test_large_blob_roundtrip(self, adapter):
        data = random.Random(42).randbytes(2_000_000)  # ~2 MB snapshot-sized
        await adapter.put("snapshots/01J00000000000000000000000/manifest.age", data)
        got = await adapter.get("snapshots/01J00000000000000000000000/manifest.age")
        assert got == data

    async def test_spec_layout_roundtrip(self, adapter):
        for i, key in enumerate(SPEC_KEYS):
            await adapter.put(key, f"body-{i}".encode())
        for i, key in enumerate(SPEC_KEYS):
            assert await adapter.get(key) == f"body-{i}".encode()

    async def test_delete_then_get_missing(self, adapter):
        await adapter.put("devices/dev-a.json", b"{}")
        await adapter.delete("devices/dev-a.json")
        with pytest.raises(KeyMissing):
            await adapter.get("devices/dev-a.json")

    async def test_delete_missing_is_idempotent(self, adapter):
        await adapter.delete("devices/never-existed.json")  # must not raise

    # -- list ----------------------------------------------------------

    async def test_list_prefix_filters_full_keys_sorted(self, adapter):
        for key in SPEC_KEYS:
            await adapter.put(key, b"x")
        dev_a = await adapter.list("journal/dev-a/")
        assert dev_a == [
            "journal/dev-a/000000000001.age",
            "journal/dev-a/000000000002.age",
        ]
        journal = await adapter.list("journal/")
        assert journal == [
            "journal/dev-a/000000000001.age",
            "journal/dev-a/000000000002.age",
            "journal/dev-b/000000000001.age",
        ]
        # non-directory-aligned string prefix works too
        assert await adapter.list("jour") == journal
        # '' lists everything
        everything = await adapter.list("")
        assert set(SPEC_KEYS) <= set(everything)
        assert everything == sorted(everything)

    async def test_list_missing_prefix_empty(self, adapter):
        assert await adapter.list("journal/dev-zz/") == []

    # -- lock/unlock (spec §6.1) ----------------------------------------

    async def test_lock_acquire_then_contend(self, adapter, make_adapter):
        assert await adapter.lock(ttl_s=300) is True
        other = make_adapter("dev-other")
        try:
            assert await other.lock(ttl_s=300) is False
        finally:
            await other.aclose()

    async def test_unlock_releases_for_next_device(self, adapter, make_adapter):
        assert await adapter.lock(ttl_s=300) is True
        await adapter.unlock()
        other = make_adapter("dev-other")
        try:
            assert await other.lock(ttl_s=300) is True
            await other.unlock()
        finally:
            await other.aclose()

    async def test_expired_lock_is_stolen(self, adapter):
        from datetime import UTC, datetime

        stale = make_lock_payload(
            "dev-dead", 60, now=datetime(2020, 1, 1, tzinfo=UTC)
        )
        await adapter.put(LOCK_KEY, stale)
        assert await adapter.lock(ttl_s=300) is True

    async def test_garbage_lock_is_stolen(self, adapter):
        await adapter.put(LOCK_KEY, b"not json at all")
        assert await adapter.lock(ttl_s=300) is True

    async def test_unlock_when_not_held_is_noop(self, adapter, make_adapter):
        other = make_adapter("dev-other")
        try:
            assert await other.lock(ttl_s=300) is True
            await adapter.unlock()  # we never locked -> must not touch theirs
            assert await adapter.lock(ttl_s=300) is False  # still held
        finally:
            await other.aclose()

    # -- metadata --------------------------------------------------------

    async def test_encrypt_default_declared(self, adapter):
        assert isinstance(type(adapter).encrypt_default, bool)
        assert isinstance(type(adapter).name, str)
