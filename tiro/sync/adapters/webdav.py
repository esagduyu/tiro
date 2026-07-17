"""WebDAV storage adapter (sync S4) — hand-rolled on httpx (spec §8: no new
dep; WebDAV needs ~6 verbs and httpx is already here).

encrypt_default=True per spec §5 (metadata only; S5's setup enforces the
typed confirmation for turning it off).

Quirks catalogue this implementation encodes (plan decision #3):
- list() = BFS of Depth-1 PROPFINDs (Depth: infinity is widely disabled).
- put() MKCOLs missing parent collections top-down (405/301 = exists);
  created collections are cached per instance.
- 207 Multi-Status hrefs are percent-encoded absolute paths/URLs and
  include the collection itself -> unquote, relativize, skip self;
  collections identified by {DAV:}resourcetype/{DAV:}collection.
- PROPFIND on a missing path -> 404 -> empty list. DELETE on a missing
  key -> 404 -> success (idempotent).
- Locking NEVER uses the optional LOCK verb: pre-check GET (fresh foreign
  lock -> False; expired/garbage -> DELETE), then PUT If-None-Match: *
  (412 -> False), then read-back device_id verification (servers that
  ignore the precondition degrade to best-effort; spec §6.1 tolerates it).
- Keys percent-encoded with quote(key) outbound, unquote(href) inbound.

Audit: one service="sync" line per HTTP request (webdav:put/get/propfind/
mkcol/delete/lock), so MKCOL/PROPFIND sub-requests are visible — finer
than s3's per-operation line, deliberately (they are real network calls).
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlsplit

import httpx

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

_PROPFIND_BODY = (
    b'<?xml version="1.0"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
)


class WebDAVAdapter(StorageAdapter):
    name = "webdav"
    encrypt_default = True  # spec §5

    def __init__(self, base_url: str, *, username: str, password: str,
                 device_id: str, config=None, transport=None,
                 timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._base_path = unquote(urlsplit(self.base_url).path).rstrip("/")
        self.device_id = device_id
        self._config = config
        self._locked = False
        self._collections: set[str] = set()  # MKCOL cache
        self._client = httpx.AsyncClient(
            auth=(username, password), transport=transport, timeout=timeout
        )

    def _url(self, key: str) -> str:
        return f"{self.base_url}/{quote(key)}" if key else self.base_url

    async def _request(self, endpoint: str, method: str, url: str, *,
                       nbytes: int | None = None, headers: dict | None = None,
                       content: bytes | None = None) -> httpx.Response:
        started = time.monotonic()

        async def attempt() -> httpx.Response:
            try:
                resp = await self._client.request(
                    method, url, headers=headers, content=content
                )
            except httpx.TransportError as e:
                raise TransientAdapterError(f"{endpoint}: {e}") from e
            if resp.status_code >= 500:
                raise TransientAdapterError(f"{endpoint}: HTTP {resp.status_code}")
            return resp

        try:
            resp = await retrying(attempt)
        except Exception as e:
            audit_adapter_call(self._config, endpoint, started=started,
                               nbytes=nbytes, success=False, error=str(e)[:200])
            raise
        audit_adapter_call(self._config, endpoint, started=started, nbytes=nbytes)
        return resp

    async def _ensure_parents(self, key: str) -> None:
        parts = key.split("/")[:-1]
        path = ""
        for part in parts:
            path = f"{path}/{part}" if path else part
            if path in self._collections:
                continue
            resp = await self._request("webdav:mkcol", "MKCOL", self._url(path))
            if resp.status_code in (201, 405, 301):  # created / already exists
                self._collections.add(path)
            else:
                raise AdapterError(f"MKCOL {path}: HTTP {resp.status_code}")

    async def put(self, key: str, data: bytes) -> None:
        validate_key(key)
        await self._ensure_parents(key)
        resp = await self._request("webdav:put", "PUT", self._url(key),
                                   content=data, nbytes=len(data))
        if resp.status_code not in (200, 201, 204):
            raise AdapterError(f"PUT {key}: HTTP {resp.status_code}")

    async def get(self, key: str) -> bytes:
        validate_key(key)
        resp = await self._request("webdav:get", "GET", self._url(key))
        if resp.status_code == 404:
            raise KeyMissing(key)
        if resp.status_code != 200:
            raise AdapterError(f"GET {key}: HTTP {resp.status_code}")
        return resp.content

    async def delete(self, key: str) -> None:
        validate_key(key)
        resp = await self._request("webdav:delete", "DELETE", self._url(key))
        if resp.status_code not in (200, 204, 404):  # 404 = idempotent success
            raise AdapterError(f"DELETE {key}: HTTP {resp.status_code}")

    async def list(self, prefix: str) -> list[str]:
        validate_prefix(prefix)
        start = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        keys: list[str] = []
        queue = [start]
        while queue:
            coll = queue.pop(0)
            resp = await self._request(
                "webdav:propfind", "PROPFIND", self._url(coll),
                headers={"Depth": "1"}, content=_PROPFIND_BODY,
            )
            if resp.status_code == 404:
                continue  # missing prefix -> nothing to list
            if resp.status_code != 207:
                raise AdapterError(f"PROPFIND {coll}: HTTP {resp.status_code}")
            for href, is_coll in self._parse_multistatus(resp.content):
                rel = self._relativize(href)
                if rel is None or rel == coll or rel == "":
                    continue  # foreign href or the collection's self-entry
                if is_coll:
                    # descend only where the subtree can intersect the prefix
                    if (rel + "/").startswith(prefix) or prefix.startswith(rel + "/"):
                        queue.append(rel)
                elif rel.startswith(prefix):
                    keys.append(rel)
        return sorted(keys)

    @staticmethod
    def _parse_multistatus(content: bytes) -> list[tuple[str, bool]]:
        try:
            root = ET.fromstring(content)
        except ET.ParseError as e:
            raise AdapterError(f"unparseable PROPFIND response: {e}") from e
        out: list[tuple[str, bool]] = []
        for resp in root.findall("{DAV:}response"):
            href_el = resp.find("{DAV:}href")
            if href_el is None or not href_el.text:
                continue
            is_coll = resp.find(
                ".//{DAV:}resourcetype/{DAV:}collection") is not None
            out.append((href_el.text, is_coll))
        return out

    def _relativize(self, href: str) -> str | None:
        """href may be an absolute URL or an absolute percent-encoded path."""
        path = unquote(urlsplit(href).path).rstrip("/")
        if not path.startswith(self._base_path):
            return None
        return path[len(self._base_path):].strip("/")

    async def lock(self, ttl_s: int) -> bool:
        payload = make_lock_payload(self.device_id, ttl_s)
        await self._ensure_parents(LOCK_KEY)
        for _attempt in range(2):  # initial + one post-steal retry
            # (1) pre-check: guards precondition-ignoring servers too
            try:
                existing = await self.get(LOCK_KEY)
                if not lock_is_expired(existing):
                    return False
                await self.delete(LOCK_KEY)  # expired/garbage -> steal
            except KeyMissing:
                pass
            # (2) conditional create
            resp = await self._request(
                "webdav:lock", "PUT", self._url(LOCK_KEY),
                content=payload, nbytes=len(payload),
                headers={"If-None-Match": "*"},
            )
            if resp.status_code == 412:
                continue  # someone re-created it first; re-evaluate once
            if resp.status_code not in (200, 201, 204):
                raise AdapterError(f"lock PUT: HTTP {resp.status_code}")
            # (3) read-back verification (precondition-ignoring servers)
            try:
                current = await self.get(LOCK_KEY)
            except KeyMissing:
                return False
            if lock_owner(current) == self.device_id:
                self._locked = True
                return True
            return False
        return False  # lost the steal race; next cycle retries

    async def unlock(self) -> None:
        if not self._locked:
            return
        try:
            if lock_owner(await self.get(LOCK_KEY)) == self.device_id:
                await self.delete(LOCK_KEY)
        except KeyMissing:
            pass
        self._locked = False

    async def aclose(self) -> None:
        await self._client.aclose()
