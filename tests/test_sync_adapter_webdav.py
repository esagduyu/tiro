"""Sync S4: WebDAV adapter (hand-rolled on httpx).

Three tiers:
- FakeDav (httpx.MockTransport in-memory WebDAV server): the FULL shared
  conformance suite, ALWAYS-ON — no docker, no network. FakeDav implements
  the quirks catalogue (Depth-1 PROPFIND, MKCOL 405/409, percent-encoded
  hrefs incl. the collection's self-entry, 404s, If-None-Match on PUT).
- TestWebDAVQuirks: failure injection (5xx retry, encoded keys,
  precondition-ignoring servers) against FakeDav. ALWAYS-ON.
- TestWebDAVNextcloudConformance: the shared suite against a REAL Nextcloud
  from deploy/docker/docker-compose.sync-test.yml — AUTO-SKIPS when absent.
"""
import os
import uuid
from urllib.parse import quote, unquote

import httpx
import pytest

from tests.sync_conformance import AdapterConformance
from tiro.sync.adapters import base as adapter_base
from tiro.sync.adapters.base import LOCK_KEY, make_lock_payload
from tiro.sync.adapters.webdav import WebDAVAdapter

# ---------------------------------------------------------------- FakeDav


class FakeDav:
    """Minimal in-memory WebDAV server — the reference implementation of the
    quirks catalogue. Shared by every adapter instance a test creates."""

    def __init__(self, base_path: str = "/dav", honor_if_none_match: bool = True):
        self.base_path = base_path
        self.files: dict[str, bytes] = {}
        self.collections: set[str] = {""}
        self.honor_if_none_match = honor_if_none_match
        self.fail_next: list[int] = []  # injected status codes, FIFO
        self.requests: list[tuple[str, str]] = []  # (method, rel-path) log

    def _rel(self, request: httpx.Request) -> str:
        path = unquote(request.url.path)
        assert path.startswith(self.base_path), path
        return path[len(self.base_path):].strip("/")

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0))
        rel = self._rel(request)
        method = request.method
        self.requests.append((method, rel))
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if method == "MKCOL":
            if rel in self.collections:
                return httpx.Response(405)  # exists
            if parent not in self.collections:
                return httpx.Response(409)  # missing intermediate
            self.collections.add(rel)
            return httpx.Response(201)
        if method == "PUT":
            if parent not in self.collections:
                return httpx.Response(409)
            if (self.honor_if_none_match
                    and request.headers.get("If-None-Match") == "*"
                    and rel in self.files):
                return httpx.Response(412)
            self.files[rel] = request.content
            return httpx.Response(201)
        if method == "GET":
            if rel in self.files:
                return httpx.Response(200, content=self.files[rel])
            return httpx.Response(404)
        if method == "DELETE":
            if rel in self.files:
                del self.files[rel]
                return httpx.Response(204)
            return httpx.Response(404)
        if method == "PROPFIND":
            if rel not in self.collections:
                return httpx.Response(404)
            assert request.headers.get("Depth") == "1"
            prefix = f"{rel}/" if rel else ""
            entries = [(rel, True)]  # the collection lists ITSELF first
            for c in sorted(self.collections):
                if c != rel and c.startswith(prefix) and "/" not in c[len(prefix):]:
                    entries.append((c, True))
            for f in sorted(self.files):
                if f.startswith(prefix) and "/" not in f[len(prefix):]:
                    entries.append((f, False))
            parts = []
            for name, is_coll in entries:
                href = quote(f"{self.base_path}/{name}" + ("/" if is_coll and name else ""))
                rtype = ("<d:resourcetype><d:collection/></d:resourcetype>"
                         if is_coll else "<d:resourcetype/>")
                parts.append(
                    f"<d:response><d:href>{href}</d:href><d:propstat>"
                    f"<d:prop>{rtype}</d:prop>"
                    f"<d:status>HTTP/1.1 200 OK</d:status>"
                    f"</d:propstat></d:response>"
                )
            xml = ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
                   + "".join(parts) + "</d:multistatus>")
            return httpx.Response(
                207, content=xml.encode(),
                headers={"Content-Type": "application/xml; charset=utf-8"},
            )
        return httpx.Response(405)


def _fake_adapter(dav: FakeDav, device_id: str = "dev-a") -> WebDAVAdapter:
    return WebDAVAdapter(
        f"http://dav.test{dav.base_path}",
        username="u", password="p", device_id=device_id,
        transport=httpx.MockTransport(dav.handler),
    )


class TestWebDAVFakeConformance(AdapterConformance):
    """Full shared conformance suite over FakeDav — the always-on webdav gate."""

    @pytest.fixture
    def make_adapter(self):
        dav = FakeDav()  # one shared backing store per test

        def make(device_id: str) -> WebDAVAdapter:
            return _fake_adapter(dav, device_id)

        return make


class TestWebDAVQuirks:
    @pytest.fixture
    def no_sleep(self, monkeypatch):
        slept = []

        async def fake_sleep(s):
            slept.append(s)

        monkeypatch.setattr(adapter_base, "_SLEEP", fake_sleep)
        return slept

    async def test_5xx_retried_then_succeeds(self, no_sleep):
        dav = FakeDav()
        a = _fake_adapter(dav)
        dav.fail_next = [503, 500]
        await a.put("format.json", b"v1")  # 503, 500, then 201
        assert await a.get("format.json") == b"v1"
        assert len(no_sleep) >= 2
        await a.aclose()

    async def test_5xx_exhausts_raises(self, no_sleep):
        from tiro.sync.adapters.base import AdapterError

        dav = FakeDav()
        a = _fake_adapter(dav)
        dav.fail_next = [503, 503, 503, 503, 503, 503]
        with pytest.raises(AdapterError):
            await a.get("format.json")
        await a.aclose()

    async def test_429_rate_limit_retried_then_succeeds(self, no_sleep):
        """Nextcloud brute-force/rate protection emits real 429s — they are
        throttling (decision #2's transient class), not a terminal answer."""
        dav = FakeDav()
        a = _fake_adapter(dav)
        dav.fail_next = [429]
        await a.put("format.json", b"v1")  # 429, then MKCOL/PUT proceed
        assert await a.get("format.json") == b"v1"
        assert len(no_sleep) >= 1
        await a.aclose()

    async def test_relativize_never_claims_sibling_paths(self):
        """base path /dav must not relativize /davish/... (prefix-string
        trap); such hrefs are foreign and dropped."""
        dav = FakeDav()
        a = _fake_adapter(dav)
        assert a._relativize("/davish/journal/x.age") is None
        assert a._relativize("/dav/journal/x.age") == "journal/x.age"
        assert a._relativize("http://dav.test/dav/journal/x.age") == "journal/x.age"
        await a.aclose()

    async def test_unicode_and_space_keys_percent_encoded(self):
        dav = FakeDav()
        a = _fake_adapter(dav)
        key = "devices/my laptop é.json"
        await a.put(key, b"data")
        assert await a.get(key) == b"data"
        assert await a.list("devices/") == [key]
        await a.aclose()

    async def test_put_creates_intermediate_collections(self):
        dav = FakeDav()
        a = _fake_adapter(dav)
        await a.put("journal/dev-a/000000000001.age", b"seg")
        assert "journal" in dav.collections
        assert "journal/dev-a" in dav.collections
        # second put reuses the MKCOL cache (no new MKCOL requests)
        before = sum(1 for m, _ in dav.requests if m == "MKCOL")
        await a.put("journal/dev-a/000000000002.age", b"seg2")
        assert sum(1 for m, _ in dav.requests if m == "MKCOL") == before
        await a.aclose()

    async def test_lock_sends_if_none_match(self):
        dav = FakeDav()
        a = _fake_adapter(dav)
        assert await a.lock(ttl_s=300) is True
        await a.aclose()

    async def test_precondition_ignoring_server_precheck_blocks_fresh_foreign_lock(self):
        """Server that IGNORES If-None-Match: the pre-check GET must still
        refuse to clobber a fresh foreign lock (degraded best-effort)."""
        dav = FakeDav(honor_if_none_match=False)
        a = _fake_adapter(dav)
        dav.collections.add("locks")
        dav.files[LOCK_KEY] = make_lock_payload("dev-foreign", 300)
        assert await a.lock(ttl_s=300) is False
        from tiro.sync.adapters.base import lock_owner

        assert lock_owner(dav.files[LOCK_KEY]) == "dev-foreign"  # never clobbered
        await a.aclose()

    async def test_encrypt_default_on(self):
        assert WebDAVAdapter.encrypt_default is True  # spec §5
        assert WebDAVAdapter.name == "webdav"


# ------------------------------------------------------------- Nextcloud

WEBDAV_HOST = os.environ.get("TIRO_TEST_WEBDAV_URL", "http://localhost:8081")
NC_USER = os.environ.get("TIRO_TEST_WEBDAV_USER", "tiro")
NC_PASS = os.environ.get("TIRO_TEST_WEBDAV_PASSWORD", "tiro-sync-test-pass")


def _nextcloud_available() -> bool:
    try:
        r = httpx.get(f"{WEBDAV_HOST}/status.php", timeout=2.0)
        return r.status_code == 200 and '"installed":true' in r.text
    except Exception:
        return False


requires_nextcloud = pytest.mark.skipif(
    not _nextcloud_available(),
    reason=(
        "Nextcloud not reachable/installed — start it with: "
        "docker compose -f deploy/docker/docker-compose.sync-test.yml up -d nextcloud "
        "(first boot takes ~30-60s to auto-install)"
    ),
)


@requires_nextcloud
class TestWebDAVNextcloudConformance(AdapterConformance):
    @pytest.fixture
    def make_adapter(self):
        dav_root = f"{WEBDAV_HOST}/remote.php/dav/files/{NC_USER}"
        run = f"tiro-sync-test/t-{uuid.uuid4().hex}"  # per-test collection
        with httpx.Client(auth=(NC_USER, NC_PASS), timeout=10.0) as c:
            for coll in ("tiro-sync-test", run):
                c.request("MKCOL", f"{dav_root}/{coll}")  # 405 = exists, fine

        def make(device_id: str) -> WebDAVAdapter:
            return WebDAVAdapter(
                f"{dav_root}/{run}", username=NC_USER, password=NC_PASS,
                device_id=device_id,
            )

        return make
