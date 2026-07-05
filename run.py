"""Entry point for running the Tiro server."""

import logging
import os
import sys

import uvicorn

from tiro.config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _config_path() -> str:
    """Config path for run.py, mirroring tiro/mcp/server.py's _config_path():
    honors TIRO_CONFIG (absolute path) so a server started with a CWD that
    doesn't contain config.yaml (e.g. a launchd/systemd unit, or a Docker
    container with config mounted elsewhere) doesn't silently fall back to
    defaults instead of the intended file. A prior session was bitten by
    run.py ignoring TIRO_CONFIG while the MCP server honored it — keep both
    in sync."""
    return os.environ.get("TIRO_CONFIG", "config.yaml")


def main():
    config = load_config(_config_path())

    # Same refusal as `tiro run` (tiro/cli.py cmd_run): a non-loopback host
    # — whether from --lan or a bare `host: "0.0.0.0"` in config.yaml —
    # must not bind without a password. run.py has no --insecure-no-auth
    # escape hatch; use `uv run tiro run --lan --insecure-no-auth` for that.
    if config.host not in ("127.0.0.1", "localhost") and not config.auth_password_hash:
        print(f"Binding to {config.host} requires a password so other devices can't read your library.")
        print("Set one with:  uv run tiro set-password")
        sys.exit(1)

    # Import here so config is loaded before app creation
    from tiro.app import create_app

    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
