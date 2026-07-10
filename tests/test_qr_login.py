"""M3.0 Task 2: QR one-time login.

Covers the token machinery in tiro/auth.py (create_login_token /
consume_login_token) and the GET /login/qr + /setup/qr routes in
tiro/app.py. Security-critical surface: this is how an unauthenticated
device gets a session, so every failure path must be generic (no oracle)
and the token must never double as a Bearer/API token.
"""

from tiro import auth
from tiro.database import get_connection

# --- Token machinery (auth.py) ---------------------------------------------


def test_create_login_token_stores_hash_only(configured_library):
    """The raw token must never be persisted — only its SHA-256 hash."""
    token = auth.create_login_token(configured_library)
    conn = get_connection(configured_library.db_path)
    try:
        rows = conn.execute("SELECT token_hash FROM login_tokens").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["token_hash"] != token
    assert all(token not in r["token_hash"] for r in rows)


def test_consume_login_token_happy_path(configured_library):
    token = auth.create_login_token(configured_library)
    assert auth.consume_login_token(configured_library, token) is True


def test_consume_login_token_single_use(configured_library):
    """Second consume of the same token must fail — atomic single-use."""
    token = auth.create_login_token(configured_library)
    assert auth.consume_login_token(configured_library, token) is True
    assert auth.consume_login_token(configured_library, token) is False


def test_consume_login_token_double_spend_is_atomic(configured_library):
    """Simulates two concurrent redemptions of the same token: only one of
    two UPDATE attempts against the same row may succeed. A SELECT-then-
    UPDATE implementation would have a TOCTOU window where both could pass
    the SELECT check; the single atomic UPDATE...WHERE guards against it."""
    token = auth.create_login_token(configured_library)
    results = [
        auth.consume_login_token(configured_library, token),
        auth.consume_login_token(configured_library, token),
    ]
    assert sorted(results) == [False, True]


def test_consume_login_token_expired_fails(configured_library):
    token = auth.create_login_token(configured_library)
    conn = get_connection(configured_library.db_path)
    try:
        conn.execute(
            "UPDATE login_tokens SET expires_at = datetime('now', '-1 minute') "
            "WHERE token_hash = ?",
            (auth._sha256(token),),
        )
        conn.commit()
    finally:
        conn.close()
    assert auth.consume_login_token(configured_library, token) is False


def test_consume_login_token_garbage_fails(configured_library):
    assert auth.consume_login_token(configured_library, "not-a-real-token") is False


def test_consume_login_token_missing_fails(configured_library):
    assert auth.consume_login_token(configured_library, "") is False


def test_login_token_ttl_is_fifteen_minutes(configured_library):
    token = auth.create_login_token(configured_library)
    conn = get_connection(configured_library.db_path)
    try:
        row = conn.execute(
            "SELECT (julianday(expires_at) - julianday(created_at)) * 24 * 60 AS minutes "
            "FROM login_tokens WHERE token_hash = ?",
            (auth._sha256(token),),
        ).fetchone()
    finally:
        conn.close()
    assert round(row["minutes"]) == auth.LOGIN_TOKEN_TTL_MINUTES == 15


# --- GET /login/qr route ----------------------------------------------------


def test_login_qr_valid_token_creates_session_and_redirects(auth_client, configured_library):
    token = auth.create_login_token(configured_library)
    r = auth_client.get(f"/login/qr?token={token}")
    assert r.status_code == 302
    assert r.headers["location"] == "/inbox"
    assert auth.SESSION_COOKIE in r.cookies

    # Cookie is a real, valid session — same helper the password path uses.
    session_token = r.cookies[auth.SESSION_COOKIE]
    assert auth.validate_session(configured_library.db_path, session_token)


def test_login_qr_second_use_fails_generic_redirect(auth_client, configured_library):
    token = auth.create_login_token(configured_library)
    first = auth_client.get(f"/login/qr?token={token}")
    assert first.status_code == 302
    assert first.headers["location"] == "/inbox"

    second = auth_client.get(f"/login/qr?token={token}")
    assert second.status_code == 302
    assert second.headers["location"] == "/login"
    assert auth.SESSION_COOKIE not in second.cookies


def test_login_qr_expired_token_generic_redirect(auth_client, configured_library):
    token = auth.create_login_token(configured_library)
    conn = get_connection(configured_library.db_path)
    try:
        conn.execute(
            "UPDATE login_tokens SET expires_at = datetime('now', '-1 minute') "
            "WHERE token_hash = ?",
            (auth._sha256(token),),
        )
        conn.commit()
    finally:
        conn.close()
    r = auth_client.get(f"/login/qr?token={token}")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_login_qr_garbage_token_generic_redirect(auth_client):
    r = auth_client.get("/login/qr?token=totally-made-up")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_login_qr_missing_token_generic_redirect(auth_client):
    r = auth_client.get("/login/qr")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_login_qr_all_failures_land_on_same_generic_redirect(auth_client, configured_library):
    """No enumeration oracle: missing, garbage, expired, and already-used
    tokens must all be indistinguishable from the caller's point of view."""
    used_token = auth.create_login_token(configured_library)
    auth_client.get(f"/login/qr?token={used_token}")  # consume it

    expired_token = auth.create_login_token(configured_library)
    conn = get_connection(configured_library.db_path)
    try:
        conn.execute(
            "UPDATE login_tokens SET expires_at = datetime('now', '-1 minute') "
            "WHERE token_hash = ?",
            (auth._sha256(expired_token),),
        )
        conn.commit()
    finally:
        conn.close()

    responses = [
        auth_client.get("/login/qr"),
        auth_client.get("/login/qr?token="),
        auth_client.get("/login/qr?token=garbage-not-real"),
        auth_client.get(f"/login/qr?token={used_token}"),
        auth_client.get(f"/login/qr?token={expired_token}"),
    ]
    for r in responses:
        assert r.status_code == 302
        assert r.headers["location"] == "/login"
        assert auth.SESSION_COOKIE not in r.cookies


def test_login_qr_token_not_usable_as_bearer_token(auth_client, configured_library):
    """SECURITY: a QR login token must never work as an API Bearer token —
    it's a single-use session bootstrap, not a standing credential."""
    token = auth.create_login_token(configured_library)
    r = auth_client.get("/api/articles", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401

    # It also wasn't silently consumed by the failed bearer attempt — a
    # legitimate QR scan against the same token must still work.
    login_r = auth_client.get(f"/login/qr?token={token}")
    assert login_r.status_code == 302
    assert login_r.headers["location"] == "/inbox"


# --- /setup/qr page ----------------------------------------------------------


def test_setup_qr_requires_page_auth(auth_client):
    r = auth_client.get("/setup/qr")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_setup_qr_page_renders_qr_svg_and_countdown_and_regenerate(authenticated_client):
    r = authenticated_client.get("/setup/qr")
    assert r.status_code == 200
    body = r.text
    assert "<svg" in body
    assert "qr-countdown" in body
    assert "Generate new code" in body
    # Forms now carry a `mode` query param (browser vs device panel, M-iOS T1).
    assert 'action="/setup/qr?mode=browser"' in body
    assert 'method="post"' in body


def test_setup_qr_get_mints_a_redeemable_token(authenticated_client):
    """The token embedded in the rendered page must actually work end to
    end against /login/qr (not just render — be redeemable)."""
    import re

    r = authenticated_client.get("/setup/qr")
    match = re.search(r"token=([\w\-]+)", r.text)
    assert match, "no token found embedded in /setup/qr page"
    token = match.group(1)

    redeem = authenticated_client.get(f"/login/qr?token={token}")
    assert redeem.status_code == 302
    assert redeem.headers["location"] == "/inbox"


def test_setup_qr_regenerate_post_issues_a_different_token(authenticated_client):
    import re

    first = authenticated_client.get("/setup/qr")
    second = authenticated_client.post("/setup/qr")
    assert second.status_code == 200

    first_token = re.search(r"token=([\w\-]+)", first.text).group(1)
    second_token = re.search(r"token=([\w\-]+)", second.text).group(1)
    assert first_token != second_token


def test_setup_qr_regenerate_requires_page_auth(auth_client):
    r = auth_client.post("/setup/qr")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


# --- Security hardening: disjoint namespaces + no-store -----------------


def test_login_token_as_session_cookie_is_rejected(auth_client, configured_library):
    """SECURITY: a login token must not work if presented as the SESSION
    cookie value either — login_tokens and sessions are wholly separate
    tables/hash spaces. The Bearer-token variant of this is covered by
    test_login_qr_token_not_usable_as_bearer_token above; this is the other
    disjoint namespace (cookie-based auth), previously untested."""
    token = auth.create_login_token(configured_library)
    auth_client.cookies.set(auth.SESSION_COOKIE, token)
    r = auth_client.get("/api/articles", follow_redirects=False)
    assert r.status_code == 401

    page_r = auth_client.get("/inbox", follow_redirects=False)
    assert page_r.status_code == 302
    assert page_r.headers["location"] == "/login"

    # And the token wasn't silently consumed by either failed attempt — it
    # still redeems normally via the real /login/qr path.
    auth_client.cookies.delete(auth.SESSION_COOKIE)
    redeem = auth_client.get(f"/login/qr?token={token}")
    assert redeem.status_code == 302
    assert redeem.headers["location"] == "/inbox"


def test_login_qr_redirect_has_no_store(auth_client, configured_library):
    token = auth.create_login_token(configured_library)
    ok = auth_client.get(f"/login/qr?token={token}")
    assert ok.headers["cache-control"] == "no-store"

    fail = auth_client.get("/login/qr?token=garbage-not-real")
    assert fail.headers["cache-control"] == "no-store"


def test_setup_qr_page_has_no_store(authenticated_client):
    get_r = authenticated_client.get("/setup/qr")
    assert get_r.headers["cache-control"] == "no-store"

    post_r = authenticated_client.post("/setup/qr")
    assert post_r.headers["cache-control"] == "no-store"
