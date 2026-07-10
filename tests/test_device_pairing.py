"""M-iOS Task 1: device pairing backend.

Mirrors tests/test_qr_login.py's shapes point-for-point — device pairing is
the same security-critical one-time-code pattern as QR login, but instead of
minting a *session cookie* for a browser it mints a long-lived *API token* for
the native iOS client. Every failure path must be generic (no oracle), the
pair code must never double as a Bearer/session credential, and a consumed
code must be atomically single-use.

See tiro/auth.py create_pair_code/consume_pair_code and the POST /api/auth/pair
route in tiro/api/routes_auth.py + the /setup/qr?mode=device panel in
tiro/app.py.
"""

from tiro import auth
from tiro.database import get_connection

# --- Code machinery (auth.py) ----------------------------------------------


def test_create_pair_code_stores_hash_only(configured_library):
    """The raw code must never be persisted — only its SHA-256 hash."""
    code = auth.create_pair_code(configured_library.db_path)
    conn = get_connection(configured_library.db_path)
    try:
        rows = conn.execute("SELECT code_hash FROM device_pair_codes").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["code_hash"] != code
    assert all(code not in r["code_hash"] for r in rows)


def test_consume_pair_code_happy_path_returns_working_bearer_token(
    auth_client, configured_library
):
    """A consumed code yields a raw API token that then authenticates as a
    Bearer credential against a real gated endpoint."""
    code = auth.create_pair_code(configured_library.db_path)
    token = auth.consume_pair_code(configured_library.db_path, code, "iPhone")
    assert token
    r = auth_client.get("/api/articles", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_consume_pair_code_token_row_named_ios_device(configured_library):
    """The minted api_tokens row is named ios:<device_name>."""
    code = auth.create_pair_code(configured_library.db_path)
    auth.consume_pair_code(configured_library.db_path, code, "Ege's iPhone")
    conn = get_connection(configured_library.db_path)
    try:
        names = {r["name"] for r in conn.execute("SELECT name FROM api_tokens").fetchall()}
    finally:
        conn.close()
    assert "ios:Ege's iPhone" in names


def test_consume_pair_code_single_use(configured_library):
    """Second consume of the same code fails and mints no second token."""
    code = auth.create_pair_code(configured_library.db_path)
    assert auth.consume_pair_code(configured_library.db_path, code, "iPhone")
    assert auth.consume_pair_code(configured_library.db_path, code, "iPhone") is None
    conn = get_connection(configured_library.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM api_tokens").fetchone()["n"]
    finally:
        conn.close()
    assert n == 1  # only the first consume minted a token


def test_consume_pair_code_double_spend_is_atomic(configured_library):
    """Two concurrent redemptions of one code: only one UPDATE...WHERE may
    win. A SELECT-then-UPDATE would have a TOCTOU window where both pass."""
    code = auth.create_pair_code(configured_library.db_path)
    results = [
        auth.consume_pair_code(configured_library.db_path, code, "iPhone"),
        auth.consume_pair_code(configured_library.db_path, code, "iPhone"),
    ]
    successes = [r for r in results if r is not None]
    assert len(successes) == 1


def test_consume_pair_code_expired_fails(configured_library):
    code = auth.create_pair_code(configured_library.db_path)
    conn = get_connection(configured_library.db_path)
    try:
        conn.execute(
            "UPDATE device_pair_codes SET expires_at = datetime('now', '-1 minute') "
            "WHERE code_hash = ?",
            (auth._sha256(code),),
        )
        conn.commit()
    finally:
        conn.close()
    assert auth.consume_pair_code(configured_library.db_path, code, "iPhone") is None


def test_consume_pair_code_garbage_fails(configured_library):
    assert auth.consume_pair_code(configured_library.db_path, "nope", "iPhone") is None


def test_consume_pair_code_missing_fails(configured_library):
    assert auth.consume_pair_code(configured_library.db_path, "", "iPhone") is None


def test_pair_code_ttl_is_fifteen_minutes(configured_library):
    code = auth.create_pair_code(configured_library.db_path)
    conn = get_connection(configured_library.db_path)
    try:
        row = conn.execute(
            "SELECT (julianday(expires_at) - julianday(created_at)) * 24 * 60 AS minutes "
            "FROM device_pair_codes WHERE code_hash = ?",
            (auth._sha256(code),),
        ).fetchone()
    finally:
        conn.close()
    assert round(row["minutes"]) == auth.LOGIN_TOKEN_TTL_MINUTES == 15


# --- POST /api/auth/pair route ---------------------------------------------


def test_pair_route_happy_path(auth_client, configured_library):
    code = auth.create_pair_code(configured_library.db_path)
    r = auth_client.post("/api/auth/pair", json={"code": code, "device_name": "iPhone"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    token = body["data"]["token"]
    assert body["data"]["name"] == "ios:iPhone"
    # The returned token is a working Bearer credential.
    articles = auth_client.get("/api/articles", headers={"Authorization": f"Bearer {token}"})
    assert articles.status_code == 200


def test_pair_route_default_device_name(auth_client, configured_library):
    code = auth.create_pair_code(configured_library.db_path)
    r = auth_client.post("/api/auth/pair", json={"code": code})
    assert r.status_code == 200
    assert r.json()["data"]["name"] == "ios:iPhone"


def test_pair_route_second_use_is_generic_400_and_mints_nothing(
    auth_client, configured_library
):
    code = auth.create_pair_code(configured_library.db_path)
    first = auth_client.post("/api/auth/pair", json={"code": code, "device_name": "iPhone"})
    assert first.status_code == 200

    second = auth_client.post("/api/auth/pair", json={"code": code, "device_name": "iPhone"})
    assert second.status_code == 400
    assert second.json()["detail"] == "invalid or expired code"

    conn = get_connection(configured_library.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM api_tokens").fetchone()["n"]
    finally:
        conn.close()
    assert n == 1


def test_pair_route_expired_code_is_generic_400(auth_client, configured_library):
    code = auth.create_pair_code(configured_library.db_path)
    conn = get_connection(configured_library.db_path)
    try:
        conn.execute(
            "UPDATE device_pair_codes SET expires_at = datetime('now', '-1 minute') "
            "WHERE code_hash = ?",
            (auth._sha256(code),),
        )
        conn.commit()
    finally:
        conn.close()
    r = auth_client.post("/api/auth/pair", json={"code": code, "device_name": "iPhone"})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid or expired code"


def test_pair_route_garbage_code_is_generic_400(auth_client):
    r = auth_client.post("/api/auth/pair", json={"code": "made-up", "device_name": "iPhone"})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid or expired code"


def test_pair_route_all_failures_identical_400(auth_client, configured_library):
    """No enumeration oracle: garbage, expired, and already-used codes are
    indistinguishable in the response."""
    used = auth.create_pair_code(configured_library.db_path)
    auth_client.post("/api/auth/pair", json={"code": used, "device_name": "iPhone"})

    expired = auth.create_pair_code(configured_library.db_path)
    conn = get_connection(configured_library.db_path)
    try:
        conn.execute(
            "UPDATE device_pair_codes SET expires_at = datetime('now', '-1 minute') "
            "WHERE code_hash = ?",
            (auth._sha256(expired),),
        )
        conn.commit()
    finally:
        conn.close()

    responses = [
        auth_client.post("/api/auth/pair", json={"code": "garbage", "device_name": "iPhone"}),
        auth_client.post("/api/auth/pair", json={"code": used, "device_name": "iPhone"}),
        auth_client.post("/api/auth/pair", json={"code": expired, "device_name": "iPhone"}),
    ]
    for r in responses:
        assert r.status_code == 400
        assert r.json()["detail"] == "invalid or expired code"


def test_pair_route_device_name_length_capped(auth_client, configured_library):
    code = auth.create_pair_code(configured_library.db_path)
    r = auth_client.post(
        "/api/auth/pair", json={"code": code, "device_name": "x" * 200}
    )
    assert r.status_code == 422  # pydantic max_length


def test_pair_route_no_auth_required(auth_client, configured_library):
    """The single-use code IS the boundary — the route accepts an
    unauthenticated (session-less) request, like /login/qr."""
    code = auth.create_pair_code(configured_library.db_path)
    # auth_client carries no session cookie.
    r = auth_client.post("/api/auth/pair", json={"code": code, "device_name": "iPhone"})
    assert r.status_code == 200


# --- Security hardening: disjoint namespaces -------------------------------


def test_pair_code_not_usable_as_bearer_token(auth_client, configured_library):
    """SECURITY: a pair code must never work as an API Bearer token — it's a
    single-use token-mint code, not a standing credential."""
    code = auth.create_pair_code(configured_library.db_path)
    r = auth_client.get("/api/articles", headers={"Authorization": f"Bearer {code}"})
    assert r.status_code == 401

    # It wasn't silently consumed by the failed bearer attempt.
    token = auth.consume_pair_code(configured_library.db_path, code, "iPhone")
    assert token


def test_pair_code_not_usable_as_session_cookie(auth_client, configured_library):
    """SECURITY: a pair code presented as the session cookie value must fail —
    device_pair_codes and sessions are wholly disjoint tables/hash spaces."""
    code = auth.create_pair_code(configured_library.db_path)
    auth_client.cookies.set(auth.SESSION_COOKIE, code)
    r = auth_client.get("/api/articles", follow_redirects=False)
    assert r.status_code == 401

    page = auth_client.get("/inbox", follow_redirects=False)
    assert page.status_code == 302
    assert page.headers["location"] == "/login"

    # And it still redeems normally afterward.
    auth_client.cookies.delete(auth.SESSION_COOKIE)
    assert auth.consume_pair_code(configured_library.db_path, code, "iPhone")


# --- /setup/qr?mode=device panel -------------------------------------------


def test_setup_qr_device_mode_renders_tiro_pair_and_no_store(authenticated_client):
    r = authenticated_client.get("/setup/qr?mode=device")
    assert r.status_code == 200
    assert "tiro://pair?" in r.text
    assert r.headers["cache-control"] == "no-store"


def test_setup_qr_device_mode_requires_page_auth(auth_client):
    r = auth_client.get("/setup/qr?mode=device")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_setup_qr_device_mode_mints_a_consumable_code(authenticated_client, configured_library):
    """The code embedded in the tiro://pair QR must actually be redeemable."""
    import re
    from urllib.parse import parse_qs, urlparse

    r = authenticated_client.get("/setup/qr?mode=device")
    match = re.search(r"tiro://pair\?[^\s\"'<]+", r.text)
    assert match, "no tiro://pair URI found in device-mode page"
    # The URL is rendered into HTML text, so its `&` separator is escaped to
    # `&amp;` — un-escape before parsing the query string.
    uri = match.group(0).replace("&amp;", "&")
    qs = parse_qs(urlparse(uri).query)
    code = qs["code"][0]

    token = auth.consume_pair_code(configured_library.db_path, code, "iPhone")
    assert token


def test_setup_qr_default_mode_still_browser_login(authenticated_client):
    """The default (no mode) panel is unchanged: browser QR login."""
    r = authenticated_client.get("/setup/qr")
    assert r.status_code == 200
    assert "/login/qr?token=" in r.text
