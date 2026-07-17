"""Filesystem storage adapter (sync S4).

The sync root is a local directory (typically inside a Syncthing/iCloud/
Dropbox-synced folder). encrypt_default=False per spec §5: the folder is the
user's own disk and those services provide their own transport encryption.

Atomicity: put() writes a .tiro-tmp-* sibling then os.replace()s it into
place (the repo's persist_config pattern) — a crash mid-put never leaves a
readable partial object, and list() hides temp files.

Locking: O_EXCL create of locks/sync.lock per spec §6.1, with a one-shot
steal of expired/garbage locks (base.lock_is_expired).

Methods are async to satisfy the FROZEN contract but do plain synchronous
disk I/O (local writes are sub-ms at these blob sizes; matching S1's
file-I/O posture). No retries (decision #2) and no audit lines (decision #6:
audit covers NETWORK calls only).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from tiro.sync.adapters.base import (
    LOCK_KEY,
    KeyMissing,
    StorageAdapter,
    lock_is_expired,
    lock_owner,
    make_lock_payload,
    validate_key,
    validate_prefix,
)


class FilesystemAdapter(StorageAdapter):
    name = "filesystem"
    encrypt_default = False  # spec §5

    TMP_PREFIX = ".tiro-tmp-"

    def __init__(self, root: Path | str, *, device_id: str):
        self.root = Path(root)
        self.device_id = device_id
        self._locked = False

    def _path(self, key: str) -> Path:
        validate_key(key)
        return self.root / key

    async def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f"{self.TMP_PREFIX}{uuid.uuid4().hex}"
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)  # no-op on success (replace consumed it)

    async def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise KeyMissing(key) from None

    async def list(self, prefix: str) -> list[str]:
        validate_prefix(prefix)
        # Walk the deepest directory the prefix implies (cheap), then
        # string-filter (correct for non-directory-aligned prefixes).
        base = self.root / prefix.rsplit("/", 1)[0] if "/" in prefix else self.root
        if not base.is_dir():
            return []
        keys = []
        for p in base.rglob("*"):
            if p.is_file() and not p.name.startswith(self.TMP_PREFIX):
                key = p.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    keys.append(key)
        return sorted(keys)

    async def delete(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass  # idempotent

    async def lock(self, ttl_s: int) -> bool:
        path = self.root / LOCK_KEY
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = make_lock_payload(self.device_id, ttl_s)
        for _attempt in range(2):  # initial + one post-steal retry
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    existing = path.read_bytes()
                except FileNotFoundError:
                    continue  # released between our attempts -> retry create
                if lock_is_expired(existing):
                    path.unlink(missing_ok=True)  # steal; retry O_EXCL
                    continue
                return False
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            self._locked = True
            return True
        return False  # lost the steal race; next cycle retries

    async def unlock(self) -> None:
        if not self._locked:
            return
        path = self.root / LOCK_KEY
        try:
            if lock_owner(path.read_bytes()) == self.device_id:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        self._locked = False
