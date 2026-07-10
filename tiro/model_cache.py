"""Seed the Hugging Face hub cache with a bundled embedding-model snapshot.

Spec D1 (Phase 5 desktop): the frozen desktop binary ships the
``all-MiniLM-L6-v2`` snapshot as data files. On first launch the entry point
copies it into the standard HF hub cache location **iff absent**, then lets
``tiro.vectorstore.init_vectorstore`` load through the completely normal code
path (no ``HF_HOME`` redirection to a read-only bundle dir — a user who
overrides ``default_embedding_model`` still downloads that model exactly as
today, with no read-only-dir write failures).

The copy is idempotent, best-effort, and never raises: a failed or missing
seed simply falls back to HF's own download path (online) — the same behavior
Tiro has always had.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# The HF hub cache directory name for sentence-transformers/all-MiniLM-L6-v2.
# External references (the bundle layout, the cache destination) key on this.
MODEL_CACHE_DIRNAME = "models--sentence-transformers--all-MiniLM-L6-v2"


def hf_hub_cache_dir() -> Path:
    """Resolve the HF hub cache directory the same way huggingface_hub does,
    but purely from the current environment so it stays testable and honors a
    process that sets ``HF_HUB_CACHE`` / ``HF_HOME`` / ``XDG_CACHE_HOME``
    before we run (the smoke script points ``HF_HOME`` at an empty tmp dir to
    prove the seeding path).

    Precedence mirrors huggingface_hub.constants: ``HF_HUB_CACHE`` wins, else
    ``$HF_HOME/hub``, else ``$XDG_CACHE_HOME/huggingface/hub``, else
    ``~/.cache/huggingface/hub``.
    """
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    if os.environ.get("XDG_CACHE_HOME"):
        return Path(os.environ["XDG_CACHE_HOME"]) / "huggingface" / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def seed_embedding_model_cache(bundle_dir: Path | str | None) -> bool:
    """Copy the bundled model snapshot into the HF hub cache iff absent.

    ``bundle_dir`` is the directory that *contains* the
    ``models--sentence-transformers--all-MiniLM-L6-v2`` tree (as staged into
    the frozen app's data files). Returns ``True`` when a copy happened,
    ``False`` otherwise (already cached, bundle missing/malformed, or any
    error). Never raises — embedding-model availability degrades to HF's
    normal online download, exactly as before this function existed.
    """
    try:
        if bundle_dir is None:
            return False
        src = Path(bundle_dir) / MODEL_CACHE_DIRNAME
        if not src.is_dir():
            logger.debug("No bundled embedding model at %s — skipping seed", src)
            return False

        dest = hf_hub_cache_dir() / MODEL_CACHE_DIRNAME
        if dest.exists():
            logger.debug("Embedding model already cached at %s — skipping seed", dest)
            return False

        dest.parent.mkdir(parents=True, exist_ok=True)
        # Copy into a temp sibling then atomically rename, so a crash mid-copy
        # never leaves a half-populated cache dir that would look "present".
        tmp = dest.parent / (MODEL_CACHE_DIRNAME + ".seed-tmp")
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp)
        os.replace(tmp, dest)
        logger.info("Seeded embedding model cache: %s -> %s", src, dest)
        return True
    except Exception as exc:  # never raise into startup
        logger.warning("Embedding model cache seed failed (%s) — will fall back to download", exc)
        return False
