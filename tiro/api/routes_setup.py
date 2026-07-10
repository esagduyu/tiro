"""First-run onboarding wizard routes (Phase 5 M5.1, spec D6).

Three POST routes the /welcome wizard drives after the password step:
library-path (pristine-only re-point), ai (provider + key), samples (seed the
two packaged public-domain docs). All three share the wizard's own auth gate —
`require_setup_access`: served/accepted while UNCONFIGURED (the same trust
window POST /api/auth/setup accepts — the wizard IS how the password gets set),
and require a real session/token once a password exists. Every config write
goes through `persist_config` (the chokepoint), never a raw YAML write.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tiro import auth
from tiro.config import persist_config
from tiro.database import get_connection, init_db, migrate_db
from tiro.onboarding import seed_samples
from tiro.vectorstore import init_vectorstore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])

# The exact llm.py provider set (Decision #7) — no new abstraction. "skip"
# means "don't configure AI now"; "fake" is a test-only backend, never offered.
VALID_PROVIDERS = {"anthropic", "openai-compatible", "claude-cli", "codex-cli"}


async def require_setup_access(request: Request) -> None:
    """Gate for the wizard's setup routes: unconfigured-OR-authenticated.

    While `auth_password_hash` is unset the pre-password steps must work with no
    session (the same conditional the /welcome page uses, and the same trust
    window POST /api/auth/setup already accepts) — but mutating cross-site
    requests are still rejected via `_check_csrf`, exactly as the setup route
    does. Once a password exists, defer to the normal `require_auth` (session
    cookie or bearer token, with its own CSRF check), so the surface closes the
    moment setup completes.
    """
    config = request.app.state.config
    if not config.auth_password_hash:
        auth._check_csrf(request)
        return
    await auth.require_auth(request)


class LibraryPathBody(BaseModel):
    path: str


@router.post("/library-path")
async def set_library_path(body: LibraryPathBody, request: Request):
    """Re-point a PRISTINE (zero-article) library to a new absolute directory.

    Never moves data: it validates the target is an absolute, creatable,
    empty-or-absent directory, refuses if any article already exists
    (`library_not_pristine`), then persists + re-points the live config and
    bootstraps the store dirs at the new path. Because the library is pristine,
    re-initializing SQLite/ChromaDB at the new location is clean (nothing to
    migrate) — the vectorstore singleton simply rebinds to the new path.
    """
    config = request.app.state.config
    raw = body.path.strip()
    dest = Path(raw)
    if not dest.is_absolute():
        raise HTTPException(status_code=400, detail="Library path must be absolute")

    # Pristine guard: any existing article means we must never move.
    conn = get_connection(config.db_path)
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
    finally:
        conn.close()
    if count > 0:
        raise HTTPException(
            status_code=400,
            detail={"error": "library_not_pristine",
                    "message": "Change the library location before saving any articles."},
        )

    # Target must be empty or absent, and creatable.
    if dest.exists():
        if not dest.is_dir():
            raise HTTPException(status_code=400, detail="Library path exists and is not a directory")
        if any(dest.iterdir()):
            raise HTTPException(status_code=400, detail="Library path must be an empty or new directory")
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"Cannot create library directory: {e}") from e

    # Persist through the chokepoint; on ValueError (defaults-only run with no
    # config file) keep the re-point in memory so the running server uses it.
    try:
        persist_config(config, {"library_path": str(dest)})
    except ValueError:
        logger.warning("No config file — library path set for this run only")

    # Re-point live config and bootstrap the new stores (pristine => clean).
    config.library_path = str(dest)
    config.articles_dir.mkdir(parents=True, exist_ok=True)
    (config.library / "audio").mkdir(parents=True, exist_ok=True)
    config.wiki_dir.mkdir(parents=True, exist_ok=True)
    init_db(config.db_path)
    migrate_db(config.db_path)
    init_vectorstore(config.chroma_dir, config.default_embedding_model)

    logger.info("Library re-pointed to %s during onboarding", dest)
    return {"success": True, "data": {"library_path": str(dest)}}


class AIBody(BaseModel):
    provider: str
    api_key: str | None = None


@router.post("/ai")
async def set_ai_provider(body: AIBody, request: Request):
    """Set the AI provider for both tiers (+ optionally its API key).

    Validated against the exact llm.py provider set; `skip` is a no-op. The key
    lands in the provider-appropriate config field (anthropic_api_key /
    ai_openai_api_key); subscription-CLI backends take no key. The raw key is
    NEVER echoed back (fixed-width mask convention) — the response only reports
    the chosen provider and whether a key was stored.
    """
    config = request.app.state.config
    provider = body.provider.strip()

    if provider == "skip":
        return {"success": True, "data": {"provider": "skip", "key_stored": False}}
    if provider not in VALID_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown AI provider: {provider}")

    updates: dict = {"ai_heavy_provider": provider, "ai_light_provider": provider}
    key = (body.api_key or "").strip()
    key_stored = False
    if key:
        if provider == "anthropic":
            updates["anthropic_api_key"] = key
            key_stored = True
        elif provider == "openai-compatible":
            updates["ai_openai_api_key"] = key
            key_stored = True
        # claude-cli / codex-cli use subscription auth — no key field.

    try:
        persist_config(config, updates)
    except ValueError:
        logger.warning("No config file — AI provider set for this run only")

    config.ai_heavy_provider = provider
    config.ai_light_provider = provider
    if key_stored and provider == "anthropic":
        config.anthropic_api_key = key
    elif key_stored and provider == "openai-compatible":
        config.ai_openai_api_key = key

    logger.info("AI provider set to %s during onboarding (key_stored=%s)", provider, key_stored)
    return {"success": True, "data": {"provider": provider, "key_stored": key_stored}}


@router.post("/samples")
async def add_samples(request: Request):
    """Seed the two packaged public-domain sample docs (offline, idempotent)."""
    config = request.app.state.config
    created = seed_samples(config)
    return {"success": True, "data": {"created": len(created)}}
