"""Settings API routes."""

import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tiro.config import persist_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _mask_password(pw: str | None) -> str | None:
    """Fixed-width mask: reveals neither characters nor length."""
    return "********" if pw else None


@router.get("/network")
async def get_network_settings(request: Request):
    """Get network/LAN access info."""
    lan_mode = getattr(request.app.state, "lan_mode", False)
    lan_ip = getattr(request.app.state, "lan_ip", None)
    config = request.app.state.config
    return {
        "success": True,
        "data": {
            "lan_mode": lan_mode,
            "lan_ip": lan_ip,
            "lan_url": f"http://{lan_ip}:{config.port}" if lan_ip else None,
            "host": config.host,
            "port": config.port,
        },
    }


@router.get("/email")
async def get_email_settings(request: Request):
    """Get current email configuration (passwords masked)."""
    config = request.app.state.config
    return {
        "success": True,
        "data": {
            "smtp_configured": bool(config.smtp_user and config.smtp_password),
            "smtp_host": config.smtp_host,
            "smtp_port": config.smtp_port,
            "smtp_user": config.smtp_user,
            "smtp_password_masked": _mask_password(config.smtp_password),
            "smtp_use_tls": config.smtp_use_tls,
            "digest_email": config.digest_email,
            "imap_configured": bool(config.imap_user and config.imap_password),
            "imap_host": config.imap_host,
            "imap_port": config.imap_port,
            "imap_user": config.imap_user,
            "imap_password_masked": _mask_password(config.imap_password),
            "imap_label": config.imap_label,
            "imap_enabled": config.imap_enabled,
            "imap_sync_interval": config.imap_sync_interval,
        },
    }


class EmailSettingsUpdate(BaseModel):
    gmail_address: str | None = None
    app_password: str | None = None
    enable_send: bool = False
    enable_receive: bool = False
    imap_label: str = "tiro"
    imap_sync_interval: int = 15


@router.post("/email")
async def update_email_settings(body: EmailSettingsUpdate, request: Request):
    """Update email configuration in config.yaml and reload."""
    config = request.app.state.config

    # Both enable_send and enable_receive may be False — that's a valid
    # request to turn a previously-enabled feature (typically receive/IMAP)
    # off, and disabling everything needs no credentials at all. Credentials
    # are only demanded when actually enabling a feature, and only if none
    # are already stored — an enable post that omits the password reuses the
    # stored smtp_password/imap_password instead of forcing a re-paste.
    enabling = body.enable_send or body.enable_receive
    if enabling and not body.gmail_address:
        raise HTTPException(status_code=400, detail="Gmail address is required to enable email features")
    stored_password = config.smtp_password or config.imap_password
    password = body.app_password or stored_password
    if enabling and not password:
        raise HTTPException(
            status_code=400,
            detail="App password is required (none stored yet — paste your Gmail app password)",
        )

    # Update config.yaml
    updates: dict = {}

    if body.enable_send:
        updates["smtp_host"] = "smtp.gmail.com"
        updates["smtp_port"] = 587
        updates["smtp_user"] = body.gmail_address
        updates["smtp_password"] = password
        updates["smtp_use_tls"] = True
        updates["digest_email"] = body.gmail_address

    if body.enable_receive:
        updates["imap_host"] = "imap.gmail.com"
        updates["imap_port"] = 993
        updates["imap_user"] = body.gmail_address
        updates["imap_password"] = password
        updates["imap_label"] = body.imap_label
        updates["imap_enabled"] = True
        updates["imap_sync_interval"] = body.imap_sync_interval
    else:
        updates["imap_enabled"] = False

    try:
        persist_config(config, updates)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    # Update live config
    if body.enable_send:
        config.smtp_host = "smtp.gmail.com"
        config.smtp_port = 587
        config.smtp_user = body.gmail_address
        config.smtp_password = password
        config.smtp_use_tls = True
        config.digest_email = body.gmail_address

    if body.enable_receive:
        config.imap_host = "imap.gmail.com"
        config.imap_port = 993
        config.imap_user = body.gmail_address
        config.imap_password = password
        config.imap_label = body.imap_label
        config.imap_enabled = True
        config.imap_sync_interval = body.imap_sync_interval
    else:
        config.imap_enabled = False

    logger.info("Email settings updated: send=%s, receive=%s", body.enable_send, body.enable_receive)

    # Dynamically restart the IMAP sync task to reflect the new config, via
    # the scheduler registry (mirrors to app.state.imap_task for back-compat).
    scheduler = request.app.state.scheduler
    await scheduler.stop_and_wait("imap")

    if config.imap_enabled and config.imap_sync_interval > 0:
        from tiro.app import _imap_sync_loop

        scheduler.start("imap", _imap_sync_loop(config))
        logger.info("IMAP sync restarted: every %d min", config.imap_sync_interval)
    else:
        logger.info("IMAP sync disabled")

    return {
        "success": True,
        "data": {
            "smtp_configured": body.enable_send,
            "imap_configured": body.enable_receive,
            "gmail_address": body.gmail_address,
            "imap_label": body.imap_label if body.enable_receive else None,
        },
    }


@router.get("/tts")
async def get_tts_settings(request: Request):
    """Get current TTS configuration."""
    config = request.app.state.config
    return {
        "success": True,
        "data": {
            "tts_configured": bool(config.openai_api_key),
            "openai_api_key_masked": _mask_password(config.openai_api_key),
            "tts_voice": config.tts_voice,
            "tts_model": config.tts_model,
        },
    }


class TTSSettingsUpdate(BaseModel):
    openai_api_key: str | None = None
    tts_voice: str = "nova"
    tts_model: str = "tts-1"


@router.post("/tts")
async def update_tts_settings(body: TTSSettingsUpdate, request: Request):
    """Update TTS configuration."""
    config = request.app.state.config

    if not body.openai_api_key:
        raise HTTPException(status_code=400, detail="OpenAI API key is required")

    updates = {
        "openai_api_key": body.openai_api_key,
        "tts_voice": body.tts_voice,
        "tts_model": body.tts_model,
    }
    try:
        persist_config(config, updates)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    # Update live config
    config.openai_api_key = body.openai_api_key
    config.tts_voice = body.tts_voice
    config.tts_model = body.tts_model
    os.environ["OPENAI_API_KEY"] = body.openai_api_key

    logger.info("TTS settings updated: voice=%s, model=%s", body.tts_voice, body.tts_model)

    return {
        "success": True,
        "data": {
            "tts_configured": True,
            "tts_voice": body.tts_voice,
            "tts_model": body.tts_model,
        },
    }


class DigestScheduleUpdate(BaseModel):
    enabled: bool = False
    time: str = "07:00"           # HH:MM format
    unread_only: bool = False
    timezone_offset: int = 0      # from JS getTimezoneOffset()


@router.get("/digest-schedule")
async def get_digest_schedule(request: Request):
    """Get current digest schedule configuration."""
    config = request.app.state.config
    return {
        "success": True,
        "data": {
            "enabled": config.digest_schedule_enabled,
            "time": config.digest_schedule_time,
            "unread_only": config.digest_unread_only,
            "timezone_offset": config.digest_timezone_offset,
            "email_configured": bool(config.smtp_user and config.smtp_password and config.digest_email),
        },
    }


@router.post("/digest-schedule")
async def update_digest_schedule(body: DigestScheduleUpdate, request: Request):
    """Update digest schedule configuration."""
    # Validate HH:MM format
    if not re.match(r"^\d{2}:\d{2}$", body.time):
        raise HTTPException(status_code=400, detail="Time must be in HH:MM format")
    parts = body.time.split(":")
    hour, minute = int(parts[0]), int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise HTTPException(status_code=400, detail="Invalid time value")

    config = request.app.state.config

    updates = {
        "digest_schedule_enabled": body.enabled,
        "digest_schedule_time": body.time,
        "digest_unread_only": body.unread_only,
        "digest_timezone_offset": body.timezone_offset,
    }
    try:
        persist_config(config, updates)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    # Update live config
    config.digest_schedule_enabled = body.enabled
    config.digest_schedule_time = body.time
    config.digest_unread_only = body.unread_only
    config.digest_timezone_offset = body.timezone_offset

    # Dynamically start/stop scheduler task via the scheduler registry
    # (mirrors to app.state.digest_task for back-compat).
    scheduler = request.app.state.scheduler
    await scheduler.stop_and_wait("digest")

    if body.enabled:
        from tiro.app import _digest_schedule_loop
        scheduler.start("digest", _digest_schedule_loop(config))
        logger.info("Digest schedule started: %s daily", body.time)
    else:
        logger.info("Digest schedule disabled")

    return {
        "success": True,
        "data": {
            "enabled": body.enabled,
            "time": body.time,
            "unread_only": body.unread_only,
            "timezone_offset": body.timezone_offset,
        },
    }


# Required --tiro-* CSS variables for theme validation
REQUIRED_THEME_VARS = [
    "--tiro-bg", "--tiro-bg-surface", "--tiro-bg-hover",
    "--tiro-fg", "--tiro-fg-secondary", "--tiro-muted",
    "--tiro-border", "--tiro-accent", "--tiro-accent-hover",
    "--tiro-secondary", "--tiro-secondary-hover",
    "--tiro-gold", "--tiro-gold-hover",
    "--tiro-sidebar-bg", "--tiro-sidebar-active",
    "--tiro-tier-must-read", "--tiro-tier-summary", "--tiro-tier-discard",
    "--tiro-rate-love", "--tiro-rate-like", "--tiro-rate-dislike",
]


def _list_available_themes(config) -> list[dict]:
    """List available themes from built-in and library theme directories."""
    themes = []

    # Built-in themes
    builtin_dir = Path(__file__).parent.parent / "frontend" / "static" / "themes"
    if builtin_dir.exists():
        for css_file in sorted(builtin_dir.glob("*.css")):
            themes.append({
                "name": css_file.stem,
                "path": f"/static/themes/{css_file.name}",
                "builtin": True,
            })

    # Library custom themes
    custom_dir = config.library / "themes"
    if custom_dir.exists():
        for css_file in sorted(custom_dir.glob("*.css")):
            if not any(t["name"] == css_file.stem for t in themes):
                themes.append({
                    "name": css_file.stem,
                    "path": f"/library/themes/{css_file.name}",
                    "builtin": False,
                })

    return themes


def _validate_theme_css(css_content: str) -> list[str]:
    """Check CSS for required --tiro-* variables. Returns list of missing vars."""
    missing = []
    for var in REQUIRED_THEME_VARS:
        if var + ":" not in css_content:
            missing.append(var)
    return missing


@router.get("/appearance")
async def get_appearance_settings(request: Request):
    """Get current appearance settings (themes, page size)."""
    config = request.app.state.config
    themes = _list_available_themes(config)
    return {
        "success": True,
        "data": {
            "theme_light": config.theme_light,
            "theme_dark": config.theme_dark,
            "inbox_page_size": config.inbox_page_size,
            "themes": themes,
        },
    }


class AppearanceUpdate(BaseModel):
    theme_light: str | None = None
    theme_dark: str | None = None
    inbox_page_size: int | None = None


@router.post("/appearance")
async def update_appearance_settings(body: AppearanceUpdate, request: Request):
    """Update appearance settings (theme selections, page size)."""
    config = request.app.state.config

    updates: dict = {}

    if body.theme_light is not None or body.theme_dark is not None:
        available_names = {t["name"] for t in _list_available_themes(config)}
        for _field_name, value in (("theme_light", body.theme_light), ("theme_dark", body.theme_dark)):
            if value is not None and value not in available_names:
                raise HTTPException(status_code=400, detail=f"Unknown theme: {value}")

    if body.theme_light is not None:
        updates["theme_light"] = body.theme_light
    if body.theme_dark is not None:
        updates["theme_dark"] = body.theme_dark
    if body.inbox_page_size is not None:
        if body.inbox_page_size not in (25, 50, 100, 0):
            raise HTTPException(status_code=400, detail="Page size must be 25, 50, or 100 (0 for all)")
        updates["inbox_page_size"] = body.inbox_page_size

    try:
        persist_config(config, updates)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    # Mutate live config only after the write succeeds.
    if body.theme_light is not None:
        config.theme_light = body.theme_light
    if body.theme_dark is not None:
        config.theme_dark = body.theme_dark
    if body.inbox_page_size is not None:
        config.inbox_page_size = body.inbox_page_size

    logger.info(
        "Appearance updated: light=%s, dark=%s, page_size=%s",
        config.theme_light, config.theme_dark, config.inbox_page_size,
    )

    return {
        "success": True,
        "data": {
            "theme_light": config.theme_light,
            "theme_dark": config.theme_dark,
            "inbox_page_size": config.inbox_page_size,
        },
    }


class ThemeImport(BaseModel):
    name: str
    css: str


@router.post("/theme/import")
async def import_theme(body: ThemeImport, request: Request):
    """Import a custom theme CSS file. Validates required --tiro-* variables."""
    config = request.app.state.config

    # Validate name (alphanumeric + hyphens only)
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", body.name):
        raise HTTPException(status_code=400, detail="Theme name must be lowercase alphanumeric with hyphens")

    if len(body.css) < 50:
        raise HTTPException(status_code=400, detail="CSS content too short")

    if len(body.css) > 50000:
        raise HTTPException(status_code=400, detail="CSS content too large (max 50KB)")

    missing = _validate_theme_css(body.css)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required CSS variables: {', '.join(missing[:5])}"
            + (f" and {len(missing) - 5} more" if len(missing) > 5 else ""),
        )

    # Save to library/themes/
    themes_dir = config.library / "themes"
    themes_dir.mkdir(parents=True, exist_ok=True)
    theme_path = themes_dir / f"{body.name}.css"
    theme_path.write_text(body.css)

    logger.info("Custom theme imported: %s (%d bytes)", body.name, len(body.css))

    return {
        "success": True,
        "data": {
            "name": body.name,
            "path": f"/library/themes/{body.name}.css",
        },
    }


@router.get("/telemetry")
async def get_telemetry_settings(request: Request):
    """Get reading-session telemetry opt-in status (Phase 2 M2.3)."""
    config = request.app.state.config
    return {"success": True, "data": {"enabled": config.reading_telemetry_enabled}}


class TelemetryUpdate(BaseModel):
    enabled: bool


@router.post("/telemetry")
async def update_telemetry_settings(body: TelemetryUpdate, request: Request):
    """Update reading-session telemetry opt-in status. Strictly local-only —
    feeds the future wiki-importance ranking signal (Decision #8)."""
    config = request.app.state.config

    try:
        persist_config(config, {"reading_telemetry_enabled": body.enabled})
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    config.reading_telemetry_enabled = body.enabled

    logger.info("Reading telemetry %s", "enabled" if body.enabled else "disabled")

    return {"success": True, "data": {"enabled": config.reading_telemetry_enabled}}
