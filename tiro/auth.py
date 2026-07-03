"""Single-user authentication: password hashing, sessions, API tokens.

Sessions and API tokens are opaque random values; only SHA-256 hashes are
stored. Session cookies slide: validation extends expiry back to the full
TTL once more than a day of it has been consumed.
"""

import hashlib
import logging
import os
import secrets
from pathlib import Path

import bcrypt
from fastapi import HTTPException, Request

from tiro.config import TiroConfig
from tiro.database import get_connection

logger = logging.getLogger(__name__)

SESSION_COOKIE = "tiro_session"
SESSION_TTL_DAYS = 30


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
    from ruamel.yaml import YAML

    if not config.config_path:
        raise ValueError("config has no config_path; cannot persist password")
    path = Path(config.config_path)
    yaml = YAML()
    yaml.preserve_quotes = True
    data = yaml.load(path.read_text()) if path.exists() else None
    if data is None:
        data = {}
    data["auth_password_hash"] = password_hash
    tmp_path = path.with_suffix(".yaml.tmp")
    try:
        with tmp_path.open("w") as f:
            yaml.dump(data, f)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
    config.auth_password_hash = password_hash


MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class NotAuthenticated(Exception):
    """Raised by page routes; app handler redirects to /login."""


def _check_csrf(request: Request) -> None:
    if request.method not in MUTATING_METHODS:
        return
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return  # non-browser client (curl, tests) — cookie theft via browser sends Origin
    from urllib.parse import urlparse

    origin_host = urlparse(origin).netloc
    if origin_host != request.headers.get("host", ""):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")


def _cookie_authenticated(request: Request) -> bool:
    config = request.app.state.config
    token = request.cookies.get(SESSION_COOKIE)
    return bool(token and validate_session(config.db_path, token))


async def require_auth(request: Request) -> None:
    """Dependency for API routers: bearer token or session cookie."""
    config = request.app.state.config
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        if validate_api_token(config.db_path, auth_header[7:]):
            return
        raise HTTPException(status_code=401, detail="Invalid API token")
    if _cookie_authenticated(request):
        _check_csrf(request)
        return
    raise HTTPException(status_code=401, detail="Not authenticated")


async def require_page_auth(request: Request) -> None:
    """Dependency for HTML pages: redirect to /login when not signed in."""
    if not _cookie_authenticated(request):
        raise NotAuthenticated()
