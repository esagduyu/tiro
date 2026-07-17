"""Sync S4: filesystem adapter — conformance (the ALWAYS-ON gate; no docker,
no network) + filesystem-specific atomicity/traversal cases."""
import pytest

from tests.sync_conformance import AdapterConformance
from tiro.sync.adapters.base import AdapterError, KeyMissing
from tiro.sync.adapters.filesystem import FilesystemAdapter


class TestFilesystemConformance(AdapterConformance):
    @pytest.fixture
    def make_adapter(self, tmp_path):
        def make(device_id: str) -> FilesystemAdapter:
            return FilesystemAdapter(tmp_path / "remote", device_id=device_id)

        return make


class TestFilesystemSpecifics:
    def _adapter(self, tmp_path):
        return FilesystemAdapter(tmp_path / "remote", device_id="dev-a")

    def test_encrypt_default_off(self):
        # Spec §5: the folder is the user's own disk; transport encryption
        # is Syncthing/iCloud's job. Default OFF.
        assert FilesystemAdapter.encrypt_default is False
        assert FilesystemAdapter.name == "filesystem"

    @pytest.mark.parametrize("key", ["../evil", "/abs", "a/../b", "a\\b", ""])
    async def test_rejects_traversal_keys(self, tmp_path, key):
        a = self._adapter(tmp_path)
        with pytest.raises(AdapterError):
            await a.put(key, b"x")
        with pytest.raises(AdapterError):
            await a.get(key)
        with pytest.raises(AdapterError):
            await a.delete(key)

    async def test_failed_put_leaves_no_partial_object(self, tmp_path, monkeypatch):
        """Partial-upload failure injection: atomicity comes from
        temp-write + os.replace. A crash between the two must leave the
        final key absent AND no temp turd visible to list()."""
        a = self._adapter(tmp_path)

        def boom(src, dst):
            raise OSError("simulated crash during rename")

        monkeypatch.setattr("tiro.sync.adapters.filesystem.os.replace", boom)
        with pytest.raises(OSError):
            await a.put("objects/aa/partial.age", b"half-uploaded")
        monkeypatch.undo()
        with pytest.raises(KeyMissing):
            await a.get("objects/aa/partial.age")
        assert await a.list("objects/") == []

    async def test_list_hides_temp_files(self, tmp_path):
        a = self._adapter(tmp_path)
        await a.put("objects/aa/real.age", b"x")
        turd = tmp_path / "remote" / "objects" / "aa" / f"{a.TMP_PREFIX}leftover"
        turd.write_bytes(b"crash leftover")
        assert await a.list("objects/") == ["objects/aa/real.age"]

    async def test_interrupted_steal_race_yields_false_not_crash(self, tmp_path):
        """Two adapters both see an expired lock; one steals, the loser of
        the O_EXCL race returns False (next cycle retries) — never raises."""
        from datetime import UTC, datetime

        from tiro.sync.adapters.base import LOCK_KEY, make_lock_payload

        a = self._adapter(tmp_path)
        b = FilesystemAdapter(tmp_path / "remote", device_id="dev-b")
        stale = make_lock_payload("dev-dead", 1, now=datetime(2020, 1, 1, tzinfo=UTC))
        await a.put(LOCK_KEY, stale)
        assert await a.lock(ttl_s=300) is True  # a steals the expired lock
        assert await b.lock(ttl_s=300) is False  # b sees a's FRESH lock, yields
