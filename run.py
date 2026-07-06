"""Entry point for running the Tiro server."""

import argparse
import logging
import os
import sys

import uvicorn

from tiro.config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    """--cert/--key (M3.0 Task 4): run.py is a second server entry point
    (used directly in dev/Playwright workflows, see CLAUDE.md) alongside
    `tiro run` in tiro/cli.py -- both must accept the same TLS flags and
    validate them identically so uvicorn.run gets consistent
    ssl_certfile/ssl_keyfile behavior regardless of which entry point
    started the server."""
    parser = argparse.ArgumentParser(description="Run the Tiro server directly")
    parser.add_argument("--cert", help="TLS certificate file (must be given with --key)")
    parser.add_argument("--key", help="TLS private key file (must be given with --cert)")
    args = parser.parse_args()
    if bool(args.cert) != bool(args.key):
        parser.error("--cert and --key must be given together")
    return args


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
    args = _parse_args()

    # --cert/--key file-exists check (M3.0 Task 4): both-or-neither was
    # already enforced in _parse_args() as an argparse usage error. This is
    # the runtime check -- must fail clearly BEFORE uvicorn.run() ever
    # starts (mirrors tiro/cli.py's cmd_run; see tiro/tls.py for why the
    # check itself is shared).
    tls_enabled = bool(args.cert and args.key)
    if tls_enabled:
        from tiro.tls import check_tls_files_exist

        try:
            check_tls_files_exist(args.cert, args.key)
        except FileNotFoundError as e:
            print(str(e))
            sys.exit(1)

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

    app = create_app(config, tls_enabled=tls_enabled)

    # Startup warning (M3.0 Task 4): same wording/condition as cmd_run's,
    # driven by the same app.state.insecure_lan_http single source of truth
    # so the log line and the browser banner never disagree.
    if app.state.insecure_lan_http:
        auth_url = (
            f"http://{app.state.lan_ip}:{config.port}"
            if app.state.lan_ip
            else f"http://{config.host}:{config.port}"
        )
        logger.warning(
            "Serving unencrypted HTTP on your local network (%s) — "
            "use Tailscale Serve or `tiro run --cert/--key` for HTTPS.",
            auth_url,
        )

    uvicorn.run(app, host=config.host, port=config.port, ssl_certfile=args.cert, ssl_keyfile=args.key)


if __name__ == "__main__":
    main()
