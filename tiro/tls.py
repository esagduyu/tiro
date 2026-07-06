"""Shared TLS flag validation for `tiro run --cert/--key` (M3.0 Task 4).

Both entry points that can start the server -- tiro/cli.py's `cmd_run`
(the `tiro run` subcommand) and run.py's direct `python run.py` entry --
accept the same `--cert`/`--key` pair and must refuse identically before
handing them to uvicorn.run() as ssl_certfile/ssl_keyfile. The both-or-
neither check is argparse-level (each caller's own parser raises the
usage error via `.error()`, since only the caller holds the parser
object); this module owns the one thing genuinely shared: the
file-exists check that must run BEFORE uvicorn starts, since uvicorn's
own failure mode for a missing cert/key is a far less legible traceback
buried inside its TLS setup.
"""

from pathlib import Path


def check_tls_files_exist(cert: str, key: str) -> None:
    """Raise FileNotFoundError with a clear, actionable message if either
    path doesn't exist. Callers only invoke this after already confirming
    both --cert and --key were given (both-or-neither is validated
    separately, at the argparse layer, before this ever runs)."""
    for label, path in (("--cert", cert), ("--key", key)):
        if not Path(path).is_file():
            raise FileNotFoundError(f"{label} file not found: {path}")
