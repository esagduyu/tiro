"""Configuration loading for Tiro."""

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULTS = {
    "library_path": "./tiro-library",
    "host": "127.0.0.1",
    "port": 8000,
    "default_embedding_model": "all-MiniLM-L6-v2",
    "opus_model": "claude-opus-4-6",
    "haiku_model": "claude-haiku-4-5-20251001",
    "decay_rate_default": 0.95,
    "decay_rate_disliked": 0.90,
    "decay_rate_vip": 0.98,
    "decay_threshold": 0.1,
}


@dataclass
class TiroConfig:
    library_path: str = DEFAULTS["library_path"]
    host: str = DEFAULTS["host"]
    port: int = DEFAULTS["port"]
    default_embedding_model: str = DEFAULTS["default_embedding_model"]
    opus_model: str = DEFAULTS["opus_model"]
    haiku_model: str = DEFAULTS["haiku_model"]
    # AI backend routing (Decision #7). Call sites request a capability tier;
    # these map tiers to (provider, model). Providers: "anthropic",
    # "openai-compatible", "claude-cli", "codex-cli", "fake" (tests).
    ai_heavy_provider: str = "anthropic"
    ai_light_provider: str = "anthropic"
    ai_heavy_model: str | None = None   # None -> opus_model
    ai_light_model: str | None = None   # None -> haiku_model
    ai_openai_base_url: str = "https://api.openai.com/v1"
    ai_openai_api_key: str | None = None
    ai_claude_cli_path: str = "claude"
    ai_codex_cli_path: str = "codex"
    decay_rate_default: float = DEFAULTS["decay_rate_default"]
    decay_rate_disliked: float = DEFAULTS["decay_rate_disliked"]
    decay_rate_vip: float = DEFAULTS["decay_rate_vip"]
    decay_threshold: float = DEFAULTS["decay_threshold"]
    backup_auto_keep: int = 10  # auto-backup retention (0 = keep none)
    vector_retry_interval: int = 5  # minutes, 0 = disabled
    anthropic_api_key: str | None = None
    digest_email: str | None = None
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str | None = None
    imap_password: str | None = None
    imap_label: str = "tiro"
    imap_enabled: bool = False
    imap_sync_interval: int = 15  # minutes, 0 = manual only
    openai_api_key: str | None = None
    tts_voice: str = "nova"
    tts_model: str = "tts-1"
    digest_schedule_enabled: bool = False
    digest_schedule_time: str = "07:00"       # HH:MM in user's local time
    digest_unread_only: bool = False
    digest_timezone_offset: int = 0           # from JS getTimezoneOffset()
    inbox_page_size: int = 50
    theme_light: str = "papyrus"
    theme_dark: str = "roman-night"
    # Reading-session telemetry (Phase 2 M2.3): opt-in, strictly local-only —
    # feeds the future wiki-importance ranking signal (Decision #8).
    reading_telemetry_enabled: bool = False
    # Obsidian-compatible write mode (Phase 2 M2.3): format-only toggle for
    # NEW-article frontmatter -- tags as a YAML list (already the format;
    # unaffected by this flag), `aliases: []`, `created:` (ISO date), and
    # `related:` as `[[stem]]` wikilinks instead of /articles/{id} URLs.
    # Notes/highlights sidecars already key off the article slug regardless
    # of this flag. Bidirectional sync is Phase 2b, not this flag.
    obsidian_compatible_mode: bool = False
    # mDNS/Bonjour discovery (Phase 3 M3.0): opt-in LAN advertisement so
    # phones on the same Wi-Fi can find the server as `{mdns_hostname}.local`
    # instead of typing a raw LAN IP. Disabled by default per the roadmap.
    mdns_enabled: bool = False
    mdns_hostname: str = "tiro"
    # Remote access URL (Phase 3 M3.0/M3.1): the user's Tailscale/remote URL.
    # Set by the /setup/remote wizard (M3.1) or manually; purely
    # informational today (surfaced by `tiro status`), not yet enforced or
    # validated by the server.
    remote_url: str | None = None
    # Reverse-proxy / remote-access hardening (Phase 3 M3.1 Task 4). Entries
    # in extra_allowed_hosts join the Host-header allowlist verbatim
    # (case-insensitive; a bare entry additionally matches with this
    # server's own port appended, mirroring how the static localhost/
    # 127.0.0.1/config.host entries in tiro/app.py's create_app already
    # carry both a bare and a `:port` form) -- e.g. a Tailscale MagicDNS
    # name or a reverse proxy's public hostname. No wildcards: a wildcard
    # would let any subdomain bypass the DNS-rebinding defense this
    # allowlist exists for. Set by the /setup/remote wizard (M3.1) or by
    # hand; env override TIRO_EXTRA_ALLOWED_HOSTS is comma-separated.
    # trust_proxy_headers additionally makes `_login_qr_target` (tiro/app.py)
    # honor X-Forwarded-Proto (scheme only, never X-Forwarded-Host) when
    # running behind a TLS-terminating proxy that forwards plain HTTP
    # locally (e.g. Tailscale Serve) -- default False leaves every existing
    # deployment's behavior untouched.
    extra_allowed_hosts: list[str] = field(default_factory=list)
    trust_proxy_headers: bool = False
    auth_password_hash: str | None = None
    config_path: str | None = None  # set by load_config; never persisted to YAML

    @property
    def library(self) -> Path:
        return Path(self.library_path).resolve()

    @property
    def articles_dir(self) -> Path:
        return self.library / "articles"

    @property
    def db_path(self) -> Path:
        return self.library / "tiro.db"

    @property
    def chroma_dir(self) -> Path:
        return self.library / "chroma"

    @property
    def wiki_dir(self) -> Path:
        return self.library / "wiki"


_ENV_TRUTHY = {"1", "true", "yes", "on"}


def _apply_env_overlay(config: "TiroConfig") -> None:
    """Overlay TIRO_<FIELD_UPPER> environment variables onto an already-built
    config (YAML values + dataclass defaults). Precedence: env > yaml >
    defaults. Every TiroConfig field except config_path is eligible.

    Type coercion is driven by the field's declared type: bool via a
    casefolded membership test against {"1","true","yes","on"} (anything
    else, including ""), int via int(), float via float(), list[str]
    (extra_allowed_hosts, M3.1 Task 4) via a comma-split with whitespace
    stripped and empty entries dropped, everything else (str, and
    Optional[str] fields) verbatim. Never logs values — several fields
    hold secrets (API keys, SMTP/IMAP passwords, the auth hash).
    """
    for fld in fields(config):
        if fld.name == "config_path":
            continue
        env_name = f"TIRO_{fld.name.upper()}"
        if env_name not in os.environ:
            continue
        raw = os.environ[env_name]
        if fld.type is bool:
            value: object = raw.strip().casefold() in _ENV_TRUTHY
        elif fld.type is int:
            value = int(raw)
        elif fld.type is float:
            value = float(raw)
        elif fld.type == list[str]:
            value = [h.strip() for h in raw.split(",") if h.strip()]
        else:
            value = raw
        setattr(config, fld.name, value)


def load_config(config_path: str | Path = "config.yaml") -> TiroConfig:
    """Load configuration from a YAML file, falling back to defaults."""
    path = Path(config_path)
    data: dict = {}

    if path.exists():
        logger.info("Loading config from %s", path)
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    else:
        logger.info("No config file found at %s, using defaults", path)

    # Only pass known fields to the dataclass
    known_fields = {f.name for f in TiroConfig.__dataclass_fields__.values()}
    filtered = {k: v for k, v in data.items() if k in known_fields}

    config = TiroConfig(**filtered)
    config.config_path = str(path)

    _apply_env_overlay(config)

    # Set ANTHROPIC_API_KEY env var from config if not already set
    if config.anthropic_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key

    # Set OPENAI_API_KEY env var from config if not already set
    if config.openai_api_key and not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = config.openai_api_key

    return config


def persist_config(config: TiroConfig, updates: dict) -> None:
    """Merge updates into the YAML file at config.config_path.

    Preserves comments, quoting, and key order (ruamel round-trip) and
    writes atomically (temp file + os.replace) with 0600 permissions —
    the same pattern as auth.save_password_hash, which delegates here.
    Creates the file if it does not exist yet.

    Cross-process note: last-writer-wins whole-file semantics (no locking);
    os.replace on a symlinked config.yaml replaces the symlink itself; no
    fsync before replace (same durability window as save_password_hash).
    """
    from ruamel.yaml import YAML

    if not config.config_path:
        raise ValueError("config has no config_path; cannot persist settings")
    path = Path(config.config_path)
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    data = yaml_rt.load(path.read_text()) if path.exists() else None
    if data is None:
        data = {}
    for key, value in updates.items():
        data[key] = value
    tmp_path = path.with_suffix(".yaml.tmp")
    try:
        with tmp_path.open("w") as f:
            yaml_rt.dump(data, f)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
