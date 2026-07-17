# tests/test_sync_adapter_base.py
"""Sync S4: adapter base — contract shape, key validation, lock payloads,
retry/backoff policy. Pure unit tests, no I/O, no docker."""
from datetime import UTC, datetime

import pytest

from tiro.sync.adapters.base import (
    LOCK_KEY,
    RETRY_ATTEMPTS,
    RETRY_BASE_DELAY_S,
    AdapterError,
    KeyMissing,
    StorageAdapter,
    TransientAdapterError,
    lock_is_expired,
    lock_owner,
    make_lock_payload,
    retrying,
    validate_key,
    validate_prefix,
)


class TestContractShape:
    def test_constants(self):
        assert LOCK_KEY == "locks/sync.lock"
        assert RETRY_ATTEMPTS == 3
        assert RETRY_BASE_DELAY_S == 0.5

    def test_abstract_cannot_instantiate(self):
        with pytest.raises(TypeError):
            StorageAdapter()

    def test_exception_hierarchy(self):
        assert issubclass(KeyMissing, AdapterError)
        assert issubclass(TransientAdapterError, AdapterError)
        assert issubclass(AdapterError, Exception)

    def test_contract_method_names_frozen(self):
        # Skeleton-FROZEN signatures: put/get/list/delete/lock/unlock.
        for name in ("put", "get", "list", "delete", "lock", "unlock"):
            assert name in StorageAdapter.__abstractmethods__


class TestKeyValidation:
    @pytest.mark.parametrize("key", [
        "format.json",
        "journal/dev-a/000000000001.age",
        "objects/aa/" + "a" * 64 + ".age",
        "snapshots/01J00000000000000000000000/manifest.age",
        "locks/sync.lock",
        "devices/my-laptop 2.json",  # spaces are fine (adapters encode)
    ])
    def test_accepts_spec_layout_keys(self, key):
        assert validate_key(key) == key

    @pytest.mark.parametrize("key", [
        "", "/abs/path", "a//b", "../evil", "a/../b", "a/./b",
        "a\\b", "journal/",
    ])
    def test_rejects_bad_keys(self, key):
        with pytest.raises(AdapterError):
            validate_key(key)

    @pytest.mark.parametrize("prefix", ["", "journal/", "journal/dev-a/", "jour", "objects/aa"])
    def test_accepts_prefixes(self, prefix):
        assert validate_prefix(prefix) == prefix

    @pytest.mark.parametrize("prefix", ["/abs", "../x", "a/../b", "a\\b"])
    def test_rejects_bad_prefixes(self, prefix):
        with pytest.raises(AdapterError):
            validate_prefix(prefix)


class TestLockPayload:
    def test_roundtrip_owner(self):
        p = make_lock_payload("dev-a", 300)
        assert lock_owner(p) == "dev-a"

    def test_fresh_lock_not_expired(self):
        p = make_lock_payload("dev-a", 300)
        assert lock_is_expired(p) is False

    def test_expired_by_ttl(self):
        past = datetime(2020, 1, 1, tzinfo=UTC)
        p = make_lock_payload("dev-a", 60, now=past)
        assert lock_is_expired(p) is True

    def test_expiry_boundary_exact_ttl_is_expired(self):
        acquired = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)
        p = make_lock_payload("dev-a", 60, now=acquired)
        at_ttl = datetime(2026, 7, 11, 12, 1, 0, tzinfo=UTC)
        just_before = datetime(2026, 7, 11, 12, 0, 59, tzinfo=UTC)
        assert lock_is_expired(p, now=at_ttl) is True
        assert lock_is_expired(p, now=just_before) is False

    @pytest.mark.parametrize("garbage", [b"", b"not json", b"[1,2]", b'{"device_id": "x"}'])
    def test_garbage_payload_counts_as_expired_and_ownerless(self, garbage):
        # A corrupt lock must never wedge sync forever (decision #4).
        assert lock_is_expired(garbage) is True
        assert lock_owner(garbage) is None


class TestRetrying:
    async def test_success_first_try_no_sleep(self):
        sleeps = []

        async def fn():
            return "ok"

        async def sleep(s):
            sleeps.append(s)

        assert await retrying(fn, sleep=sleep) == "ok"
        assert sleeps == []

    async def test_transient_retried_then_succeeds_with_backoff(self):
        calls, sleeps = [], []

        async def fn():
            calls.append(1)
            if len(calls) < 3:
                raise TransientAdapterError("flaky")
            return "ok"

        async def sleep(s):
            sleeps.append(s)

        # rng=lambda: 0.5 makes jitter factor exactly 1.0 -> delays 0.5, 1.0
        assert await retrying(fn, sleep=sleep, rng=lambda: 0.5) == "ok"
        assert len(calls) == 3
        assert sleeps == [0.5, 1.0]

    async def test_exhausted_raises_last_transient(self):
        calls = []

        async def fn():
            calls.append(1)
            raise TransientAdapterError("still down")

        async def sleep(s):
            pass

        with pytest.raises(TransientAdapterError):
            await retrying(fn, sleep=sleep)
        assert len(calls) == RETRY_ATTEMPTS  # 3 total attempts

    async def test_non_transient_propagates_immediately(self):
        calls = []

        async def fn():
            calls.append(1)
            raise KeyMissing("nope")

        async def sleep(s):  # pragma: no cover - must not be reached
            raise AssertionError("slept on a non-transient error")

        with pytest.raises(KeyMissing):
            await retrying(fn, sleep=sleep)
        assert len(calls) == 1

    async def test_jitter_bounds(self):
        # jitter factor is 0.5 + rng() with rng in [0,1) -> [0.5, 1.5)
        sleeps = []

        async def fn():
            raise TransientAdapterError("x")

        async def sleep(s):
            sleeps.append(s)

        with pytest.raises(TransientAdapterError):
            await retrying(fn, sleep=sleep, rng=lambda: 0.0)
        assert sleeps == [0.25, 0.5]  # base 0.5 * 2^i * 0.5
