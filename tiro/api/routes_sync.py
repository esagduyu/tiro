"""Sync API routes (S5.7) — the FROZEN route set.

GET/POST /api/settings/sync (settings + dynamic scheduler restart, imap
pattern), POST /api/sync/now (manual cycle; 409 while one runs in-process),
POST /api/sync/repair (typed-confirm backend wipe-and-reseed).

Secrets follow the email-settings precedent: masked on GET via
routes_settings._mask_password (reused, never forked), and a posted secret
equal to None/""/the mask means "keep the stored value". Any posted string
that pyyaml (YAML 1.1) would re-read as a boolean (`on`/`off`/`no`/...) is
persisted through config.yaml_quote — a plain `on`/`off` scalar would
otherwise round-trip as a boolean and poison resolve_encryption (or a
bucket named "no").
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from tiro.api.routes_settings import _mask_password
from tiro.config import persist_config, yaml_quote
from tiro.sync.engine import (
    SyncConfigError,
    adapter_for_config,
    load_sync_status,
    repair,
    resolve_encryption,
    sync_cycle,
    sync_cycle_running,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sync"])

_SECRET_MASK = "********"  # what _mask_password returns for a set secret

# Strings pyyaml's YAML 1.1 loader parses as booleans — any posted value
# in this set must persist through yaml_quote or it round-trips as a bool.
_YAML_BOOL_STRINGS = {"on", "off", "yes", "no", "true", "false", "y", "n"}

# Request-body field -> TiroConfig field (FROZEN key names, spec §8).
_FIELD_MAP = {
    "enabled": "sync_enabled",
    "backend": "sync_backend",
    "path": "sync_path",
    "s3_endpoint": "sync_s3_endpoint",
    "s3_bucket": "sync_s3_bucket",
    "s3_access_key": "sync_s3_access_key",
    "s3_secret_key": "sync_s3_secret_key",
    "webdav_url": "sync_webdav_url",
    "webdav_user": "sync_webdav_user",
    "webdav_password": "sync_webdav_password",
    "encrypt": "sync_encrypt",
    "interval_s": "sync_interval_s",
}
_SECRET_FIELDS = {"s3_access_key", "s3_secret_key", "webdav_password"}

# Per-backend required config keys for enabling sync (mirrors
# adapter_for_config's validation — the 400 names the missing keys).
_REQUIRED_FIELDS = {
    "filesystem": ("sync_path",),
    "s3": ("sync_s3_endpoint", "sync_s3_bucket",
           "sync_s3_access_key", "sync_s3_secret_key"),
    "webdav": ("sync_webdav_url", "sync_webdav_user", "sync_webdav_password"),
}


@router.get("/api/settings/sync")
async def get_sync_settings(request: Request):
    """Sync status + configuration (secrets masked, identity never leaves)."""
    config = request.app.state.config
    try:
        resolved: object = resolve_encryption(config)
    except SyncConfigError:
        resolved = "invalid"
    data = {
        **load_sync_status(config),
        "backend": config.sync_backend,
        "interval_s": config.sync_interval_s,
        "encrypt": config.sync_encrypt,
        "encrypt_resolved": resolved,
        "path": config.sync_path,
        "s3_endpoint": config.sync_s3_endpoint,
        "s3_bucket": config.sync_s3_bucket,
        "s3_access_key_masked": _mask_password(config.sync_s3_access_key),
        "s3_secret_key_masked": _mask_password(config.sync_s3_secret_key),
        "webdav_url": config.sync_webdav_url,
        "webdav_user": config.sync_webdav_user,
        "webdav_password_masked": _mask_password(config.sync_webdav_password),
        "identity_masked": _mask_password(config.sync_identity),
    }
    return {"success": True, "data": data}


class SyncSettings(BaseModel):
    enabled: bool | None = None
    backend: str | None = None
    path: str | None = None
    s3_endpoint: str | None = None
    s3_bucket: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    webdav_url: str | None = None
    webdav_user: str | None = None
    webdav_password: str | None = None
    encrypt: str | None = None
    interval_s: int | None = None
    confirm_unencrypted: str | None = None


def _resolves_on(backend: str, encrypt: str) -> bool:
    """resolve_encryption's rule over an EFFECTIVE (not-yet-applied) view."""
    if encrypt == "on":
        return True
    if encrypt == "off":
        return False
    return backend in ("s3", "webdav")


@router.post("/api/settings/sync")
async def update_sync_settings(body: SyncSettings, request: Request):
    """Update sync settings; validate, persist, restart the loop (imap
    pattern). All fields optional — omitted means unchanged."""
    config = request.app.state.config

    if body.backend is not None and body.backend not in _REQUIRED_FIELDS:
        raise HTTPException(
            status_code=400, detail="backend must be filesystem, s3 or webdav")
    if body.encrypt is not None and body.encrypt not in ("auto", "on", "off"):
        raise HTTPException(
            status_code=400, detail="encrypt must be auto, on or off")
    if body.interval_s is not None and body.interval_s < 0:
        raise HTTPException(
            status_code=400, detail="interval_s must be >= 0 (0 = manual only)")

    # Changes to apply + the effective post-update view (body-else-config).
    # A secret that is None, empty, or the mask means UNCHANGED (the
    # email-settings precedent — the UI posts the mask back on save).
    changes: dict = {}
    for body_field, cfg_field in _FIELD_MAP.items():
        value = getattr(body, body_field)
        if value is None:
            continue
        if body_field in _SECRET_FIELDS and value in ("", _SECRET_MASK):
            continue
        changes[cfg_field] = value
    effective = {cfg_field: changes.get(cfg_field, getattr(config, cfg_field))
                 for cfg_field in _FIELD_MAP.values()}
    new_backend = effective["sync_backend"]
    new_on = _resolves_on(new_backend, effective["sync_encrypt"])

    # Typed confirm: the ceremony guards the END STATE — plaintext on a
    # network backend — not just the downgrade transition (matches the
    # CLI's posture; review M1). A single POST from a filesystem/auto
    # config straight to {backend: s3, encrypt: off} needs the ceremony
    # too; only an idempotent re-save of an ALREADY-plaintext network
    # config stays ceremony-free.
    try:
        currently_on = resolve_encryption(config)
    except SyncConfigError:
        # The stored pin is invalid — this POST is (or precedes) the fix;
        # no ceremony over a value that never resolved ON.
        currently_on = False
    already_plain_network = (config.sync_backend in ("s3", "webdav")
                            and not currently_on)
    if (new_backend in ("s3", "webdav") and not new_on
            and not already_plain_network
            and body.confirm_unencrypted != "UNENCRYPTED"):
        raise HTTPException(status_code=400, detail=(
            "turning encryption off stores your library as PLAINTEXT on a "
            'network backend — resend with {"confirm_unencrypted": '
            '"UNENCRYPTED"} to confirm'))

    if effective["sync_enabled"]:
        missing = [f for f in _REQUIRED_FIELDS[new_backend]
                   if not str(effective[f] or "").strip()]
        if missing:
            raise HTTPException(status_code=400, detail=(
                f"cannot enable sync: the {new_backend} backend requires "
                + ", ".join(missing)))
        identity = config.sync_identity
        if new_on and not (identity.strip()
                           if isinstance(identity, str) else identity):
            raise HTTPException(status_code=400, detail=(
                "run `tiro sync setup` first — no sync identity on this "
                "device"))

    if changes:
        updates = dict(changes)
        # yaml_quote ANY string that pyyaml (YAML 1.1) would re-read as a
        # boolean — not just sync_encrypt: an S3 bucket literally named
        # "no" must round-trip as the string "no" (review m5).
        for key, value in updates.items():
            if isinstance(value, str) and value.lower() in _YAML_BOOL_STRINGS:
                updates[key] = yaml_quote(value)
        try:
            persist_config(config, updates)
        except ValueError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        for cfg_field, value in changes.items():
            setattr(config, cfg_field, value)
        logger.info("Sync settings updated: %s", ", ".join(sorted(changes)))

    # Dynamically restart the sync loop to reflect the new config, via the
    # scheduler registry (the landed imap pattern — stop_and_wait so an
    # in-flight cycle's to_thread body can't overlap its successor).
    scheduler = request.app.state.scheduler
    await scheduler.stop_and_wait("sync")
    if config.sync_enabled and config.sync_interval_s > 0:
        from tiro.app import _make_sync_task

        scheduler.start_periodic("sync", _make_sync_task(config))
        logger.info("Sync loop restarted: every %d seconds",
                    config.sync_interval_s)
    else:
        logger.info("Sync loop disabled")

    return {
        "success": True,
        "data": {
            "enabled": config.sync_enabled,
            "interval_s": config.sync_interval_s,
        },
    }


class SyncNowRequest(BaseModel):
    accept_mass_delete: bool = False


@router.post("/api/sync/now")
async def sync_now(body: SyncNowRequest, request: Request):
    """Run one sync cycle now. 409 while a cycle is already running in this
    process; 400 on a config problem (probed up front so the caller gets
    adapter_for_config's own message instead of a recorded error report)."""
    config = request.app.state.config
    if sync_cycle_running():
        raise HTTPException(status_code=409, detail="sync_running")
    # Build-then-close probe: one source of validation truth
    # (adapter_for_config); the cycle constructs its own adapter.
    try:
        probe = adapter_for_config(config)
    except SyncConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        await probe.aclose()
    except Exception:  # noqa: S110 - best-effort close of a probe adapter
        pass
    report = await sync_cycle(config,
                              accept_mass_delete=body.accept_mass_delete)
    return {"success": True, "data": report.as_dict()}


class SyncRepairRequest(BaseModel):
    confirm: str = ""


@router.post("/api/sync/repair")
async def sync_repair(body: SyncRepairRequest, request: Request):
    """Wipe the backend's sync state and re-seed it from THIS device's
    library (engine.repair). Destructive for other devices' un-pulled
    history, so it takes the same typed confirmation as the CLI."""
    config = request.app.state.config
    if sync_cycle_running():
        raise HTTPException(status_code=409, detail="sync_running")
    if body.confirm != "REPAIR":
        return JSONResponse(status_code=400, content={
            "error": "confirm_required",
            "detail": 'body must be {"confirm": "REPAIR"}',
        })
    try:
        adapter = adapter_for_config(config)
    except SyncConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        report = await repair(config, adapter)
    finally:
        try:
            await adapter.aclose()
        except Exception:  # noqa: S110 - best-effort close
            pass
    return {"success": True, "data": report.as_dict()}
