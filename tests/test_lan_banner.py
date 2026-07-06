"""M3.0 Task 4: TLS run flags + the LAN-over-HTTP warning banner.

Covers:
- create_app's `insecure_lan_http` context-flag matrix (loopback/no-TLS,
  0.0.0.0/no-TLS, 0.0.0.0/TLS) via the `tls_enabled` kwarg.
- base.html banner presence/absence per flag, rendered end-to-end through
  an authenticated page request (not a bare _theme_context() unit call —
  that function now needs a real Request, so the render IS the test).
- cli.py `tiro run --cert/--key`: both-or-neither argparse validation,
  file-exists validation before uvicorn ever starts, and ssl_certfile/
  ssl_keyfile passthrough to uvicorn.run.
- run.py's equivalent --cert/--key validation and passthrough (the second
  entry point named in the Task 4 brief).
- Static pins: base.html/sidebar.js contain the banner markup and
  dismissal wiring (sidebar.js's dismissal logic is DOM-bound and not
  meaningfully node-testable without a DOM shim, so it's covered here as a
  content pin plus the Playwright pass described in the Task 4 report).

STATIC_VERSION's 62->63 bump is exercised by the existing pins in
test_views.py (test_static_version_bumped_for_saved_views_ui) and
test_wiki_views.py (test_static_version_is_63) — not duplicated here.
"""

import argparse
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tiro import auth as tiro_auth
from tiro.config import TiroConfig
from tiro.database import init_db, migrate_db
from tiro.vectorstore import init_vectorstore

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_PW = "banner-test-pw"


def _make_app(tmp_path, name, host, tls_enabled, monkeypatch):
    import tiro.app as app_mod

    config = TiroConfig(library_path=str(tmp_path / name), host=host)
    config.articles_dir.mkdir(parents=True, exist_ok=True)
    (config.library / "audio").mkdir(parents=True, exist_ok=True)
    init_db(config.db_path)
    migrate_db(config.db_path)
    init_vectorstore(config.chroma_dir, config.default_embedding_model)
    config.auth_password_hash = tiro_auth.hash_password(TEST_PW)

    if host not in ("127.0.0.1", "localhost"):
        # Same stand-in used by test_auth.py's
        # test_lan_binding_from_config_accepts_machine_ip — avoids real
        # socket calls in create_app's _detect_lan_ips().
        monkeypatch.setattr(app_mod, "_detect_lan_ips", lambda: ["192.168.1.50"])

    return app_mod.create_app(config, tls_enabled=tls_enabled)


def _logged_in_client(app):
    client = TestClient(app, base_url="http://localhost", follow_redirects=False)
    r = client.post("/api/auth/login", json={"password": TEST_PW})
    assert r.status_code == 200
    return client


# --- insecure_lan_http context-flag matrix ------------------------------


def test_insecure_lan_http_false_on_loopback_no_tls(tmp_path, monkeypatch, _shared_embeddings):
    app = _make_app(tmp_path, "loopback", "127.0.0.1", False, monkeypatch)
    assert app.state.insecure_lan_http is False


def test_insecure_lan_http_true_on_lan_no_tls(tmp_path, monkeypatch, _shared_embeddings):
    app = _make_app(tmp_path, "lan-no-tls", "0.0.0.0", False, monkeypatch)
    assert app.state.insecure_lan_http is True


def test_insecure_lan_http_false_on_lan_with_tls(tmp_path, monkeypatch, _shared_embeddings):
    app = _make_app(tmp_path, "lan-tls", "0.0.0.0", True, monkeypatch)
    assert app.state.insecure_lan_http is False


def test_tls_enabled_defaults_false(tmp_path, monkeypatch, _shared_embeddings):
    """create_app() with no tls_enabled kwarg — every pre-existing caller
    (tests, bare construction) must keep behaving as plain HTTP."""
    import tiro.app as app_mod

    config = TiroConfig(library_path=str(tmp_path / "default"))
    config.articles_dir.mkdir(parents=True, exist_ok=True)
    (config.library / "audio").mkdir(parents=True, exist_ok=True)
    init_db(config.db_path)
    migrate_db(config.db_path)
    init_vectorstore(config.chroma_dir, config.default_embedding_model)

    app = app_mod.create_app(config)
    assert app.state.tls_enabled is False
    assert app.state.insecure_lan_http is False


# --- banner rendering (end-to-end through a real page render) ----------


def test_banner_absent_on_loopback(tmp_path, monkeypatch, _shared_embeddings):
    app = _make_app(tmp_path, "loopback-page", "127.0.0.1", False, monkeypatch)
    client = _logged_in_client(app)
    r = client.get("/inbox")
    assert r.status_code == 200
    assert 'id="lan-http-banner"' not in r.text
    assert "has-lan-banner" not in r.text


def test_banner_present_on_lan_without_tls(tmp_path, monkeypatch, _shared_embeddings):
    app = _make_app(tmp_path, "lan-page", "0.0.0.0", False, monkeypatch)
    client = _logged_in_client(app)
    r = client.get("/inbox", headers={"Host": "192.168.1.50:8000"})
    assert r.status_code == 200
    assert 'id="lan-http-banner"' in r.text
    assert 'role="status"' in r.text
    assert "lan-http-banner-dismiss" in r.text
    assert "has-lan-banner" in r.text


def test_banner_absent_on_lan_with_tls(tmp_path, monkeypatch, _shared_embeddings):
    app = _make_app(tmp_path, "lan-tls-page", "0.0.0.0", True, monkeypatch)
    client = _logged_in_client(app)
    r = client.get("/inbox", headers={"Host": "192.168.1.50:8000"})
    assert r.status_code == 200
    assert 'id="lan-http-banner"' not in r.text


def test_banner_absent_on_login_page(tmp_path, monkeypatch, _shared_embeddings):
    """login.html doesn't extend base.html (pre-auth standalone page), so
    even when insecure_lan_http is True there's no banner markup to render
    there — documented, not a bug: the banner only matters once the user
    is looking at their library."""
    app = _make_app(tmp_path, "lan-login-page", "0.0.0.0", False, monkeypatch)
    with TestClient(app, base_url="http://localhost", follow_redirects=False) as client:
        r = client.get("/login", headers={"Host": "192.168.1.50:8000"})
    assert r.status_code == 200
    assert 'id="lan-http-banner"' not in r.text


# --- cli.py: --cert/--key validation ------------------------------------


def test_cli_cert_without_key_is_argparse_error(monkeypatch, tmp_path):
    from tiro import cli

    monkeypatch.setattr(
        sys, "argv",
        ["tiro", "run", "--cert", str(tmp_path / "c.pem"), "--no-browser"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_cli_key_without_cert_is_argparse_error(monkeypatch, tmp_path):
    from tiro import cli

    monkeypatch.setattr(
        sys, "argv",
        ["tiro", "run", "--key", str(tmp_path / "k.pem"), "--no-browser"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_cli_nonexistent_cert_errors_before_uvicorn(monkeypatch, tmp_path):
    import uvicorn as uvicorn_mod

    from tiro import cli

    called = {"ran": False}
    monkeypatch.setattr(uvicorn_mod, "run", lambda *a, **k: called.__setitem__("ran", True))

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"library_path: {tmp_path / 'lib'}\n")
    args = argparse.Namespace(
        config=str(cfg_file), lan=False, no_browser=True, insecure_no_auth=False,
        cert=str(tmp_path / "missing-cert.pem"), key=str(tmp_path / "missing-key.pem"),
    )
    with pytest.raises(SystemExit) as exc:
        cli.cmd_run(args)
    assert exc.value.code == 1
    assert called["ran"] is False


def test_cli_missing_cert_attrs_do_not_crash_existing_callers(monkeypatch, tmp_path):
    """Pre-existing tests (test_auth.py) build argparse.Namespace(...) by
    hand without cert/key attributes at all — cmd_run must keep tolerating
    that via getattr(..., None), not require the new attributes."""
    from tiro import cli

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"library_path: {tmp_path / 'lib'}\n")  # no password
    args = argparse.Namespace(
        config=str(cfg_file), lan=True, no_browser=True, insecure_no_auth=False,
    )
    with pytest.raises(SystemExit) as exc:
        cli.cmd_run(args)
    assert exc.value.code == 1  # refused for lack of a password, not an AttributeError


def test_cli_passes_ssl_args_to_uvicorn(monkeypatch, tmp_path, _shared_embeddings):
    import uvicorn as uvicorn_mod

    from tiro import cli

    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("dummy cert")
    key.write_text("dummy key")

    captured = {}
    monkeypatch.setattr(uvicorn_mod, "run", lambda app, **kw: captured.update(kw))

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"library_path: {tmp_path / 'lib'}\n")
    args = argparse.Namespace(
        config=str(cfg_file), lan=False, no_browser=True, insecure_no_auth=False,
        cert=str(cert), key=str(key),
    )
    cli.cmd_run(args)
    assert captured["ssl_certfile"] == str(cert)
    assert captured["ssl_keyfile"] == str(key)


def test_cli_no_tls_passes_none_ssl_args(monkeypatch, tmp_path, _shared_embeddings):
    import uvicorn as uvicorn_mod

    from tiro import cli

    captured = {}
    monkeypatch.setattr(uvicorn_mod, "run", lambda app, **kw: captured.update(kw))

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"library_path: {tmp_path / 'lib'}\n")
    args = argparse.Namespace(
        config=str(cfg_file), lan=False, no_browser=True, insecure_no_auth=False,
        cert=None, key=None,
    )
    cli.cmd_run(args)
    assert captured["ssl_certfile"] is None
    assert captured["ssl_keyfile"] is None


# --- run.py: equivalent --cert/--key validation and passthrough ---------


def _import_run_module():
    sys.path.insert(0, str(REPO_ROOT))
    return importlib.import_module("run")


def test_run_py_cert_without_key_is_argparse_error(monkeypatch):
    run = _import_run_module()
    monkeypatch.setattr(sys, "argv", ["run.py", "--cert", "/tmp/does-not-matter.pem"])
    with pytest.raises(SystemExit) as exc:
        run._parse_args()
    assert exc.value.code == 2


def test_run_py_key_without_cert_is_argparse_error(monkeypatch):
    """Symmetric to the cert-without-key case above — both-or-neither must
    be enforced regardless of which of the pair is given alone."""
    run = _import_run_module()
    monkeypatch.setattr(sys, "argv", ["run.py", "--key", "/tmp/does-not-matter.pem"])
    with pytest.raises(SystemExit) as exc:
        run._parse_args()
    assert exc.value.code == 2


def test_run_py_nonexistent_cert_errors_before_uvicorn(monkeypatch, tmp_path):
    run = _import_run_module()

    called = {"ran": False}
    monkeypatch.setattr(run.uvicorn, "run", lambda *a, **k: called.__setitem__("ran", True))
    monkeypatch.setattr(
        sys, "argv",
        ["run.py", "--cert", str(tmp_path / "missing-cert.pem"), "--key", str(tmp_path / "missing-key.pem")],
    )
    monkeypatch.delenv("TIRO_CONFIG", raising=False)

    with pytest.raises(SystemExit) as exc:
        run.main()
    assert exc.value.code == 1
    assert called["ran"] is False


def test_run_py_passes_ssl_args_to_uvicorn(monkeypatch, tmp_path):
    run = _import_run_module()

    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("dummy cert")
    key.write_text("dummy key")

    captured = {}
    monkeypatch.setattr(run.uvicorn, "run", lambda app, **kw: captured.update(kw))
    monkeypatch.setattr(sys, "argv", ["run.py", "--cert", str(cert), "--key", str(key)])
    monkeypatch.delenv("TIRO_CONFIG", raising=False)

    run.main()
    assert captured["ssl_certfile"] == str(cert)
    assert captured["ssl_keyfile"] == str(key)


def test_run_py_no_tls_passes_none_ssl_args(monkeypatch):
    run = _import_run_module()

    captured = {}
    monkeypatch.setattr(run.uvicorn, "run", lambda app, **kw: captured.update(kw))
    monkeypatch.setattr(sys, "argv", ["run.py"])
    monkeypatch.delenv("TIRO_CONFIG", raising=False)

    run.main()
    assert captured["ssl_certfile"] is None
    assert captured["ssl_keyfile"] is None


# --- static pins: banner markup + dismissal wiring ----------------------


def test_base_html_has_lan_banner_markup():
    content = (REPO_ROOT / "tiro/frontend/templates/base.html").read_text()
    assert 'id="lan-http-banner"' in content
    assert 'role="status"' in content
    assert "insecure_lan_http" in content
    assert "lan-http-banner-dismiss" in content


def test_sidebar_js_has_lan_banner_dismissal():
    content = (REPO_ROOT / "tiro/frontend/static/js/sidebar.js").read_text()
    assert "tiro-lan-banner-dismissed" in content
    assert "sessionStorage" in content
    assert "lan-http-banner-dismiss" in content
    assert "setupLanBanner" in content
