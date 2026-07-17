"""S3 storage adapter (sync S4) — works against AWS S3, MinIO, and other
S3-compatible endpoints.

boto3's sync client wrapped in asyncio.to_thread (plan decision #1 — NOT
aioboto3: not pre-cleared, pins botocore; the sync cycle is sequential so
thread-per-call is fine; to_thread is repo precedent). botocore's own
retries are DISABLED so base.retrying is the single retry policy.

encrypt_default=True per spec §5 (turning it off requires typed
confirmation — S5's setup UI enforces that; the flag here is metadata).

Locking (spec §6.1): conditional PUT `If-None-Match: *` (native S3
conditional writes — AWS since 2024-08, MinIO since late 2024). Backends
that reject the precondition mechanism itself (501 NotImplemented, or an
old boto3 without the IfNoneMatch param => ParamValidationError) degrade to
documented best-effort: read-check-put + read-back device_id verification
(plan decision #12); the sync protocol tolerates lock imperfection by design.
"""

from __future__ import annotations

import asyncio
import time

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import (
    ClientError,
    HTTPClientError,
    IncompleteReadError,
    ParamValidationError,
)
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)

from tiro.sync.adapters.base import (
    LOCK_KEY,
    AdapterError,
    KeyMissing,
    StorageAdapter,
    TransientAdapterError,
    audit_adapter_call,
    lock_is_expired,
    lock_owner,
    make_lock_payload,
    retrying,
    validate_key,
    validate_prefix,
)

# ClientError codes treated as transient alongside any HTTP status >= 500.
_TRANSIENT_CODES = {
    "InternalError", "ServiceUnavailable", "SlowDown", "RequestTimeout",
    "Throttling", "ThrottlingException", "RequestLimitExceeded",
}


def _classify(e: ClientError) -> tuple[str, int]:
    code = e.response.get("Error", {}).get("Code", "")
    status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    return code, status


def _is_transient(code: str, status: int) -> bool:
    """5xx and throttle codes retry — EXCEPT 501 NotImplemented, which never
    heals by retrying (it's a capability answer, not a fault) and must reach
    lock()'s best-effort fallback un-retried (plan decision #12; the plan's
    reference code classified it transient, making the fallback dead code —
    caught in per-task review)."""
    if status == 501 or code == "NotImplemented":
        return False
    return status >= 500 or status == 429 or code in _TRANSIENT_CODES


class S3Adapter(StorageAdapter):
    name = "s3"
    encrypt_default = True  # spec §5

    def __init__(self, *, endpoint_url: str, bucket: str, access_key: str,
                 secret_key: str, device_id: str, prefix: str = "",
                 region: str = "us-east-1", config=None, page_size: int = 1000,
                 client=None):
        self.bucket = bucket
        self.device_id = device_id
        self.prefix = prefix
        self._config = config  # TiroConfig for audit lines; None => no audit
        self._page_size = page_size
        self._locked = False
        self._client = client or boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=BotoConfig(
                retries={"max_attempts": 1, "mode": "standard"},  # ours governs
                connect_timeout=10,
                read_timeout=60,
            ),
        )

    def _k(self, key: str) -> str:
        validate_key(key)
        return self.prefix + key

    async def _call(self, endpoint: str, fn, *, nbytes: int | None = None,
                    count: int | None = None, **kwargs):
        """to_thread + transient classification + retry + one audit line."""
        started = time.monotonic()

        async def attempt():
            try:
                return await asyncio.to_thread(fn, **kwargs)
            except ClientError as e:
                code, status = _classify(e)
                if _is_transient(code, status):
                    raise TransientAdapterError(f"{endpoint}: {code or status}") from e
                raise
            except (BotoConnectionError, HTTPClientError, IncompleteReadError) as e:
                # endpoint/connect/read-timeout/connection-closed/mid-stream
                # body-read families (IncompleteReadError is a bare
                # BotoCoreError, not under the other two)
                raise TransientAdapterError(f"{endpoint}: {e}") from e

        try:
            result = await retrying(attempt)
        except Exception as e:
            audit_adapter_call(self._config, endpoint, started=started,
                               nbytes=nbytes, count=count, success=False,
                               error=str(e)[:200])
            raise
        audit_adapter_call(self._config, endpoint, started=started,
                           nbytes=nbytes, count=count)
        return result

    async def put(self, key: str, data: bytes) -> None:
        try:
            await self._call("s3:put", self._client.put_object, nbytes=len(data),
                             Bucket=self.bucket, Key=self._k(key), Body=data)
        except ClientError as e:
            raise AdapterError(f"put {key}: {e}") from e

    async def get(self, key: str) -> bytes:
        k = self._k(key)

        def _get_sync() -> bytes:
            # Body read happens INSIDE the retried/classified/audited
            # envelope: get_object returns at headers, the body streams
            # after — a mid-stream disconnect must retry and audit as a
            # failure, not escape as a raw botocore streaming error after
            # a success audit line (per-task review fix).
            resp = self._client.get_object(Bucket=self.bucket, Key=k)
            return resp["Body"].read()

        try:
            return await self._call("s3:get", _get_sync)
        except ClientError as e:
            code, status = _classify(e)
            if code in ("NoSuchKey", "404") or status == 404:
                raise KeyMissing(key) from e
            raise AdapterError(f"get {key}: {e}") from e

    async def list(self, prefix: str) -> list[str]:
        validate_prefix(prefix)

        def _list_sync() -> list[str]:
            paginator = self._client.get_paginator("list_objects_v2")
            keys: list[str] = []
            for page in paginator.paginate(
                Bucket=self.bucket, Prefix=self.prefix + prefix,
                PaginationConfig={"PageSize": self._page_size},
            ):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"][len(self.prefix):])
            return keys

        async def attempt():
            try:
                return await asyncio.to_thread(_list_sync)
            except ClientError as e:
                code, status = _classify(e)
                if _is_transient(code, status):
                    raise TransientAdapterError(f"s3:list: {code or status}") from e
                raise AdapterError(f"list {prefix}: {e}") from e
            except (BotoConnectionError, HTTPClientError, IncompleteReadError) as e:
                raise TransientAdapterError(f"s3:list: {e}") from e

        started = time.monotonic()
        try:
            keys = await retrying(attempt)
        except Exception as e:
            audit_adapter_call(self._config, "s3:list", started=started,
                               success=False, error=str(e)[:200])
            raise
        audit_adapter_call(self._config, "s3:list", started=started, count=len(keys))
        return sorted(keys)

    async def delete(self, key: str) -> None:
        # S3 DeleteObject is idempotent (204 even when absent).
        try:
            await self._call("s3:delete", self._client.delete_object,
                             Bucket=self.bucket, Key=self._k(key))
        except ClientError as e:
            raise AdapterError(f"delete {key}: {e}") from e

    async def lock(self, ttl_s: int) -> bool:
        payload = make_lock_payload(self.device_id, ttl_s)
        for _attempt in range(2):  # initial + one post-steal retry
            try:
                await self._call("s3:lock", self._client.put_object,
                                 nbytes=len(payload), Bucket=self.bucket,
                                 Key=self._k(LOCK_KEY), Body=payload,
                                 IfNoneMatch="*")
                self._locked = True
                return True
            except ParamValidationError:
                # boto3 too old for IfNoneMatch — best-effort fallback.
                return await self._lock_best_effort(payload)
            except ClientError as e:
                code, status = _classify(e)
                if status == 412 or code in ("PreconditionFailed", "412"):
                    pass  # held — fall through to expiry check
                elif status == 501 or code == "NotImplemented":
                    # backend doesn't support conditional PUT
                    return await self._lock_best_effort(payload)
                else:
                    raise AdapterError(f"lock: {e}") from e
            # Contended: honor TTL expiry (spec §6.1), steal once.
            try:
                existing = await self.get(LOCK_KEY)
            except KeyMissing:
                continue  # released between calls -> retry conditional PUT
            if lock_is_expired(existing):
                await self.delete(LOCK_KEY)
                continue
            return False
        return False  # lost the steal race; next cycle retries

    async def _lock_best_effort(self, payload: bytes) -> bool:
        """No conditional-PUT support: read-check-put + read-back verify.
        A documented race window remains; spec §6.1 tolerates it (per-device
        journals + content-addressed objects keep two writers safe)."""
        try:
            existing = await self.get(LOCK_KEY)
            if not lock_is_expired(existing):
                return False
        except KeyMissing:
            pass
        await self.put(LOCK_KEY, payload)
        try:
            current = await self.get(LOCK_KEY)
        except KeyMissing:
            return False
        if lock_owner(current) == self.device_id:
            self._locked = True
            return True
        return False

    async def unlock(self) -> None:
        if not self._locked:
            return
        try:
            if lock_owner(await self.get(LOCK_KEY)) == self.device_id:
                await self.delete(LOCK_KEY)
        except KeyMissing:
            pass
        self._locked = False
