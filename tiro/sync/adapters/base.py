"""Storage-adapter base contract (sync S4).

FROZEN contract (spec/skeleton): async put(key, bytes) / get(key) -> bytes /
list(prefix) -> [key] / delete(key) / lock(ttl_s) -> bool / unlock().

Keys are POSIX-relative paths in the spec §5 backend layout
(journal/{device}/{seq}.age, objects/{h2}/{sha}.age, locks/sync.lock, ...).
Adapters are DUMB BYTE STORES: encryption (milestone S3's crypto.py) happens
above this layer; each adapter only DECLARES its encrypt_default (spec §5 —
filesystem OFF, s3/webdav ON).

Locking (spec §6.1) is best-effort advisory: atomic create-if-absent
(O_EXCL / conditional PUT), TTL expiry honored via the shared JSON payload
helpers below, one-shot steal of expired-or-garbage locks. The sync protocol
survives lock absence by design; the lock only reduces wasted work.

Retry policy (plan decision #2): only TransientAdapterError retries —
adapters classify 5xx/throttle/connection faults as transient; 4xx never
retry. 3 attempts total, exponential backoff with jitter, injectable sleep.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any, ClassVar

LOCK_KEY = "locks/sync.lock"
RETRY_ATTEMPTS = 3  # total attempts: initial + 2 retries
RETRY_BASE_DELAY_S = 0.5

# Module-level sleep hook so tests can monkeypatch
# tiro.sync.adapters.base._SLEEP without touching asyncio globally.
_SLEEP = asyncio.sleep


class AdapterError(Exception):
    """Base error for storage-adapter failures."""


class KeyMissing(AdapterError):
    """get() on a key that does not exist."""


class TransientAdapterError(AdapterError):
    """A retryable fault (5xx, throttle, connection error). Internal to the
    retry loop — public methods re-raise it only after retries exhaust."""


def validate_key(key: str) -> str:
    """Reject traversal/absolute/malformed keys. Load-bearing for the
    filesystem adapter; enforced by all adapters for symmetry."""
    if not key or key.startswith("/") or "\\" in key:
        raise AdapterError(f"invalid key: {key!r}")
    if any(part in ("", ".", "..") for part in key.split("/")):
        raise AdapterError(f"invalid key: {key!r}")
    return key


def validate_prefix(prefix: str) -> str:
    """Prefixes may be '' or end with '/' (unlike keys); traversal still rejected."""
    if prefix == "":
        return prefix
    if prefix.startswith("/") or "\\" in prefix:
        raise AdapterError(f"invalid prefix: {prefix!r}")
    if any(part in (".", "..") for part in prefix.split("/")):
        raise AdapterError(f"invalid prefix: {prefix!r}")
    return prefix


def make_lock_payload(device_id: str, ttl_s: int, *, now: datetime | None = None) -> bytes:
    now = now or datetime.now(UTC)
    return json.dumps(
        {
            "device_id": device_id,
            "acquired_at": now.isoformat(timespec="seconds"),
            "ttl_s": ttl_s,
        }
    ).encode("utf-8")


def lock_is_expired(payload: bytes, *, now: datetime | None = None) -> bool:
    """TTL check per spec §6.1. Garbage/unparseable payloads count as
    EXPIRED — a corrupt lock must never wedge sync forever."""
    now = now or datetime.now(UTC)
    try:
        data = json.loads(payload.decode("utf-8"))
        acquired = datetime.fromisoformat(data["acquired_at"])
        ttl_s = float(data["ttl_s"])
    except Exception:
        return True
    if acquired.tzinfo is None:  # defensive: treat naive stamps as UTC
        acquired = acquired.replace(tzinfo=UTC)
    return (now - acquired).total_seconds() >= ttl_s


def lock_owner(payload: bytes) -> str | None:
    """Owner of a VALID lock payload; None for garbage. A payload that fails
    full-shape validation (missing/unparseable acquired_at or ttl_s) is
    ownerless — a corrupt lock is stealable, never attributed (decision #4)."""
    try:
        data = json.loads(payload.decode("utf-8"))
        owner = data["device_id"]
        datetime.fromisoformat(data["acquired_at"])
        float(data["ttl_s"])
        return owner if isinstance(owner, str) else None
    except Exception:
        return None


async def retrying(fn, *, attempts: int = RETRY_ATTEMPTS,
                   base_delay_s: float = RETRY_BASE_DELAY_S,
                   sleep=None, rng=None) -> Any:
    """Run async fn(); retry TransientAdapterError with jittered exponential
    backoff (delay = base * 2^attempt * (0.5 + rng())). Anything else
    propagates immediately; the last transient error propagates when
    attempts exhaust. Lock CONTENTION is never routed through here —
    a held lock is an answer (False), not a fault."""
    sleep = sleep or _SLEEP
    rng = rng or random.random
    for attempt in range(attempts):
        try:
            return await fn()
        except TransientAdapterError:
            if attempt == attempts - 1:
                raise
            await sleep(base_delay_s * (2**attempt) * (0.5 + rng()))
    raise AssertionError("unreachable")  # pragma: no cover


def audit_adapter_call(config, endpoint: str, *, started: float,
                       nbytes: int | None = None, count: int | None = None,
                       success: bool = True, error: str | None = None) -> None:
    """One service='sync' audit line per public NETWORK adapter call (M6
    pattern). config=None (unit tests, filesystem adapter) => no-op.
    log_api_call swallows its own failures; import is lazy so base.py has
    no import-time repo dependencies."""
    if config is None:
        return
    from tiro.audit import log_api_call

    log_api_call(
        config,
        "sync",
        endpoint=endpoint,
        bytes_out=nbytes,
        count=count,
        duration_ms=int((time.monotonic() - started) * 1000),
        success=success,
        error=error,
    )


class StorageAdapter(ABC):
    """FROZEN adapter contract. list() returns FULL keys relative to the
    adapter root (string-prefix filtered, lexicographically sorted).
    delete() is idempotent (missing key is success). get() raises
    KeyMissing on an absent key."""

    name: ClassVar[str]
    encrypt_default: ClassVar[bool]

    @abstractmethod
    async def put(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    async def get(self, key: str) -> bytes: ...

    @abstractmethod
    async def list(self, prefix: str) -> list[str]: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def lock(self, ttl_s: int) -> bool: ...

    @abstractmethod
    async def unlock(self) -> None: ...

    async def aclose(self) -> None:  # noqa: B027
        """Release client resources (httpx). Default no-op — deliberately
        concrete, not abstract: the shared conformance suite may call it on
        any adapter, and only httpx-backed adapters need to override it."""
