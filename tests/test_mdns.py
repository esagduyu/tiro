"""Phase 3 M3.0: mDNS/Bonjour advertisement + remote_url config + `tiro
status` surfacing.

Zeroconf is NEVER touched for real here (no real sockets/mDNS in CI) --
`tiro.mdns`'s `Zeroconf`/`ServiceInfo` names and its `_detect_lan_ips`
import are monkeypatched with fakes. App-lifespan wiring tests instead
patch `tiro.mdns.register_mdns`/`unregister_mdns` as spies, exercising only
tiro/app.py's decision logic (enabled + non-loopback gating, failure
isolation, unconditional unregister on shutdown).
"""

import asyncio
import socket

import pytest
from fastapi.testclient import TestClient

import tiro.mdns as mdns_mod
from tiro.config import TiroConfig


class FakeServiceInfo:
    def __init__(self, service_type, name, addresses=None, port=None, server=None):
        self.service_type = service_type
        self.name = name
        self.addresses = addresses
        self.port = port
        self.server = server


class FakeZeroconf:
    def __init__(self):
        self.registered = []
        self.closed = False

    def register_service(self, info):
        self.registered.append(info)

    def unregister_service(self, info):
        self.registered.remove(info)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_mdns_module_state():
    """`_zeroconf`/`_service_info` are module-level (see tiro/mdns.py
    docstring) -- reset around every test so registration state never
    leaks between tests."""
    mdns_mod._zeroconf = None
    mdns_mod._service_info = None
    yield
    mdns_mod._zeroconf = None
    mdns_mod._service_info = None


# --- tiro/mdns.py unit tests ------------------------------------------------


def test_register_mdns_builds_correct_service_info(monkeypatch):
    fake_zc = FakeZeroconf()
    monkeypatch.setattr(mdns_mod, "Zeroconf", lambda **kw: fake_zc)
    monkeypatch.setattr(mdns_mod, "ServiceInfo", FakeServiceInfo)
    monkeypatch.setattr(mdns_mod, "_detect_lan_ips", lambda: ["192.168.1.50"])

    config = TiroConfig(mdns_enabled=True, mdns_hostname="tiro")
    assert mdns_mod.register_mdns(config, host="0.0.0.0", port=8000) is True

    assert len(fake_zc.registered) == 1
    info = fake_zc.registered[0]
    assert info.name == "tiro._http._tcp.local."
    assert info.server == "tiro.local."
    assert info.port == 8000
    assert info.addresses == [socket.inet_aton("192.168.1.50")]


def test_register_mdns_folds_in_concrete_host(monkeypatch):
    """A concrete effective bind host (not 0.0.0.0/loopback) is unioned into
    the advertised addresses alongside `_detect_lan_ips()`."""
    fake_zc = FakeZeroconf()
    monkeypatch.setattr(mdns_mod, "Zeroconf", lambda **kw: fake_zc)
    monkeypatch.setattr(mdns_mod, "ServiceInfo", FakeServiceInfo)
    monkeypatch.setattr(mdns_mod, "_detect_lan_ips", lambda: ["192.168.1.50"])

    config = TiroConfig(mdns_enabled=True, mdns_hostname="tiro")
    mdns_mod.register_mdns(config, host="10.0.0.9", port=8000)

    addrs = set(fake_zc.registered[0].addresses)
    assert addrs == {socket.inet_aton("192.168.1.50"), socket.inet_aton("10.0.0.9")}


def test_register_mdns_wildcard_host_not_duplicated(monkeypatch):
    fake_zc = FakeZeroconf()
    monkeypatch.setattr(mdns_mod, "Zeroconf", lambda **kw: fake_zc)
    monkeypatch.setattr(mdns_mod, "ServiceInfo", FakeServiceInfo)
    monkeypatch.setattr(mdns_mod, "_detect_lan_ips", lambda: ["192.168.1.50"])

    config = TiroConfig(mdns_enabled=True)
    mdns_mod.register_mdns(config, host="0.0.0.0", port=8000)

    assert fake_zc.registered[0].addresses == [socket.inet_aton("192.168.1.50")]


def test_register_mdns_collision_retries_with_suffix(monkeypatch):
    class CollidingZeroconf(FakeZeroconf):
        def register_service(self, info):
            if not info.name.startswith("tiro-2."):
                raise Exception("NonUniqueNameException: name collision")
            super().register_service(info)

    fake_zc = CollidingZeroconf()
    monkeypatch.setattr(mdns_mod, "Zeroconf", lambda **kw: fake_zc)
    monkeypatch.setattr(mdns_mod, "ServiceInfo", FakeServiceInfo)
    monkeypatch.setattr(mdns_mod, "_detect_lan_ips", lambda: [])

    config = TiroConfig(mdns_enabled=True, mdns_hostname="tiro")
    assert mdns_mod.register_mdns(config, host="0.0.0.0", port=8000) is True
    assert len(fake_zc.registered) == 1
    assert fake_zc.registered[0].name == "tiro-2._http._tcp.local."
    assert fake_zc.registered[0].server == "tiro-2.local."


def test_register_mdns_gives_up_quietly_after_retry_fails(monkeypatch):
    class AlwaysFailZeroconf(FakeZeroconf):
        def register_service(self, info):
            raise Exception("still colliding")

    fake_zc = AlwaysFailZeroconf()
    monkeypatch.setattr(mdns_mod, "Zeroconf", lambda **kw: fake_zc)
    monkeypatch.setattr(mdns_mod, "ServiceInfo", FakeServiceInfo)
    monkeypatch.setattr(mdns_mod, "_detect_lan_ips", lambda: [])

    config = TiroConfig(mdns_enabled=True, mdns_hostname="tiro")
    # Must never raise -- gives up quietly.
    assert mdns_mod.register_mdns(config, host="0.0.0.0", port=8000) is False
    assert fake_zc.closed is True


def test_register_mdns_zeroconf_constructor_failure_isolated(monkeypatch):
    def boom(**kwargs):
        raise OSError("no multicast support in this sandbox")

    monkeypatch.setattr(mdns_mod, "Zeroconf", boom)
    config = TiroConfig(mdns_enabled=True)
    assert mdns_mod.register_mdns(config, host="0.0.0.0", port=8000) is False


def test_register_mdns_malformed_host_isolated(monkeypatch):
    """A non-IP `host` string blows up `socket.inet_aton()` inside
    `_build_service_info` -- must be caught by the same per-candidate
    try/except as `register_service` failures, not escape `register_mdns`
    uncaught (regression test: an earlier draft built ServiceInfo outside
    the try block)."""
    fake_zc = FakeZeroconf()
    monkeypatch.setattr(mdns_mod, "Zeroconf", lambda **kw: fake_zc)
    monkeypatch.setattr(mdns_mod, "ServiceInfo", FakeServiceInfo)
    monkeypatch.setattr(mdns_mod, "_detect_lan_ips", lambda: [])

    config = TiroConfig(mdns_enabled=True)
    assert mdns_mod.register_mdns(config, host="not-an-ip-address", port=8000) is False
    assert fake_zc.registered == []
    assert fake_zc.closed is True


def test_register_mdns_idempotent(monkeypatch):
    constructed = []

    def make_zc(**kwargs):
        zc = FakeZeroconf()
        constructed.append(zc)
        return zc

    monkeypatch.setattr(mdns_mod, "Zeroconf", make_zc)
    monkeypatch.setattr(mdns_mod, "ServiceInfo", FakeServiceInfo)
    monkeypatch.setattr(mdns_mod, "_detect_lan_ips", lambda: [])

    config = TiroConfig(mdns_enabled=True)
    assert mdns_mod.register_mdns(config, host="0.0.0.0", port=8000) is True
    assert mdns_mod.register_mdns(config, host="0.0.0.0", port=8000) is True
    assert len(constructed) == 1  # second call is a no-op, not a re-registration


def test_unregister_mdns_calls_unregister_and_close(monkeypatch):
    fake_zc = FakeZeroconf()
    monkeypatch.setattr(mdns_mod, "Zeroconf", lambda **kw: fake_zc)
    monkeypatch.setattr(mdns_mod, "ServiceInfo", FakeServiceInfo)
    monkeypatch.setattr(mdns_mod, "_detect_lan_ips", lambda: [])

    config = TiroConfig(mdns_enabled=True)
    mdns_mod.register_mdns(config, host="0.0.0.0", port=8000)
    assert len(fake_zc.registered) == 1

    mdns_mod.unregister_mdns()
    assert fake_zc.registered == []
    assert fake_zc.closed is True
    # Idempotent: calling again when nothing is registered must not raise.
    mdns_mod.unregister_mdns()


def test_unregister_mdns_noop_when_never_registered():
    mdns_mod.unregister_mdns()  # must not raise


def test_unregister_mdns_isolates_failures(monkeypatch):
    class FailingZeroconf(FakeZeroconf):
        def unregister_service(self, info):
            raise Exception("boom")

        def close(self):
            raise Exception("boom again")

    fake_zc = FailingZeroconf()
    monkeypatch.setattr(mdns_mod, "Zeroconf", lambda **kw: fake_zc)
    monkeypatch.setattr(mdns_mod, "ServiceInfo", FakeServiceInfo)
    monkeypatch.setattr(mdns_mod, "_detect_lan_ips", lambda: [])

    config = TiroConfig(mdns_enabled=True)
    mdns_mod.register_mdns(config, host="0.0.0.0", port=8000)
    mdns_mod.unregister_mdns()  # both failures logged, never raised


# --- tiro/app.py lifespan wiring -------------------------------------------


def _build_lan_config(tmp_path, *, host, mdns_enabled, mdns_hostname="tiro"):
    from tiro import auth as tiro_auth
    from tiro.database import init_db, migrate_db
    from tiro.vectorstore import init_vectorstore

    config = TiroConfig(
        library_path=str(tmp_path / "lib"),
        host=host,
        mdns_enabled=mdns_enabled,
        mdns_hostname=mdns_hostname,
    )
    config.articles_dir.mkdir(parents=True, exist_ok=True)
    (config.library / "audio").mkdir(parents=True, exist_ok=True)
    init_db(config.db_path)
    migrate_db(config.db_path)
    init_vectorstore(config.chroma_dir, config.default_embedding_model)
    config.auth_password_hash = tiro_auth.hash_password("pw")
    return config


def test_lifespan_registers_when_enabled_and_lan(tmp_path, monkeypatch, _shared_embeddings):
    import tiro.app as app_mod

    config = _build_lan_config(tmp_path, host="0.0.0.0", mdns_enabled=True)
    monkeypatch.setattr(app_mod, "_detect_lan_ips", lambda: ["192.168.1.50"])

    calls = []
    monkeypatch.setattr(
        mdns_mod, "register_mdns",
        lambda cfg, host, port: calls.append(("register", host, port)) or True,
    )
    monkeypatch.setattr(mdns_mod, "unregister_mdns", lambda: calls.append(("unregister",)))

    app = app_mod.create_app(config)
    with TestClient(app, base_url="http://localhost"):
        assert calls == [("register", "0.0.0.0", config.port)]
    assert calls[-1] == ("unregister",)


def test_lifespan_calls_mdns_via_to_thread(tmp_path, monkeypatch, _shared_embeddings):
    """Structural regression test for the mdns event-loop deadlock (Phase 3
    M3.0 fix wave 1, CRITICAL finding): tiro/app.py's lifespan MUST invoke
    register_mdns/unregister_mdns via `asyncio.to_thread(...)`, never call
    them directly on the event-loop thread. Calling zeroconf's blocking
    register_service()/unregister_service() directly from this coroutine
    self-deadlocks (Zeroconf autodetects and attaches to the running loop,
    then blocks that same loop's thread waiting on a callback that can only
    run once this coroutine yields) -- fully-mocked functional tests like
    `test_lifespan_registers_when_enabled_and_lan` above cannot see this
    because they never exercise real zeroconf/asyncio interaction.

    This probes the call-site wiring instead: a worker thread spawned by
    `asyncio.to_thread` has no running asyncio loop, so
    `asyncio.get_running_loop()` raises RuntimeError there. Called directly
    from the lifespan coroutine (the bug this guards against), the
    surrounding event loop IS considered "running" on that same OS thread
    while the coroutine's synchronous code executes, so
    `get_running_loop()` would succeed. DO NOT add a test that touches real
    zeroconf/sockets to reproduce the deadlock itself in CI.
    """
    import tiro.app as app_mod

    config = _build_lan_config(tmp_path, host="0.0.0.0", mdns_enabled=True)
    monkeypatch.setattr(app_mod, "_detect_lan_ips", lambda: ["192.168.1.50"])

    ran_off_loop_thread = {"register": None, "unregister": None}

    def probe_register(cfg, host, port):
        try:
            asyncio.get_running_loop()
            ran_off_loop_thread["register"] = False
        except RuntimeError:
            ran_off_loop_thread["register"] = True
        return True

    def probe_unregister():
        try:
            asyncio.get_running_loop()
            ran_off_loop_thread["unregister"] = False
        except RuntimeError:
            ran_off_loop_thread["unregister"] = True

    monkeypatch.setattr(mdns_mod, "register_mdns", probe_register)
    monkeypatch.setattr(mdns_mod, "unregister_mdns", probe_unregister)

    app = app_mod.create_app(config)
    with TestClient(app, base_url="http://localhost"):
        pass

    assert ran_off_loop_thread["register"] is True, (
        "register_mdns ran on the event-loop thread -- must be wrapped in "
        "asyncio.to_thread(...) in tiro/app.py's lifespan (self-deadlock risk)"
    )
    assert ran_off_loop_thread["unregister"] is True, (
        "unregister_mdns ran on the event-loop thread -- must be wrapped in "
        "asyncio.to_thread(...) in tiro/app.py's lifespan (self-deadlock risk)"
    )


def test_lifespan_skips_registration_when_disabled(tmp_path, monkeypatch, _shared_embeddings):
    import tiro.app as app_mod

    config = _build_lan_config(tmp_path, host="0.0.0.0", mdns_enabled=False)
    monkeypatch.setattr(app_mod, "_detect_lan_ips", lambda: ["192.168.1.50"])

    calls = []
    monkeypatch.setattr(
        mdns_mod, "register_mdns", lambda *a, **k: calls.append("register") or True
    )
    monkeypatch.setattr(mdns_mod, "unregister_mdns", lambda: calls.append("unregister"))

    app = app_mod.create_app(config)
    with TestClient(app, base_url="http://localhost"):
        assert "register" not in calls
    # Unregister is unconditional/idempotent -- fine for it to still fire.
    assert calls.count("register") == 0


def test_lifespan_skips_registration_when_loopback(tmp_path, monkeypatch, _shared_embeddings):
    import tiro.app as app_mod

    config = _build_lan_config(tmp_path, host="127.0.0.1", mdns_enabled=True)

    calls = []
    monkeypatch.setattr(
        mdns_mod, "register_mdns", lambda *a, **k: calls.append("register") or True
    )
    monkeypatch.setattr(mdns_mod, "unregister_mdns", lambda: calls.append("unregister"))

    app = app_mod.create_app(config)
    assert app.state.lan_mode is False
    with TestClient(app, base_url="http://localhost"):
        assert "register" not in calls


def test_lifespan_survives_registration_failure(tmp_path, monkeypatch, _shared_embeddings):
    """A raising register_mdns (defense in depth beyond mdns.py's own
    try/except) must never prevent the app from starting."""
    import tiro.app as app_mod

    config = _build_lan_config(tmp_path, host="0.0.0.0", mdns_enabled=True)
    monkeypatch.setattr(app_mod, "_detect_lan_ips", lambda: ["192.168.1.50"])

    def raise_register(*a, **k):
        raise RuntimeError("zeroconf exploded")

    monkeypatch.setattr(mdns_mod, "register_mdns", raise_register)

    app = app_mod.create_app(config)
    with TestClient(app, base_url="http://localhost") as c:
        assert c.get("/healthz").status_code == 200


def test_lifespan_survives_unregister_failure(tmp_path, monkeypatch, _shared_embeddings):
    import tiro.app as app_mod

    config = _build_lan_config(tmp_path, host="0.0.0.0", mdns_enabled=True)
    monkeypatch.setattr(app_mod, "_detect_lan_ips", lambda: ["192.168.1.50"])
    monkeypatch.setattr(mdns_mod, "register_mdns", lambda *a, **k: True)

    def raise_unregister():
        raise RuntimeError("zeroconf exploded on the way out")

    monkeypatch.setattr(mdns_mod, "unregister_mdns", raise_unregister)

    app = app_mod.create_app(config)
    with TestClient(app, base_url="http://localhost"):
        pass  # shutdown must not raise despite unregister_mdns blowing up


# --- config -----------------------------------------------------------------


def test_mdns_and_remote_url_defaults():
    config = TiroConfig()
    assert config.mdns_enabled is False
    assert config.mdns_hostname == "tiro"
    assert config.remote_url is None


def test_env_overlay_mdns_and_remote_url(tmp_path, monkeypatch):
    from tiro.config import load_config

    monkeypatch.setenv("TIRO_MDNS_ENABLED", "true")
    monkeypatch.setenv("TIRO_MDNS_HOSTNAME", "envhost")
    monkeypatch.setenv("TIRO_REMOTE_URL", "https://example.ts.net")
    config = load_config(tmp_path / "none.yaml")
    assert config.mdns_enabled is True
    assert config.mdns_hostname == "envhost"
    assert config.remote_url == "https://example.ts.net"


# --- `tiro status` -----------------------------------------------------------


def test_cli_status_mdns_disabled_and_remote_unset(initialized_library, capsys):
    from types import SimpleNamespace

    from tiro import cli

    cli.cmd_status(SimpleNamespace(config="unused", _config_override=initialized_library))
    out = capsys.readouterr().out
    assert "mDNS: disabled" in out
    assert "Remote URL: not set" in out


def test_cli_status_mdns_enabled_and_remote_set(initialized_library, capsys):
    from types import SimpleNamespace

    from tiro import cli

    initialized_library.mdns_enabled = True
    initialized_library.mdns_hostname = "myhouse"
    initialized_library.remote_url = "https://100.64.1.2:8000"

    cli.cmd_status(SimpleNamespace(config="unused", _config_override=initialized_library))
    out = capsys.readouterr().out
    assert "mDNS: on (myhouse.local)" in out
    assert "Remote URL: https://100.64.1.2:8000" in out
