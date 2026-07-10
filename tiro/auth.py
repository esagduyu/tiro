"""Single-user authentication: password hashing, sessions, API tokens.

Sessions and API tokens are opaque random values; only SHA-256 hashes are
stored. Session cookies slide: validation extends expiry back to the full
TTL once more than a day of it has been consumed.
"""

import hashlib
import logging
import secrets
from pathlib import Path

import bcrypt
from fastapi import HTTPException, Request, Response

from tiro.config import TiroConfig
from tiro.database import get_connection

logger = logging.getLogger(__name__)

SESSION_COOKIE = "tiro_session"
SESSION_TTL_DAYS = 30
LOGIN_TOKEN_TTL_MINUTES = 15


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        logger.warning("Malformed password hash in config")
        return False


def create_session(db_path: Path) -> str:
    token = secrets.token_urlsafe(32)
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions (token_hash, expires_at) "
            "VALUES (?, datetime('now', ?))",
            (_sha256(token), f"+{SESSION_TTL_DAYS} days"),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def validate_session(db_path: Path, token: str) -> bool:
    token_hash = _sha256(token)
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT expires_at > datetime('now') AS valid, "
            f"       expires_at < datetime('now', '+{SESSION_TTL_DAYS - 1} days') AS stale "
            "FROM sessions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None or not row["valid"]:
            return False
        # Sliding renewal: only rewrite when >1 day of TTL has been consumed
        if row["stale"]:
            conn.execute(
                "UPDATE sessions SET expires_at = datetime('now', ?), "
                "last_seen_at = datetime('now') WHERE token_hash = ?",
                (f"+{SESSION_TTL_DAYS} days", token_hash),
            )
            conn.commit()
        return True
    finally:
        conn.close()


def destroy_session(db_path: Path, token: str) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_sha256(token),))
        conn.commit()
    finally:
        conn.close()


def attach_session_cookie(response: Response, request: Request, token: str) -> None:
    """Set the session cookie with the one set of flags every login path
    (password POST /api/auth/login|setup, QR GET /login/qr) must agree on.
    Factored out so QR login can never drift from the password path's
    cookie security properties (httponly/samesite/secure)."""
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


def create_login_token(config: TiroConfig) -> str:
    """Issue a one-time QR-login token (Phase 3 M3.0 Task 2).

    `secrets.token_urlsafe(32)` is >=128 bits of entropy — brute force is
    infeasible even against the (deliberately short) 15-minute TTL. Only the
    SHA-256 hash is ever persisted (same pattern as sessions/api_tokens); the
    raw token is returned exactly once, to be embedded in the QR code, and
    is never recoverable from the database afterward.
    """
    token = secrets.token_urlsafe(32)
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            "INSERT INTO login_tokens (token_hash, created_at, expires_at) "
            "VALUES (?, datetime('now'), datetime('now', ?))",
            (_sha256(token), f"+{LOGIN_TOKEN_TTL_MINUTES} minutes"),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def consume_login_token(config: TiroConfig, token: str) -> bool:
    """Atomically redeem a one-time login token.

    A single UPDATE ... WHERE used_at IS NULL AND expires_at > now, gated on
    rowcount == 1, is the only safe way to implement single-use: a
    SELECT-then-UPDATE has a TOCTOU window where two concurrent requests
    (e.g. a screenshot of the QR code scanned twice at once) could both
    observe an unused token and both succeed. The UPDATE's WHERE clause is
    the single point of truth, evaluated atomically by SQLite for this
    connection's statement.

    Returns False uniformly for "no such token", "already used", and
    "expired" — the caller (GET /login/qr) must not distinguish these in
    its response, since doing so would let an attacker probe token
    validity/expiry (a timing- or response-shape oracle) without needing
    the token to actually work.
    """
    if not token:
        return False
    token_hash = _sha256(token)
    conn = get_connection(config.db_path)
    try:
        cursor = conn.execute(
            "UPDATE login_tokens SET used_at = datetime('now') "
            "WHERE token_hash = ? AND used_at IS NULL AND expires_at > datetime('now')",
            (token_hash,),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def create_pair_code(db_path: Path, label: str | None = None) -> str:
    """Issue a one-time device-pairing code (M-iOS Task 1).

    Structurally identical to create_login_token: `secrets.token_urlsafe(32)`
    (>=128 bits, brute force infeasible even against the short 15-minute TTL —
    same LOGIN_TOKEN_TTL_MINUTES constant), only the SHA-256 hash is ever
    persisted, and the raw code is returned exactly once (to be embedded in the
    tiro://pair QR) and is never recoverable from the database afterward. The
    one difference from login_tokens: what redemption *mints* — a login token
    yields a session cookie for a browser, a pair code yields a long-lived
    api_tokens row for the native client (see consume_pair_code)."""
    code = secrets.token_urlsafe(32)
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO device_pair_codes (code_hash, label, created_at, expires_at) "
            "VALUES (?, ?, datetime('now'), datetime('now', ?))",
            (_sha256(code), label, f"+{LOGIN_TOKEN_TTL_MINUTES} minutes"),
        )
        conn.commit()
    finally:
        conn.close()
    return code


def consume_pair_code(db_path: Path, code: str, device_name: str) -> str | None:
    """Atomically redeem a one-time pairing code into a fresh API token.

    Same atomic single-use discipline as consume_login_token: one
    UPDATE ... WHERE code_hash=? AND used_at IS NULL AND expires_at > now,
    gated on rowcount == 1, is the only safe implementation — a
    SELECT-then-UPDATE has a TOCTOU window where two concurrent redemptions of
    the same code (a screenshot scanned twice at once) could both observe it
    unused and both mint a token.

    Returns None uniformly for "no such code", "already used", and "expired" —
    the caller (POST /api/auth/pair) must not distinguish these (no oracle).
    Only on a winning UPDATE does it mint and return the raw API token named
    `ios:<device_name>` (mirroring the login-token path, which only creates a
    session after a winning consume)."""
    if not code:
        return None
    code_hash = _sha256(code)
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "UPDATE device_pair_codes SET used_at = datetime('now') "
            "WHERE code_hash = ? AND used_at IS NULL AND expires_at > datetime('now')",
            (code_hash,),
        )
        conn.commit()
        if cursor.rowcount != 1:
            return None
    finally:
        conn.close()
    return create_api_token(db_path, f"ios:{device_name}")


def create_api_token(db_path: Path, name: str) -> str:
    token = secrets.token_urlsafe(32)
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO api_tokens (name, token_hash) VALUES (?, ?)",
            (name, _sha256(token)),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def validate_api_token(db_path: Path, token: str) -> bool:
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "UPDATE api_tokens SET last_used_at = datetime('now') WHERE token_hash = ?",
            (_sha256(token),),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def list_api_tokens(db_path: Path) -> list[dict]:
    """List API tokens (metadata only — hashes never leave this module)."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name, created_at, last_used_at FROM api_tokens ORDER BY created_at, id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def revoke_api_token(db_path: Path, token_id: int) -> bool:
    conn = get_connection(db_path)
    try:
        cursor = conn.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def save_password_hash(config: TiroConfig, password_hash: str) -> None:
    """Persist the hash to config.yaml, preserving comments and key order."""
    from tiro.config import persist_config

    persist_config(config, {"auth_password_hash": password_hash})
    config.auth_password_hash = password_hash


MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class NotAuthenticated(Exception):
    """Raised by page routes; app handler redirects to /login."""


def _check_csrf(request: Request) -> None:
    # Cross-site navigations/fetches must never reach the API with ambient
    # cookie auth. Digest/analysis generation moved to POST in M4b (GETs are
    # pure cache reads now), so mutating methods carry the Origin check below;
    # remaining GET exposure is expensive-but-pure reads (export, search).
    # Modern browsers (Chrome 76+, Firefox 90+, Safari 16.4+) send
    # Sec-Fetch-Site; older browsers fail open on the GET path only — residual
    # risk is cross-site read-cost burning (accepted, single-user local app).
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site request rejected")
    if request.method not in MUTATING_METHODS:
        return
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return  # non-browser client (curl, tests) — browsers always send Origin on cross-origin POSTs
    from urllib.parse import urlparse

    origin_host = urlparse(origin).netloc
    if origin_host != request.headers.get("host", ""):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")


def _cookie_authenticated(request: Request) -> bool:
    config = request.app.state.config
    token = request.cookies.get(SESSION_COOKIE)
    return bool(token and validate_session(config.db_path, token))


def is_authenticated(request: Request) -> bool:
    """Read-only auth check for optional detail (no CSRF check, never raises).

    Not fully side-effect-free: the bearer-token path calls validate_api_token,
    which UPDATEs api_tokens.last_used_at on every probe — the one write this
    otherwise read-only check performs.
    """
    config = request.app.state.config
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return validate_api_token(config.db_path, auth_header[7:])
    return _cookie_authenticated(request)


async def require_auth(request: Request) -> None:
    """Dependency for API routers: bearer token or session cookie."""
    config = request.app.state.config
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        if validate_api_token(config.db_path, auth_header[7:]):
            return
        logger.warning(
            "Invalid API token presented from %s",
            request.client.host if request.client else "?",
        )
        raise HTTPException(status_code=401, detail="Invalid API token")
    if _cookie_authenticated(request):
        _check_csrf(request)
        return
    raise HTTPException(status_code=401, detail="Not authenticated")


async def require_page_auth(request: Request) -> None:
    """Dependency for HTML pages: redirect to /login when not signed in."""
    if not _cookie_authenticated(request):
        raise NotAuthenticated()
