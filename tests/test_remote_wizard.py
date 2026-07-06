"""Phase 3 M3.1 Task 4: /setup/remote wizard (Tailscale detection, the
extra_allowed_hosts Host-allowlist extension, trust_proxy_headers scheme
trust, and the config/test wizard endpoints).

Zeroconf/mDNS is untouched by this task -- see tests/test_mdns.py for that.
This file covers config plumbing, the Host-header allowlist extension, the
X-Forwarded-Proto scheme trust in `_login_qr_target`, Tailscale detection
(fully mocked -- no real `tailscale` binary touched), and the three
/api/remote/* endpoints.
"""

import json
import subprocess

from fastapi.testclient import TestClient

from tiro.config import TiroConfig

TEST_PASSWORD = "test-password-123"


def _build_remote_config(
    tmp_path, *, extra_allowed_hosts=None, trust_proxy_headers=False, with_password=True
):
    """Mirrors test_mdns.py's `_build_lan_config` helper: a fully
    initialized library (SQLite + ChromaDB + articles dir) with a real
    config.yaml on disk (so persist_config-backed endpoints have somewhere
    to write), independent of the conftest fixtures so extra_allowed_hosts/
    trust_proxy_headers can be parameterized per test.
    """
    from tiro import auth as tiro_auth
    from tiro.database import init_db, migrate_db
    from tiro.vectorstore import init_vectorstore

    library_path = tmp_path / "lib"
    config = TiroConfig(
        library_path=str(library_path),
        extra_allowed_hosts=extra_allowed_hosts or [],
        trust_proxy_headers=trust_proxy_headers,
    )
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f'library_path: "{library_path}"\n')
    config.config_path = str(cfg_file)
    config.articles_dir.mkdir(parents=True, exist_ok=True)
    (config.library / "audio").mkdir(parents=True, exist_ok=True)
    init_db(config.db_path)
    migrate_db(config.db_path)
    init_vectorstore(config.chroma_dir, config.default_embedding_model)
    if with_password:
        config.auth_password_hash = tiro_auth.hash_password(TEST_PASSWORD)
    return config


# --- config: fields, env overlay, persist round-trip -----------------------


def test_extra_allowed_hosts_and_trust_proxy_headers_defaults():
    config = TiroConfig()
    assert config.extra_allowed_hosts == []
    assert config.trust_proxy_headers is False


def test_env_overlay_extra_allowed_hosts_comma_separated(tmp_path, monkeypatch):
    from tiro.config import load_config

    monkeypatch.setenv(
        "TIRO_EXTRA_ALLOWED_HOSTS",
        "host-a.example.com, host-b.example.com:9000 ,,host-c.example.com",
    )
    config = load_config(tmp_path / "none.yaml")
    assert config.extra_allowed_hosts == [
        "host-a.example.com", "host-b.example.com:9000", "host-c.example.com",
    ]


def test_env_overlay_extra_allowed_hosts_empty_string_yields_empty_list(tmp_path, monkeypatch):
    from tiro.config import load_config

    monkeypatch.setenv("TIRO_EXTRA_ALLOWED_HOSTS", "")
    config = load_config(tmp_path / "none.yaml")
    assert config.extra_allowed_hosts == []


def test_env_overlay_trust_proxy_headers(tmp_path, monkeypatch):
    from tiro.config import load_config

    monkeypatch.setenv("TIRO_TRUST_PROXY_HEADERS", "true")
    config = load_config(tmp_path / "none.yaml")
    assert config.trust_proxy_headers is True


def test_persist_config_round_trips_extra_allowed_hosts_and_trust_proxy_headers(tmp_path):
    from tiro.config import load_config, persist_config

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text('library_path: "./lib"\n')
    config = load_config(cfg_file)
    persist_config(
        config,
        {"extra_allowed_hosts": ["proxy.example.com"], "trust_proxy_headers": True},
    )
    reloaded = load_config(cfg_file)
    assert reloaded.extra_allowed_hosts == ["proxy.example.com"]
    assert reloaded.trust_proxy_headers is True


# --- Host allowlist: extra_allowed_hosts effect -----------------------------


def test_extra_allowed_host_passes_bare_form(tmp_path, _shared_embeddings):
    from tiro.app import create_app

    config = _build_remote_config(
        tmp_path, extra_allowed_hosts=["myhost.example.com"], with_password=False
    )
    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as client:
        ok = client.get("/healthz", headers={"Host": "myhost.example.com"})
        assert ok.status_code == 200


def test_extra_allowed_host_bare_entry_also_matches_with_default_port(tmp_path, _shared_embeddings):
    from tiro.app import create_app

    config = _build_remote_config(
        tmp_path, extra_allowed_hosts=["myhost.example.com"], with_password=False
    )
    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as client:
        ok = client.get("/healthz", headers={"Host": f"myhost.example.com:{config.port}"})
        assert ok.status_code == 200


def test_host_not_in_extra_allowed_hosts_still_rejected(tmp_path, _shared_embeddings):
    from tiro.app import create_app

    config = _build_remote_config(
        tmp_path, extra_allowed_hosts=["myhost.example.com"], with_password=False
    )
    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as client:
        rejected = client.get("/healthz", headers={"Host": "evil.example.com"})
        assert rejected.status_code == 400


def test_extra_allowed_host_match_is_case_insensitive(tmp_path, _shared_embeddings):
    from tiro.app import create_app

    config = _build_remote_config(
        tmp_path, extra_allowed_hosts=["myhost.example.com"], with_password=False
    )
    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as client:
        ok = client.get("/healthz", headers={"Host": "MYHOST.EXAMPLE.COM"})
        assert ok.status_code == 200


def test_extra_allowed_host_with_explicit_port_matches_only_that_exact_form(
    tmp_path, _shared_embeddings
):
    """An entry that already carries its own port (e.g. a reverse proxy on a
    non-standard port) is matched ONLY in that exact form -- config.port is
    not additionally appended on top of an already-porty entry."""
    from tiro.app import create_app

    config = _build_remote_config(
        tmp_path, extra_allowed_hosts=["proxy.example.com:9443"], with_password=False
    )
    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as client:
        ok = client.get("/healthz", headers={"Host": "proxy.example.com:9443"})
        assert ok.status_code == 200

        rejected_bare = client.get("/healthz", headers={"Host": "proxy.example.com"})
        assert rejected_bare.status_code == 400

        rejected_default_port = client.get(
            "/healthz", headers={"Host": f"proxy.example.com:{config.port}"}
        )
        assert rejected_default_port.status_code == 400


def test_no_extra_allowed_hosts_by_default_still_rejects_arbitrary_host(tmp_path, _shared_embeddings):
    from tiro.app import create_app

    config = _build_remote_config(tmp_path, with_password=False)
    app = create_app(config)
    with TestClient(app, base_url="http://localhost") as client:
        rejected = client.get("/healthz", headers={"Host": "myhost.example.com"})
        assert rejected.status_code == 400


# --- trust_proxy_headers: X-Forwarded-Proto scheme trust in the QR page ----


def _login(client):
    r = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert r.status_code == 200


def test_qr_scheme_ignores_forwarded_proto_by_default(tmp_path, _shared_embeddings):
    from tiro.app import create_app

    config = _build_remote_config(tmp_path, trust_proxy_headers=False)
    app = create_app(config)
    with TestClient(app, base_url="http://localhost", follow_redirects=False) as client:
        _login(client)
        r = client.get("/setup/qr", headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 200
        assert "http://localhost/login/qr?token=" in r.text
        assert "https://localhost/login/qr?token=" not in r.text


def test_qr_scheme_honors_forwarded_proto_when_trusted(tmp_path, _shared_embeddings):
    from tiro.app import create_app

    config = _build_remote_config(tmp_path, trust_proxy_headers=True)
    app = create_app(config)
    with TestClient(app, base_url="http://localhost", follow_redirects=False) as client:
        _login(client)
        r = client.get("/setup/qr", headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 200
        assert "https://localhost/login/qr?token=" in r.text


def test_qr_scheme_ignores_garbage_forwarded_proto_even_when_trusted(tmp_path, _shared_embeddings):
    from tiro.app import create_app

    config = _build_remote_config(tmp_path, trust_proxy_headers=True)
    app = create_app(config)
    with TestClient(app, base_url="http://localhost", follow_redirects=False) as client:
        _login(client)
        r = client.get("/setup/qr", headers={"X-Forwarded-Proto": "ftp"})
        assert r.status_code == 200
        assert "http://localhost/login/qr?token=" in r.text


def test_qr_scheme_takes_first_value_of_comma_separated_forwarded_proto(tmp_path, _shared_embeddings):
    from tiro.app import create_app

    config = _build_remote_config(tmp_path, trust_proxy_headers=True)
    app = create_app(config)
    with TestClient(app, base_url="http://localhost", follow_redirects=False) as client:
        _login(client)
        r = client.get("/setup/qr", headers={"X-Forwarded-Proto": "https, http"})
        assert r.status_code == 200
        assert "https://localhost/login/qr?token=" in r.text


def test_qr_never_trusts_forwarded_host_even_when_proxy_headers_trusted(tmp_path, _shared_embeddings):
    """X-Forwarded-Host must never override the real Host header -- Host
    validation (and the host half of the QR target URL) stays anchored to
    the real Host header regardless of trust_proxy_headers."""
    from tiro.app import create_app

    config = _build_remote_config(tmp_path, trust_proxy_headers=True)
    app = create_app(config)
    with TestClient(app, base_url="http://localhost", follow_redirects=False) as client:
        _login(client)
        r = client.get("/setup/qr", headers={"X-Forwarded-Host": "evil.example.com"})
        assert r.status_code == 200
        assert "evil.example.com" not in r.text
        assert "localhost/login/qr?token=" in r.text


# --- Tailscale detection (_detect_tailscale) -- fully mocked, never raises -


def test_detect_tailscale_binary_not_found(monkeypatch):
    from tiro.api import routes_remote

    monkeypatch.setattr(routes_remote.shutil, "which", lambda name: None)
    result = routes_remote._detect_tailscale(8000)
    assert result == {"tailscale_installed": False, "magicdns_name": None, "serve_command": None}


def test_detect_tailscale_success_parses_magicdns_name(monkeypatch):
    from tiro.api import routes_remote

    monkeypatch.setattr(routes_remote.shutil, "which", lambda name: "/usr/bin/tailscale")
    fake_result = subprocess.CompletedProcess(
        args=["tailscale", "status", "--json"],
        returncode=0,
        stdout=json.dumps({"Self": {"DNSName": "myhost.tailnet-name.ts.net."}}),
    )
    monkeypatch.setattr(routes_remote.subprocess, "run", lambda *a, **k: fake_result)
    result = routes_remote._detect_tailscale(8000)
    assert result == {
        "tailscale_installed": True,
        "magicdns_name": "myhost.tailnet-name.ts.net",
        "serve_command": "tailscale serve --bg 8000",
    }


def test_detect_tailscale_serve_command_uses_given_port(monkeypatch):
    from tiro.api import routes_remote

    monkeypatch.setattr(routes_remote.shutil, "which", lambda name: "/usr/bin/tailscale")
    fake_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
    monkeypatch.setattr(routes_remote.subprocess, "run", lambda *a, **k: fake_result)
    result = routes_remote._detect_tailscale(9999)
    assert result["serve_command"] == "tailscale serve --bg 9999"


def test_detect_tailscale_status_timeout_degrades_gracefully(monkeypatch):
    from tiro.api import routes_remote

    monkeypatch.setattr(routes_remote.shutil, "which", lambda name: "/usr/bin/tailscale")

    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="tailscale", timeout=3)

    monkeypatch.setattr(routes_remote.subprocess, "run", raise_timeout)
    result = routes_remote._detect_tailscale(8000)
    assert result["tailscale_installed"] is True
    assert result["magicdns_name"] is None
    assert result["serve_command"] == "tailscale serve --bg 8000"


def test_detect_tailscale_garbage_json_degrades_gracefully(monkeypatch):
    from tiro.api import routes_remote

    monkeypatch.setattr(routes_remote.shutil, "which", lambda name: "/usr/bin/tailscale")
    fake_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json at all")
    monkeypatch.setattr(routes_remote.subprocess, "run", lambda *a, **k: fake_result)
    result = routes_remote._detect_tailscale(8000)
    assert result["tailscale_installed"] is True
    assert result["magicdns_name"] is None


def test_detect_tailscale_nonzero_returncode_degrades_gracefully(monkeypatch):
    from tiro.api import routes_remote

    monkeypatch.setattr(routes_remote.shutil, "which", lambda name: "/usr/bin/tailscale")
    fake_result = subprocess.CompletedProcess(args=[], returncode=1, stdout="")
    monkeypatch.setattr(routes_remote.subprocess, "run", lambda *a, **k: fake_result)
    result = routes_remote._detect_tailscale(8000)
    assert result["tailscale_installed"] is True
    assert result["magicdns_name"] is None


def test_detect_tailscale_missing_self_or_dnsname_key_degrades_gracefully(monkeypatch):
    from tiro.api import routes_remote

    monkeypatch.setattr(routes_remote.shutil, "which", lambda name: "/usr/bin/tailscale")
    fake_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps({}))
    monkeypatch.setattr(routes_remote.subprocess, "run", lambda *a, **k: fake_result)
    result = routes_remote._detect_tailscale(8000)
    assert result["tailscale_installed"] is True
    assert result["magicdns_name"] is None


# --- GET /api/remote/status --------------------------------------------------


def test_get_remote_status_requires_auth(auth_client):
    r = auth_client.get("/api/remote/status")
    assert r.status_code == 401


def test_get_remote_status_returns_detection_and_saved_remote_url(authenticated_client, monkeypatch):
    from tiro.api import routes_remote

    monkeypatch.setattr(
        routes_remote,
        "_detect_tailscale",
        lambda port: {
            "tailscale_installed": True,
            "magicdns_name": "myhost.ts.net",
            "serve_command": f"tailscale serve --bg {port}",
        },
    )
    r = authenticated_client.get("/api/remote/status")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["tailscale_installed"] is True
    assert data["magicdns_name"] == "myhost.ts.net"
    assert data["remote_url"] is None
    assert "tailscale serve --bg" in data["serve_command"]


# --- POST /api/remote/config -------------------------------------------------


def test_post_remote_config_requires_auth(auth_client):
    r = auth_client.post("/api/remote/config", json={"remote_url": "https://example.com"})
    assert r.status_code == 401


def test_post_remote_config_rejects_invalid_scheme(authenticated_client):
    r = authenticated_client.post(
        "/api/remote/config", json={"remote_url": "ftp://example.com", "allow_hostname": False}
    )
    assert r.status_code == 400


def test_post_remote_config_rejects_url_with_no_netloc(authenticated_client):
    r = authenticated_client.post(
        "/api/remote/config", json={"remote_url": "https://", "allow_hostname": False}
    )
    assert r.status_code == 400


def test_post_remote_config_rejects_hostless_authority(authenticated_client):
    """"https://:8000" has a non-empty netloc (":8000") so `not parsed.netloc`
    alone doesn't catch it -- but urlparse's `.hostname` is None (nothing
    precedes the port), so it must still 400 rather than persist as a
    remote_url nothing can resolve to."""
    r = authenticated_client.post(
        "/api/remote/config", json={"remote_url": "https://:8000", "allow_hostname": False}
    )
    assert r.status_code == 400


def test_post_remote_config_persists_remote_url_without_touching_allowlist(
    authenticated_client, configured_library
):
    r = authenticated_client.post(
        "/api/remote/config",
        json={"remote_url": "https://myhost.example.com", "allow_hostname": False},
    )
    assert r.status_code == 200
    assert r.json()["data"]["remote_url"] == "https://myhost.example.com"
    assert r.json()["data"]["extra_allowed_hosts"] == []

    from tiro.config import load_config

    reloaded = load_config(configured_library.config_path)
    assert reloaded.remote_url == "https://myhost.example.com"
    assert reloaded.extra_allowed_hosts == []


def test_post_remote_config_allow_hostname_persists_bare_hostname_and_dedupes(
    authenticated_client, configured_library
):
    r1 = authenticated_client.post(
        "/api/remote/config",
        json={"remote_url": "https://myhost.example.com", "allow_hostname": True},
    )
    assert r1.status_code == 200
    assert r1.json()["data"]["extra_allowed_hosts"] == ["myhost.example.com"]

    # Same hostname again (different port in the URL) must not duplicate.
    r2 = authenticated_client.post(
        "/api/remote/config",
        json={"remote_url": "https://myhost.example.com:9000", "allow_hostname": True},
    )
    assert r2.status_code == 200
    assert r2.json()["data"]["extra_allowed_hosts"] == ["myhost.example.com"]

    from tiro.config import load_config

    reloaded = load_config(configured_library.config_path)
    assert reloaded.extra_allowed_hosts == ["myhost.example.com"]


def test_post_remote_config_allow_hostname_updates_live_allowlist_without_restart(
    authenticated_client,
):
    """The whole point of the static-vs-dynamic app.state design (see
    tiro/app.py's create_app comment): a hostname newly allowed via this
    endpoint must pass the Host allowlist on the VERY NEXT request against
    the SAME running app/client -- no new TestClient, no restart."""
    before = authenticated_client.get("/healthz", headers={"Host": "myhost.example.com"})
    assert before.status_code == 400

    r = authenticated_client.post(
        "/api/remote/config",
        json={"remote_url": "https://myhost.example.com", "allow_hostname": True},
    )
    assert r.status_code == 200

    after = authenticated_client.get("/healthz", headers={"Host": "myhost.example.com"})
    assert after.status_code == 200


# --- POST /api/remote/test ---------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def _fake_async_client_factory(head_impl, capture=None):
    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            if capture is not None:
                capture.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def head(self, url):
            return await head_impl(url)

    return FakeAsyncClient


def test_post_remote_test_requires_auth(auth_client):
    r = auth_client.post("/api/remote/test", json={"url": "https://example.com"})
    assert r.status_code == 401


def test_post_remote_test_rejects_invalid_scheme(authenticated_client):
    r = authenticated_client.post("/api/remote/test", json={"url": "ftp://example.com"})
    assert r.status_code == 400


def test_post_remote_test_requires_url_or_saved_remote_url(authenticated_client):
    r = authenticated_client.post("/api/remote/test", json={"url": None})
    assert r.status_code == 400


def test_post_remote_test_uses_saved_remote_url_when_no_url_given(authenticated_client, monkeypatch):
    from tiro.api import routes_remote

    authenticated_client.post(
        "/api/remote/config",
        json={"remote_url": "https://saved.example.com", "allow_hostname": False},
    )

    requested = {}

    async def head_impl(url):
        requested["url"] = url
        return _FakeResponse(200)

    monkeypatch.setattr(
        routes_remote.httpx, "AsyncClient", _fake_async_client_factory(head_impl)
    )
    r = authenticated_client.post("/api/remote/test", json={"url": None})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["ok"] is True
    assert data["status_code"] == 200
    assert requested["url"] == "https://saved.example.com"


def test_post_remote_test_ok_response_reports_status_and_latency(authenticated_client, monkeypatch):
    from tiro.api import routes_remote

    async def head_impl(url):
        return _FakeResponse(200)

    monkeypatch.setattr(
        routes_remote.httpx, "AsyncClient", _fake_async_client_factory(head_impl)
    )
    r = authenticated_client.post("/api/remote/test", json={"url": "https://example.com"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data == {"ok": True, "status_code": 200, "latency_ms": data["latency_ms"], "error": None}
    assert isinstance(data["latency_ms"], int)
    assert data["latency_ms"] >= 0


def test_post_remote_test_error_status_code_marks_not_ok_without_raising(
    authenticated_client, monkeypatch
):
    from tiro.api import routes_remote

    async def head_impl(url):
        return _FakeResponse(502)

    monkeypatch.setattr(
        routes_remote.httpx, "AsyncClient", _fake_async_client_factory(head_impl)
    )
    r = authenticated_client.post("/api/remote/test", json={"url": "https://example.com"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["ok"] is False
    assert data["status_code"] == 502
    assert data["error"] is None


def test_post_remote_test_timeout_path(authenticated_client, monkeypatch):
    from tiro.api import routes_remote

    async def head_impl(url):
        raise routes_remote.httpx.TimeoutException("timed out")

    monkeypatch.setattr(
        routes_remote.httpx, "AsyncClient", _fake_async_client_factory(head_impl)
    )
    r = authenticated_client.post("/api/remote/test", json={"url": "https://slow.example.com"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data == {"ok": False, "status_code": None, "latency_ms": None, "error": "timeout"}


def test_post_remote_test_too_many_redirects_path(authenticated_client, monkeypatch):
    from tiro.api import routes_remote

    async def head_impl(url):
        raise routes_remote.httpx.TooManyRedirects("too many redirects")

    monkeypatch.setattr(
        routes_remote.httpx, "AsyncClient", _fake_async_client_factory(head_impl)
    )
    r = authenticated_client.post("/api/remote/test", json={"url": "https://redir.example.com"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["ok"] is False
    assert data["error"] == "too_many_redirects"


def test_post_remote_test_passes_timeout_and_redirect_cap_to_httpx(authenticated_client, monkeypatch):
    from tiro.api import routes_remote

    captured: dict = {}

    async def head_impl(url):
        return _FakeResponse(200)

    monkeypatch.setattr(
        routes_remote.httpx,
        "AsyncClient",
        _fake_async_client_factory(head_impl, capture=captured),
    )
    authenticated_client.post("/api/remote/test", json={"url": "https://example.com"})
    assert captured.get("follow_redirects") is True
    assert captured.get("max_redirects") == 3
    assert captured.get("timeout") == 5.0


# --- /setup/remote page ------------------------------------------------------


def test_setup_remote_page_requires_auth(auth_client):
    r = auth_client.get("/setup/remote")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_setup_remote_page_renders_expected_sections(authenticated_client):
    r = authenticated_client.get("/setup/remote")
    assert r.status_code == 200
    assert "Remote Access" in r.text
    assert "Tailscale" in r.text
    assert 'id="remote-tailscale-status"' in r.text
    assert 'id="remote-url-form"' in r.text
    assert 'id="remote-url-input"' in r.text
    assert 'id="remote-url-allow-hostname"' in r.text
    assert 'id="btn-save-remote-url"' in r.text
    assert 'id="btn-test-remote-url"' in r.text
    assert 'href="/setup/qr"' in r.text
    assert "/static/js/remote-setup.js" in r.text


def test_settings_page_links_to_remote_setup(authenticated_client):
    r = authenticated_client.get("/settings")
    assert r.status_code == 200
    assert 'href="/setup/remote"' in r.text


# --- route-walk auto-covers ---------------------------------------------------


def test_new_remote_routes_registered_under_protected_router_list(configured_library):
    """Belt-and-suspenders pin alongside test_auth.py's generic
    test_route_walk_everything_gated: if a future refactor silently drops
    routes_remote's router from the `protected` list in create_app, this
    fails with a specific, legible path list rather than a generic
    "unprotected routes" diff."""
    from tiro.app import create_app

    app = create_app(configured_library)
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/remote/status" in paths
    assert "/api/remote/config" in paths
    assert "/api/remote/test" in paths
    assert "/setup/remote" in paths
