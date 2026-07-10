"""Frozen-binary entry point for the Tiro server (Phase 5 / M5.0, spec D1).

Mirrors run.py's startup contract but for a PyInstaller onedir bundle that a
Tauri sidecar (Task 5) launches with env only — no argv, no CWD assumptions:

  1. Seed the bundled embedding-model snapshot into the HF hub cache (iff
     absent) so the very first launch reaches a working, offline vector store
     through the completely normal init_vectorstore path (spec D1).
  2. load_config honoring TIRO_CONFIG (ON-8 overlay; TIRO_HOST/TIRO_PORT etc.
     are applied by the same load_config env overlay).
  3. create_app, then uvicorn.run on the effective host/port.

Kept deliberately thin: all real logic lives in the tiro package so the frozen
build and the normal `uv run` tree share one code path.
"""

import logging
import multiprocessing
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tiro.desktop")


def _bundle_root() -> Path:
    """Directory holding bundled data files. In a frozen app PyInstaller sets
    sys._MEIPASS (the onedir `_internal` dir in 6.x); unfrozen we return the
    repo dir, where seeding is a harmless no-op (the model is already cached)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def main() -> None:
    from tiro.model_cache import seed_embedding_model_cache

    # hf_model/<models--...> is staged next to the app's other datas.
    seeded = seed_embedding_model_cache(_bundle_root() / "hf_model")
    logger.info("Embedding model cache seed: %s", "copied" if seeded else "already-present-or-skipped")

    from tiro.config import load_config

    config_path = os.environ.get("TIRO_CONFIG", "config.yaml")
    config = load_config(config_path)
    logger.info("Loaded config from %s (library=%s)", config_path, config.library_path)

    # Same non-loopback-needs-password refusal as run.py / `tiro run`.
    if config.host not in ("127.0.0.1", "localhost") and not config.auth_password_hash:
        logger.error(
            "Binding to %s requires a password so other devices can't read your library.",
            config.host,
        )
        sys.exit(1)

    import uvicorn

    from tiro.app import create_app

    app = create_app(config)
    logger.info("Starting Tiro server on %s:%s", config.host, config.port)
    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    # MUST be the first thing to run in the frozen process. Torch/chromadb spawn
    # helper processes (DataLoader workers, resource_tracker); without this a
    # frozen child re-executes the bootloader and re-enters main() instead of the
    # multiprocessing worker bootstrap, leaking orphaned children that outlive a
    # parent kill. Verified empirically (boot -> kill parent -> no survivors).
    multiprocessing.freeze_support()
    main()
