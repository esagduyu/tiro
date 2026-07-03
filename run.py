"""Entry point for running the Tiro server."""

import logging
import sys

import uvicorn

from tiro.config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def main():
    config = load_config()

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
