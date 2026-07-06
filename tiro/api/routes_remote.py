"""Remote access wizard API routes (Phase 3 M3.1 Task 4: /setup/remote).

Three endpoints back the Tailscale/reverse-proxy wizard page:
- GET  /api/remote/status  — best-effort Tailscale detection, never raises.
- POST /api/remote/config  — validate + persist the user's remote URL,
  optionally allowlisting its hostname live (no restart).
- POST /api/remote/test    — an authenticated, user-initiated HEAD probe of
  a user-supplied URL (see `post_remote_test`'s docstring for the SSRF
  posture — deliberately narrow, not IP-range-blocklisted).

All three are mounted under the existing `protected` router list in
tiro/app.py's create_app (Depends(auth.require_auth)) — no route-walk
allowlist entry needed, and mutating POSTs get CSRF checking for free from
`require_auth` (see auth._check_csrf).
"""

import asyncio
import json
import logging
import shutil
import subprocess
import time
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tiro.config import persist_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/remote", tags=["remote"])

_TAILSCALE_STATUS_TIMEOUT = 3.0  # seconds
_PROBE_TIMEOUT = 5.0  # seconds
_PROBE_MAX_REDIRECTS = 3


def _detect_tailscale(port: int) -> dict:
    """Best-effort local Tailscale detection. NEVER raises: every failure
    mode (binary not found, daemon not running, `status --json` timing out,
    garbage/partial JSON, an unexpected shape missing `Self`/`DNSName`)
    degrades to "installed but MagicDNS name unknown" rather than
    propagating — this is UX polish for a setup wizard, not a security
    boundary, so silent degradation is the right failure mode.

    `serve_command` is derived purely from `port` (not from whether
    `status --json` itself succeeded) — it's just a suggested command
    string, always safe to show once the `tailscale` binary is present.
    """
    if shutil.which("tailscale") is None:
        return {"tailscale_installed": False, "magicdns_name": None, "serve_command": None}

    magicdns_name = None
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=_TAILSCALE_STATUS_TIMEOUT,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            self_info = data.get("Self") or {}
            dns_name = (self_info.get("DNSName") or "").rstrip(".")
            magicdns_name = dns_name or None
    except Exception:
        logger.debug("Tailscale status detection failed (non-fatal)", exc_info=True)
        magicdns_name = None

    return {
        "tailscale_installed": True,
        "magicdns_name": magicdns_name,
        "serve_command": f"tailscale serve --bg {port}",
    }


@router.get("/status")
async def get_remote_status(request: Request):
    config = request.app.state.config
    # to_thread: the subprocess call blocks up to 3s on a hung tailscaled —
    # never park the event loop on it (same convention as the LLM calls).
    detection = await asyncio.to_thread(_detect_tailscale, config.port)
    return {
        "success": True,
        "data": {
            "tailscale_installed": detection["tailscale_installed"],
            "magicdns_name": detection["magicdns_name"],
            "remote_url": config.remote_url,
            "serve_command": detection["serve_command"],
        },
    }


class RemoteConfigRequest(BaseModel):
    remote_url: str
    allow_hostname: bool = False


@router.post("/config")
async def post_remote_config(request: Request, body: RemoteConfigRequest):
    parsed = urlparse(body.remote_url)
    # `not parsed.netloc` alone lets a hostless authority like "https://:8000"
    # through -- netloc is the non-empty string ":8000", but urlparse's own
    # `.hostname` property is None for it (nothing before the port/`@`).
    # Require BOTH so a scheme-only-plus-port URL 400s instead of persisting
    # as a "remote_url" nothing can actually reach.
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not parsed.hostname:
        raise HTTPException(status_code=400, detail="remote_url must be a valid http(s) URL")

    config = request.app.state.config
    updates: dict = {"remote_url": body.remote_url}
    extra_hosts = list(config.extra_allowed_hosts)

    if body.allow_hostname:
        # Bare hostname only (no port) -- _validate_host's extra-hosts match
        # already tries the bare form both alone and with THIS server's own
        # port appended, so storing the bare form covers both "reached
        # directly on this server's port" and "reached via a proxy on the
        # same port" without duplicating port-specific variants here.
        hostname = (parsed.hostname or "").lower()
        if hostname and hostname not in [h.lower() for h in extra_hosts]:
            extra_hosts.append(hostname)
        # A proxy on a non-default port sends "host:port" as the Host header;
        # the bare form only matches bare or :{this server's port}, so store
        # the explicit host:port variant too when the URL names one.
        default_port = 443 if parsed.scheme == "https" else 80
        if parsed.port and parsed.port != default_port:
            with_port = f"{hostname}:{parsed.port}"
            if hostname and with_port not in [h.lower() for h in extra_hosts]:
                extra_hosts.append(with_port)
        updates["extra_allowed_hosts"] = extra_hosts

    persist_config(config, updates)
    config.remote_url = body.remote_url
    if body.allow_hostname:
        config.extra_allowed_hosts = extra_hosts
        # Live update (no restart): mirrors how mdns_name is populated onto
        # app.state dynamically -- _validate_host reads app.state.extra_
        # allowed_hosts fresh on every request, so this takes effect on the
        # very next request.
        request.app.state.extra_allowed_hosts = {h.lower() for h in extra_hosts}

    return {
        "success": True,
        "data": {
            "remote_url": config.remote_url,
            "extra_allowed_hosts": config.extra_allowed_hosts,
        },
    }


class RemoteTestRequest(BaseModel):
    url: str | None = None


@router.post("/test")
async def post_remote_test(request: Request, body: RemoteTestRequest):
    """Server-side HEAD probe of a URL, for the wizard's "Test connection"
    button. SSRF posture (deliberately narrow): this is an authenticated,
    user-initiated probe of a URL the SAME user just typed (or previously
    saved as their own remote_url) into a single-user local app they
    already control -- there is no untrusted-third-party input here for an
    attacker to redirect at internal services. The guardrails that matter
    for THIS threat model are the scheme allowlist (http/https only -- no
    `file://`, `gopher://`, etc.) and the timeout/redirect cap (bound worst-
    case latency and hop count); an IP-range blocklist (rejecting RFC1918/
    loopback/link-local targets, the standard SSRF hardening for a
    multi-tenant server accepting arbitrary attacker-supplied URLs) would
    be actively counterproductive here -- testing a LAN IP or 127.0.0.1 is
    exactly the legitimate use case for a self-hosted reader confirming its
    own reachability. Not added; see the task brief's own framing.
    """
    config = request.app.state.config
    target = body.url or config.remote_url
    if not target:
        raise HTTPException(
            status_code=400,
            detail="No URL to test -- pass one or save a remote_url first",
        )

    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="URL must be http or https")

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=_PROBE_TIMEOUT,
            follow_redirects=True,
            max_redirects=_PROBE_MAX_REDIRECTS,
        ) as http_client:
            resp = await http_client.head(target)
        latency_ms = int((time.monotonic() - start) * 1000)
        return {
            "success": True,
            "data": {
                "ok": resp.status_code < 400,
                "status_code": resp.status_code,
                "latency_ms": latency_ms,
                "error": None,
            },
        }
    except httpx.TimeoutException:
        return {
            "success": True,
            "data": {"ok": False, "status_code": None, "latency_ms": None, "error": "timeout"},
        }
    except httpx.TooManyRedirects:
        return {
            "success": True,
            "data": {
                "ok": False, "status_code": None, "latency_ms": None,
                "error": "too_many_redirects",
            },
        }
    except httpx.HTTPError as e:
        return {
            "success": True,
            "data": {"ok": False, "status_code": None, "latency_ms": None, "error": str(e)},
        }
