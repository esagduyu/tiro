"""First-run onboarding wizard tests (Phase 5 M5.1, spec D6).

The wizard's whole point is to set the password, so parts are PRE-AUTH by
design — exactly the same trust window POST /api/auth/setup has always
accepted. These tests pin the three-way gate on /welcome and each /api/setup/*
route, the library-path pristine guard, provider validation, and samples
idempotence.
"""

from pathlib import Path

import pytest

# --- /welcome three-way gate -------------------------------------------------

def test_welcome_served_unconfigured(client):
    # Unconfigured: the wizard IS how the password gets set, so it serves anyone.
    r = client.get("/welcome")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_welcome_served_when_authenticated(authenticated_client):
    # Configured + session: revisitable (later steps stay useful post-setup).
    r = authenticated_client.get("/welcome")
    assert r.status_code == 200


def test_welcome_redirects_configured_anonymous(auth_client):
    # Configured + no session: 302 to /login (the wizard is not a bypass).
    r = auth_client.get("/welcome", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


# --- /api/setup/* three-way gate ---------------------------------------------

# Parametrized (not a shared list) so each case gets a FRESH fixture + tmp_path:
# the library-path handler repoints the live library at its dest, which would
# invalidate a session shared across routes in one test (the new DB has no
# session row). The probe path is derived from tmp_path — NOT a fixed
# /tmp/tiro-gate-probe-* path, which would (a) leave a library outside tmp_path
# across runs and (b) rebind the process-global vectorstore to it.
SETUP_ROUTE_KEYS = ["library-path", "ai", "samples"]


def _setup_post(key, tmp_path):
    return {
        "library-path": ("/api/setup/library-path", {"path": str(tmp_path / "gate-probe-lib")}),
        "ai": ("/api/setup/ai", {"provider": "skip"}),
        "samples": ("/api/setup/samples", {}),
    }[key]


# Gate-open means the handler ran: not an auth rejection (401), not a page-auth
# redirect (302), and not an unhandled server error (500) masquerading as "open".
def _assert_gate_open(status):
    assert status not in (401, 302, 500), f"gate did not open cleanly: {status}"


@pytest.mark.parametrize("key", SETUP_ROUTE_KEYS)
def test_setup_routes_reject_configured_anonymous(auth_client, tmp_path, key):
    path, body = _setup_post(key, tmp_path)
    r = auth_client.request("POST", path, json=body)
    assert r.status_code == 401


@pytest.mark.parametrize("key", SETUP_ROUTE_KEYS)
def test_setup_routes_accept_authenticated(authenticated_client, tmp_path, key):
    path, body = _setup_post(key, tmp_path)
    r = authenticated_client.request("POST", path, json=body)
    _assert_gate_open(r.status_code)


@pytest.mark.parametrize("key", SETUP_ROUTE_KEYS)
def test_setup_routes_accept_unconfigured(client, tmp_path, key):
    # Pre-password steps (1-2) must work with no session; the same conditional
    # the /welcome page uses. Reaching the handler (any non-auth status) proves
    # the gate opened.
    path, body = _setup_post(key, tmp_path)
    r = client.request("POST", path, json=body)
    _assert_gate_open(r.status_code)


# --- library-path ------------------------------------------------------------

def test_library_path_pristine_repoints(authenticated_client, configured_library, tmp_path):
    dest = tmp_path / "relocated-library"
    r = authenticated_client.post("/api/setup/library-path", json={"path": str(dest)})
    assert r.status_code == 200
    # Persisted to the YAML file (through persist_config).
    assert str(dest) in Path(configured_library.config_path).read_text()
    # Live config re-pointed and the new store dirs bootstrapped.
    assert configured_library.library == dest.resolve() or configured_library.library == dest
    assert (dest / "articles").exists()
    assert configured_library.db_path.exists()


def test_library_path_rejects_non_pristine(authenticated_client, configured_library, tmp_path):
    # Seed one article so the library is no longer pristine.
    from tiro.ingestion.processor import process_article

    process_article(
        title="Existing", author=None, content_md="body",
        url="https://example.com/existing", config=configured_library,
    )
    dest = tmp_path / "should-not-happen"
    r = authenticated_client.post("/api/setup/library-path", json={"path": str(dest)})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "library_not_pristine"
    assert not dest.exists()


def test_library_path_rejects_relative(authenticated_client):
    r = authenticated_client.post("/api/setup/library-path", json={"path": "./relative-lib"})
    assert r.status_code == 400


def test_library_path_rejects_uncreatable(authenticated_client, tmp_path):
    # A path whose parent is a regular file cannot be created as a directory.
    blocker = tmp_path / "iamafile"
    blocker.write_text("x")
    r = authenticated_client.post(
        "/api/setup/library-path", json={"path": str(blocker / "child")}
    )
    assert r.status_code == 400


# --- AI provider -------------------------------------------------------------

@pytest.mark.parametrize("provider", ["anthropic", "openai-compatible", "claude-cli", "codex-cli"])
def test_ai_provider_persists(authenticated_client, configured_library, provider):
    r = authenticated_client.post(
        "/api/setup/ai", json={"provider": provider, "api_key": "sk-secret-value"}
    )
    assert r.status_code == 200
    yaml_text = Path(configured_library.config_path).read_text()
    assert f"ai_heavy_provider: {provider}" in yaml_text
    assert f"ai_light_provider: {provider}" in yaml_text
    # The raw key must never be echoed back unmasked in the response.
    assert "sk-secret-value" not in r.text


def test_ai_provider_skip_is_noop(authenticated_client):
    r = authenticated_client.post("/api/setup/ai", json={"provider": "skip"})
    assert r.status_code == 200


def test_ai_unknown_provider_400(authenticated_client):
    r = authenticated_client.post("/api/setup/ai", json={"provider": "bogus-llm"})
    assert r.status_code == 400


def test_ai_key_persisted_but_not_echoed(authenticated_client, configured_library):
    r = authenticated_client.post(
        "/api/setup/ai", json={"provider": "anthropic", "api_key": "sk-anthropic-xyz"}
    )
    assert "sk-anthropic-xyz" not in r.text
    # But it IS written to config so the running server can use it.
    assert "sk-anthropic-xyz" in Path(configured_library.config_path).read_text()


# --- samples -----------------------------------------------------------------

def test_samples_creates_exactly_two(authenticated_client, configured_library):
    r = authenticated_client.post("/api/setup/samples", json={})
    assert r.status_code == 200
    assert r.json()["data"]["created"] == 2

    articles = authenticated_client.get("/api/articles").json()["data"]
    assert len(articles) == 2
    # Markdown files exist on disk (the normal pipeline ran).
    md_files = list(configured_library.articles_dir.glob("*.md"))
    assert len(md_files) == 2


def test_samples_idempotent(authenticated_client, configured_library):
    authenticated_client.post("/api/setup/samples", json={})
    r2 = authenticated_client.post("/api/setup/samples", json={})
    assert r2.status_code == 200
    # Second call rides existing duplicate detection — no new rows.
    assert r2.json()["data"]["created"] == 0
    articles = authenticated_client.get("/api/articles").json()["data"]
    assert len(articles) == 2


# --- unconfigured page-auth redirect now targets /welcome --------------------

def test_unconfigured_pages_redirect_to_welcome(client):
    for path in ["/inbox", "/digest", "/settings"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 302, f"{path} -> {r.status_code}"
        assert r.headers["location"] == "/welcome"


# --- login.html folds setup mode into a redirect -----------------------------

def test_login_page_has_no_inline_setup_form(client):
    # The setup-mode confirm field moved into the wizard; login.html is now
    # password-entry-only. login.js/inline script redirects unconfigured
    # visitors to /welcome instead of flipping into setup mode in place.
    html = client.get("/login").text
    assert 'id="confirm"' not in html
    assert "/welcome" in html
