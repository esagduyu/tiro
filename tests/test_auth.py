"""Auth spine tests: config fields, hashing, sessions, tokens, routes, enforcement."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_PASSWORD
from tiro import auth
from tiro.app import create_app
from tiro.config import TiroConfig, load_config
from tiro.database import get_connection, init_db


def test_config_has_auth_fields_default_none(test_config):
    assert test_config.auth_password_hash is None


def test_load_config_records_its_path(tmp_path):
    cfg_file = tmp_path / "custom.yaml"
    cfg_file.write_text("library_path: ./lib\nauth_password_hash: dummy-hash\n")
    cfg = load_config(cfg_file)
    assert cfg.auth_password_hash == "dummy-hash"
    assert Path(cfg.config_path) == cfg_file


def test_load_config_records_path_even_when_missing(tmp_path):
    cfg = load_config(tmp_path / "nonexistent.yaml")
    assert Path(cfg.config_path) == tmp_path / "nonexistent.yaml"


def test_password_hash_roundtrip():
    h = auth.hash_password("correct horse")
    assert h != "correct horse"
    assert auth.verify_password("correct horse", h)
    assert not auth.verify_password("wrong", h)


def test_session_lifecycle(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    token = auth.create_session(db)
    assert auth.validate_session(db, token)
    assert not auth.validate_session(db, "forged-token")
    auth.destroy_session(db, token)
    assert not auth.validate_session(db, token)


def test_session_expiry_and_sliding_renewal(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    token = auth.create_session(db)
    token_hash = auth._sha256(token)

    conn = get_connection(db)
    try:
        # Simulate an expired session
        conn.execute(
            "UPDATE sessions SET expires_at = datetime('now', '-1 day') WHERE token_hash = ?",
            (token_hash,),
        )
        conn.commit()
    finally:
        conn.close()
    assert not auth.validate_session(db, token)

    # Fresh session, aged 10 days: validation must slide expiry forward
    token2 = auth.create_session(db)
    t2_hash = auth._sha256(token2)
    conn = get_connection(db)
    try:
        conn.execute(
            "UPDATE sessions SET expires_at = datetime('now', '+20 days') WHERE token_hash = ?",
            (t2_hash,),
        )
        conn.commit()
    finally:
        conn.close()
    assert auth.validate_session(db, token2)
    conn = get_connection(db)
    try:
        row = conn.execute(
            "SELECT expires_at > datetime('now', '+29 days') AS slid FROM sessions WHERE token_hash = ?",
            (t2_hash,),
        ).fetchone()
    finally:
        conn.close()
    assert row["slid"] == 1


def test_api_token_lifecycle(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    raw = auth.create_api_token(db, "chrome-extension")
    assert auth.validate_api_token(db, raw)
    assert not auth.validate_api_token(db, "forged")
    conn = get_connection(db)
    try:
        row = conn.execute("SELECT name, token_hash FROM api_tokens").fetchone()
    finally:
        conn.close()
    assert row["name"] == "chrome-extension"
    assert row["token_hash"] != raw  # stored hashed, never plaintext


def test_save_password_hash_preserves_comments(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "# Tiro Configuration\n"
        "library_path: \"./tiro-library\"  # where articles live\n"
        "port: 8000\n"
    )
    from tiro.config import load_config

    cfg = load_config(cfg_file)
    auth.save_password_hash(cfg, "bcrypt-hash-here")
    text = cfg_file.read_text()
    assert "# Tiro Configuration" in text          # comments preserved
    assert "# where articles live" in text
    assert "auth_password_hash: bcrypt-hash-here" in text
    assert cfg.auth_password_hash == "bcrypt-hash-here"  # in-memory updated


def test_save_password_hash_failure_leaves_config_intact(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    original = "# precious comments\nlibrary_path: \"./tiro-library\"\n"
    cfg_file.write_text(original)
    cfg = load_config(cfg_file)

    from ruamel.yaml import YAML

    def boom(self, data, stream):
        raise OSError("disk full")

    monkeypatch.setattr(YAML, "dump", boom)
    with pytest.raises(OSError):
        auth.save_password_hash(cfg, "hash")
    assert cfg_file.read_text() == original  # untouched
    assert not cfg_file.with_suffix(".yaml.tmp").exists()  # no litter


def test_healthz_open(auth_client):
    r = auth_client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_wrong_password_401(auth_client):
    r = auth_client.post("/api/auth/login", json={"password": "nope"})
    assert r.status_code == 401
    assert "tiro_session" not in auth_client.cookies


def test_login_sets_session_cookie(auth_client):
    r = auth_client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert r.status_code == 200
    assert auth_client.cookies.get("tiro_session")


def test_logout_destroys_session(auth_client, configured_library):
    auth_client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    token = auth_client.cookies.get("tiro_session")
    assert auth.validate_session(configured_library.db_path, token)
    r = auth_client.post("/api/auth/logout")
    assert r.status_code == 200
    assert not auth.validate_session(configured_library.db_path, token)


def test_status_reports_configured_and_authenticated(auth_client, client):
    r = auth_client.get("/api/auth/status")
    assert r.json()["data"] == {"configured": True, "authenticated": False}
    auth_client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    r = auth_client.get("/api/auth/status")
    assert r.json()["data"] == {"configured": True, "authenticated": True}
    # `client` fixture has NO password configured
    r = client.get("/api/auth/status")
    assert r.json()["data"]["configured"] is False


def test_setup_only_works_once(client, auth_client):
    # Unconfigured app: setup allowed, logs you in
    r = client.post("/api/auth/setup", json={"password": "first-password-8ch"})
    assert r.status_code == 200
    assert client.cookies.get("tiro_session")
    # Configured app: setup refused
    r = auth_client.post("/api/auth/setup", json={"password": "attacker-password"})
    assert r.status_code == 403


def test_setup_rejects_short_password(client):
    r = client.post("/api/auth/setup", json={"password": "short"})
    assert r.status_code == 422


def test_login_page_renders(auth_client):
    r = auth_client.get("/login")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_api_requires_auth_when_configured(auth_client):
    r = auth_client.get("/api/articles")
    assert r.status_code == 401


def test_api_requires_auth_even_when_unconfigured(client):
    # Fail closed: no password yet -> only setup/status/healthz respond
    r = client.get("/api/articles")
    assert r.status_code == 401


def test_pages_redirect_to_login(auth_client):
    r = auth_client.get("/inbox", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_authenticated_client_can_use_api(authenticated_client):
    r = authenticated_client.get("/api/articles")
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_bearer_token_grants_api_access(configured_library):
    raw = auth.create_api_token(configured_library.db_path, "test-client")
    app = create_app(configured_library)
    with TestClient(app, base_url="http://localhost") as c:
        r = c.get("/api/articles", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 200
        r = c.get("/api/articles", headers={"Authorization": "Bearer forged"})
        assert r.status_code == 401


def test_csrf_rejects_cross_origin_cookie_mutation(authenticated_client):
    r = authenticated_client.post(
        "/api/decay/recalculate",
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_csrf_allows_same_origin_mutation(authenticated_client):
    r = authenticated_client.post(
        "/api/decay/recalculate",
        headers={"Origin": "http://localhost"},
    )
    assert r.status_code == 200


def test_session_survives_app_restart(configured_library):
    app1 = create_app(configured_library)
    with TestClient(app1, base_url="http://localhost") as c1:
        c1.post("/api/auth/login", json={"password": TEST_PASSWORD})
        token = c1.cookies.get("tiro_session")
    app2 = create_app(configured_library)
    with TestClient(app2, base_url="http://localhost") as c2:
        c2.cookies.set("tiro_session", token)
        r = c2.get("/api/articles")
        assert r.status_code == 200


def test_set_password_cli(tmp_path, monkeypatch):
    import argparse

    from tiro.cli import cmd_set_password

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("library_path: ./lib\n")

    prompts = iter(["new-password-123", "new-password-123"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(prompts))
    cmd_set_password(argparse.Namespace(config=str(cfg_file)))

    cfg = load_config(cfg_file)
    assert cfg.auth_password_hash
    assert auth.verify_password("new-password-123", cfg.auth_password_hash)


def test_set_password_cli_mismatch_aborts(tmp_path, monkeypatch):
    import argparse

    import pytest as _pytest

    from tiro.cli import cmd_set_password

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("library_path: ./lib\n")
    prompts = iter(["password-one-123", "password-two-456"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(prompts))
    with _pytest.raises(SystemExit):
        cmd_set_password(argparse.Namespace(config=str(cfg_file)))
    assert load_config(cfg_file).auth_password_hash is None


def test_save_password_hash_sets_restrictive_permissions(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("library_path: ./lib\n")
    cfg = load_config(cfg_file)
    auth.save_password_hash(cfg, "some-hash")
    assert (cfg_file.stat().st_mode & 0o777) == 0o600


def test_host_header_validation(auth_client):
    r = auth_client.get("/healthz", headers={"Host": "evil.example:8000"})
    assert r.status_code == 400
    r = auth_client.get("/healthz", headers={"Host": "localhost:8000"})
    assert r.status_code == 200


def test_cross_site_get_rejected_for_cookie_auth(authenticated_client):
    # Sec-Fetch-Site: cross-site = hostile page navigating/fetching our API
    r = authenticated_client.get(
        "/api/digest/today", headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert r.status_code == 403
    # same-origin (the SPA) is untouched
    r = authenticated_client.get(
        "/api/articles", headers={"Sec-Fetch-Site": "same-origin"}
    )
    assert r.status_code == 200


def test_route_walk_everything_gated(auth_client, configured_library):
    """The allowlist as an executable invariant: every registered route outside
    it must refuse an unauthenticated request. Protects future routers too."""
    from tiro.app import create_app

    app = create_app(configured_library)
    ALLOWED_PATHS = {
        "/api/auth/login", "/api/auth/setup", "/api/auth/status",
        "/api/auth/logout",  # idempotent logout, open by design
        "/healthz", "/login", "/",
    }
    ALLOWED_PREFIXES = ("/static", "/library/themes")
    failures = []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        if not path or not methods or path in ALLOWED_PATHS:
            continue
        if path.startswith(ALLOWED_PREFIXES):
            continue
        probe = path.replace("{article_id}", "1").replace("{token_id}", "1")
        probe = probe.replace("{digest_type}", "ranked").replace("{target_date}", "2026-01-01")
        probe = probe.replace("{source_id}", "1").replace("{node_type}", "tag").replace("{node_id}", "1")
        probe = probe.replace("{author_id}", "1")
        probe = probe.replace("{view_id}", "1")
        assert "{" not in probe, f"unsubstituted placeholder in {probe}"
        for method in methods - {"HEAD", "OPTIONS"}:
            r = auth_client.request(method, probe)
            if r.status_code not in (401, 302):
                failures.append(f"{method} {probe} -> {r.status_code}")
    assert not failures, f"Unprotected routes: {failures}"


def test_preflight_with_bad_host_rejected(auth_client):
    r = auth_client.options(
        "/api/articles",
        headers={
            "Host": "evil.example:8000",
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 400


def test_lan_mode_refuses_without_auth(tmp_path, monkeypatch):
    import argparse

    from tiro.cli import cmd_run

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"library_path: {tmp_path / 'lib'}\n")  # no password
    args = argparse.Namespace(
        config=str(cfg_file), lan=True, no_browser=True, insecure_no_auth=False
    )
    with pytest.raises(SystemExit) as exc:
        cmd_run(args)
    assert exc.value.code == 1


def test_mcp_gate_requires_valid_token(configured_library, monkeypatch):
    from tiro.mcp.server import _require_token_gate

    # No env token -> refused
    monkeypatch.delenv("TIRO_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        _require_token_gate(configured_library)

    # Invalid token -> refused
    monkeypatch.setenv("TIRO_API_TOKEN", "forged")
    with pytest.raises(RuntimeError):
        _require_token_gate(configured_library)

    # Valid token -> passes
    raw = auth.create_api_token(configured_library.db_path, "mcp")
    monkeypatch.setenv("TIRO_API_TOKEN", raw)
    _require_token_gate(configured_library)  # no raise


def test_mcp_gate_open_when_unconfigured(initialized_library, monkeypatch):
    from tiro.mcp.server import _require_token_gate

    monkeypatch.delenv("TIRO_API_TOKEN", raising=False)
    _require_token_gate(initialized_library)  # no password set -> no gate


def test_config_host_lan_refused_without_auth(tmp_path, monkeypatch):
    import argparse

    from tiro.cli import cmd_run

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"library_path: {tmp_path / 'lib'}\nhost: \"0.0.0.0\"\n")
    args = argparse.Namespace(
        config=str(cfg_file), lan=False, no_browser=True, insecure_no_auth=False
    )
    with pytest.raises(SystemExit) as exc:
        cmd_run(args)
    assert exc.value.code == 1


def test_lan_binding_from_config_accepts_machine_ip(tmp_path, monkeypatch, _shared_embeddings):
    from fastapi.testclient import TestClient

    import tiro.app as app_mod
    from tiro import auth as tiro_auth
    from tiro.database import init_db, migrate_db
    from tiro.vectorstore import init_vectorstore

    config = TiroConfig(library_path=str(tmp_path / "lan-lib"), host="0.0.0.0")
    config.articles_dir.mkdir(parents=True, exist_ok=True)
    (config.library / "audio").mkdir(parents=True, exist_ok=True)
    init_db(config.db_path)
    migrate_db(config.db_path)
    init_vectorstore(config.chroma_dir, config.default_embedding_model)
    config.auth_password_hash = tiro_auth.hash_password("pw")

    monkeypatch.setattr(app_mod, "_detect_lan_ips", lambda: ["192.168.1.50"])
    app = app_mod.create_app(config)
    with TestClient(app, base_url="http://localhost") as c:
        assert c.get("/healthz", headers={"Host": "192.168.1.50:8000"}).status_code == 200
        assert c.get("/healthz", headers={"Host": "evil.example:8000"}).status_code == 400


def test_testserver_not_in_production_allowlist(auth_client):
    assert auth_client.get("/healthz", headers={"Host": "testserver"}).status_code == 400


def test_cross_site_login_rejected(auth_client):
    r = auth_client.post("/api/auth/login", json={"password": "x"},
                         headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_mount_surface_is_pinned():
    # config never touched for route inspection — use a bare config object
    import tempfile

    from starlette.routing import Mount

    from tiro.app import create_app
    with tempfile.TemporaryDirectory() as d:
        cfg = TiroConfig(library_path=d)
        (cfg.library / "themes").mkdir(parents=True, exist_ok=True)
        app = create_app(cfg)
    mounts = {r.path for r in app.routes if isinstance(r, Mount)}
    assert mounts == {"/static", "/library/themes"}


def test_mcp_gate_enforced_per_call_after_revocation(configured_library, monkeypatch):
    import tiro.mcp.server as mcp_server

    raw = auth.create_api_token(configured_library.db_path, "mcp")
    monkeypatch.setenv("TIRO_API_TOKEN", raw)
    monkeypatch.setattr(mcp_server, "_config", None)
    # _get_config() calls the module's own load_config(_config_path()) when
    # _config is None; point it at configured_library so the gate checks the
    # same DB the token was created/revoked against (otherwise it'd load a
    # bare default config with no password and the gate would be a no-op).
    monkeypatch.setattr(mcp_server, "load_config", lambda *a, **k: configured_library)
    monkeypatch.setattr(mcp_server, "init_vectorstore", lambda *a, **k: None)
    mcp_server._get_config()  # passes

    tid = auth.list_api_tokens(configured_library.db_path)[0]["id"]
    auth.revoke_api_token(configured_library.db_path, tid)
    with pytest.raises(RuntimeError):
        mcp_server._get_config()  # revocation now bites on next call


def test_mcp_get_config_migrates_legacy_db(tmp_path, monkeypatch):
    """_get_config() must bring a hackathon-era DB (predates the auth tables
    entirely) up to the latest schema on first init, the same way app.py's
    lifespan does — otherwise MCP queries relying on post-hackathon columns
    (display_date/uid) 500, and tools that touch auth (token gating) hit
    'no such table: sessions'."""
    import sqlite3

    import tiro.mcp.server as mcp_server

    db = tmp_path / "tiro.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "domain TEXT, email_sender TEXT, source_type TEXT NOT NULL, "
        "is_vip BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE articles (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, "
        "title TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, markdown_path TEXT NOT NULL)"
    )
    conn.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL)")
    conn.execute(
        "CREATE TABLE entities (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "entity_type TEXT NOT NULL, UNIQUE(name, entity_type))"
    )
    conn.commit()
    conn.close()

    legacy_config = TiroConfig(library_path=str(tmp_path))
    monkeypatch.setattr(mcp_server, "_config", None)
    monkeypatch.setattr(mcp_server, "load_config", lambda *a, **k: legacy_config)
    monkeypatch.setattr(mcp_server, "init_vectorstore", lambda *a, **k: None)

    mcp_server._get_config()  # must not raise

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
    ).fetchone()
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_tokens'"
    ).fetchone()
    conn.close()


def test_cross_site_setup_rejected(auth_client, client):
    # On a configured library, "already configured" would also 403 here even
    # if CSRF were removed — so the status code alone doesn't pin CSRF.
    # Assert the detail string proves _check_csrf actually fired.
    r = auth_client.post("/api/auth/setup", json={"password": "x", "confirm": "x"},
                         headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert "cross-site" in r.json()["detail"].lower()

    # On an *unconfigured* library there's no masking branch to hide behind:
    # cross-site must still 403 with the same CSRF detail.
    r2 = client.post(
        "/api/auth/setup",
        json={"password": "a-valid-password-123"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert r2.status_code == 403
    assert "cross-site" in r2.json()["detail"].lower()

    # Control: the identical request minus the cross-site header must NOT
    # be blocked by CSRF — it proceeds to normal setup semantics (200).
    r3 = client.post(
        "/api/auth/setup",
        json={"password": "a-valid-password-123"},
    )
    assert r3.status_code == 200


def test_cross_site_logout_rejected(auth_client):
    r = auth_client.post("/api/auth/logout",
                         headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
