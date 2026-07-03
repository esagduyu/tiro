"""Authentication routes: login, logout, first-run setup, status."""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tiro import auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

MIN_PASSWORD_LENGTH = 8


class PasswordBody(BaseModel):
    password: str


def _session_response(request: Request, payload: dict) -> JSONResponse:
    config = request.app.state.config
    token = auth.create_session(config.db_path)
    response = JSONResponse({"success": True, "data": payload})
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=auth.SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@router.post("/login")
async def login(body: PasswordBody, request: Request):
    auth._check_csrf(request)
    config = request.app.state.config
    if not config.auth_password_hash:
        raise HTTPException(status_code=403, detail="No password configured — complete setup first")
    if not auth.verify_password(body.password, config.auth_password_hash):
        logger.warning("Failed login attempt from %s", request.client.host if request.client else "?")
        raise HTTPException(status_code=401, detail="Wrong password")
    return _session_response(request, {"logged_in": True})


@router.post("/setup")
async def setup(body: PasswordBody, request: Request):
    auth._check_csrf(request)
    config = request.app.state.config
    if config.auth_password_hash:
        raise HTTPException(status_code=403, detail="Password already configured")
    if len(body.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
        )
    password_hash = auth.hash_password(body.password)
    try:
        auth.save_password_hash(config, password_hash)
    except ValueError:
        # No config file path (e.g. defaults-only run): keep it in memory so
        # the running server is protected; warn that it won't survive restart.
        logger.warning("No config file — password set for this run only")
        config.auth_password_hash = password_hash
    return _session_response(request, {"configured": True})


@router.post("/logout")
async def logout(request: Request):
    """Log out. Open by design (no auth dependency): logging out with an
    expired/absent session is a harmless no-op, and gating it would strand
    clients holding a dead cookie. CSRF is still checked (M-12) — cross-site
    Sec-Fetch-Site is rejected the same as any other route; a hostile page
    forcing a same-window logout is low severity but there's no reason to
    special-case it."""
    auth._check_csrf(request)
    config = request.app.state.config
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        auth.destroy_session(config.db_path, token)
    response = JSONResponse({"success": True, "data": {"logged_out": True}})
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


@router.get("/status")
async def status(request: Request):
    config = request.app.state.config
    token = request.cookies.get(auth.SESSION_COOKIE)
    authenticated = bool(token and auth.validate_session(config.db_path, token))
    return {
        "success": True,
        "data": {"configured": bool(config.auth_password_hash), "authenticated": authenticated},
    }
