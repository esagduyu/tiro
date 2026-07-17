"""Sync storage adapters (S4).

FROZEN contract: async put/get/list/delete/lock/unlock over
httpx (webdav) / boto3-in-to-thread (s3) / plain disk (filesystem).
Adapters are dumb byte stores — encryption happens above this layer.
"""

from tiro.sync.adapters.base import (
    LOCK_KEY,
    AdapterError,
    KeyMissing,
    StorageAdapter,
    TransientAdapterError,
)

__all__ = [
    "LOCK_KEY",
    "AdapterError",
    "KeyMissing",
    "StorageAdapter",
    "TransientAdapterError",
]
