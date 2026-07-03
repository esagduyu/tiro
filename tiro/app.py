"""FastAPI application for Tiro."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tiro import __version__, auth
from tiro.config import TiroConfig, load_config
from tiro.database import dir_bytes, get_connection, init_db, migrate_db
from tiro.decay import recalculate_decay
from tiro.vectorstore import init_vectorstore

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent / "frontend"

_DIR_SIZE_CACHE_TTL = 30  # seconds
_dir_size_cache: dict[Path, tuple[float, int]] = {}


def _cached_dir_bytes(path: Path) -> int:
    """`dir_bytes` behind a 30s TTL cache — avoids rglob-walking the chroma/audio
    dirs on every /healthz call (they only grow, so brief staleness is fine)."""
    now = time.monotonic()
    cached = _dir_size_cache.get(path)
    if cached is not None and now - cached[0] < _DIR_SIZE_CACHE_TTL:
        return cached[1]
    size = dir_bytes(path)
    _dir_size_cache[path] = (now, size)
    return size


async def _imap_sync_loop(config: TiroConfig):
    """Background task that checks IMAP inbox on a schedule."""
    from tiro.ingestion.imap import check_imap_inbox

    while True:
        interval = config.imap_sync_interval
        if interval <= 0 or not config.imap_enabled:
            return

        await asyncio.sleep(interval * 60)

        if not config.imap_enabled or config.imap_sync_interval <= 0:
            return

        try:
            result = await asyncio.to_thread(check_imap_inbox, config)
            if result["fetched"] > 0:
                logger.info(
                    "IMAP sync: %d fetched, %d processed, %d skipped, %d failed",
                    result["fetched"], result["processed"],
                    result["skipped"], result["failed"],
                )
            else:
                logger.debug("IMAP sync: no new messages")
        except Exception as e:
            logger.error("IMAP sync failed: %s", e)


async def _vector_retry_loop(config: TiroConfig):
    """Background task that retries pending ChromaDB vector adds on a schedule."""
    from tiro.vectorstore import retry_pending_vectors

    while True:
        interval = config.vector_retry_interval
        if interval <= 0:
            return
        await asyncio.sleep(interval * 60)
        try:
            n = await asyncio.to_thread(retry_pending_vectors, config)
            if n:
                logger.info("Vector retry: indexed %d pending article(s)", n)
        except Exception as e:
            logger.error("Vector retry loop error: %s", e)


def _compute_sleep_until(time_str: str, tz_offset_minutes: int) -> float:
    """Compute seconds until next occurrence of HH:MM in user's timezone.

    Args:
        time_str: Target time as "HH:MM"
        tz_offset_minutes: JS-style getTimezoneOffset() (positive = west of UTC)
    """
    from datetime import timedelta

    hour, minute = int(time_str[:2]), int(time_str[3:5])
    now_utc = datetime.now(timezone.utc)

    # Convert user's local target time to UTC
    # JS getTimezoneOffset() returns minutes: UTC - local (e.g. EST = 300, CET = -60)
    user_offset = timedelta(minutes=-tz_offset_minutes)
    user_tz = timezone(user_offset)

    # Build today's target in user's timezone, then convert to UTC
    user_now = now_utc.astimezone(user_tz)
    target_local = user_now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # If target has already passed today, schedule for tomorrow
    if target_local <= user_now:
        target_local += timedelta(days=1)

    target_utc = target_local.astimezone(timezone.utc)
    delta = (target_utc - now_utc).total_seconds()
    return max(delta, 60)  # At least 60 seconds to avoid tight loops


async def _digest_schedule_loop(config: TiroConfig):
    """Background task that generates + emails digests on schedule."""
    from tiro.intelligence.digest import generate_digest
    from tiro.intelligence.email_digest import send_digest_email

    last_email_date = None

    while True:
        if not config.digest_schedule_enabled:
            return

        sleep_secs = _compute_sleep_until(config.digest_schedule_time, config.digest_timezone_offset)
        logger.info("Digest scheduler: next run in %.0f seconds (at %s)", sleep_secs, config.digest_schedule_time)
        await asyncio.sleep(sleep_secs)

        # Re-check config (may have been disabled while sleeping)
        if not config.digest_schedule_enabled:
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            result = await asyncio.to_thread(
                generate_digest, config, unread_only=config.digest_unread_only
            )
            logger.info("Scheduled digest generated for %s (%d sections)", today, len(result))

            # Auto-email if SMTP configured and haven't sent today
            if config.smtp_user and config.smtp_password and config.digest_email:
                if last_email_date != today:
                    try:
                        await asyncio.to_thread(send_digest_email, config, True)
                        last_email_date = today
                        logger.info("Scheduled digest emailed to %s", config.digest_email)
                    except Exception as e:
                        logger.error("Scheduled digest email failed: %s", e)
        except Exception as e:
            logger.error("Scheduled digest generation failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database and vectorstore on startup."""
    app.state.started_at = time.monotonic()
    config: TiroConfig = app.state.config

    # Ensure library directories exist
    config.articles_dir.mkdir(parents=True, exist_ok=True)
    (config.library / "audio").mkdir(parents=True, exist_ok=True)

    # Initialize SQLite + run migrations
    init_db(config.db_path)
    migrate_db(config.db_path)

    # Initialize ChromaDB with configured embedding model
    init_vectorstore(config.chroma_dir, config.default_embedding_model)

    # Recalculate content decay weights
    recalculate_decay(config)

    # Start IMAP sync background task if configured
    app.state.imap_task = None
    if config.imap_enabled and config.imap_sync_interval > 0:
        app.state.imap_task = asyncio.create_task(_imap_sync_loop(config))
        logger.info("IMAP sync started: every %d minutes", config.imap_sync_interval)

    # Start digest schedule background task if configured
    digest_task = None
    if config.digest_schedule_enabled:
        digest_task = asyncio.create_task(_digest_schedule_loop(config))
        logger.info("Digest schedule started: daily at %s", config.digest_schedule_time)
    app.state.digest_task = digest_task

    # Start vector retry background task if configured
    app.state.vector_retry_task = None
    if config.vector_retry_interval > 0:
        app.state.vector_retry_task = asyncio.create_task(_vector_retry_loop(config))
        logger.info("Vector retry started: every %d minutes", config.vector_retry_interval)

    logger.info("Tiro is ready — library at %s", config.library)
    yield

    # Cancel background tasks on shutdown. Read digest_task/imap_task off
    # app.state (not the local variables from startup) — POST
    # /api/settings/digest-schedule and /api/settings/email can replace
    # app.state.{digest,imap}_task at runtime; using the stale local would
    # cancel a dead task object and leak the live one.
    for task in [
        getattr(app.state, "imap_task", None),
        getattr(app.state, "digest_task", None),
        getattr(app.state, "vector_retry_task", None),
    ]:
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def create_app(config: TiroConfig | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if config is None:
        config = load_config()

    app = FastAPI(
        title="Tiro",
        description="A local-first reading OS for the AI age",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.state.config = config

    # CORS — restrict to the app's own origin (credentials require an exact match)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://localhost:{config.port}",
            f"http://127.0.0.1:{config.port}",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Host-header validation: DNS rebinding sends a victim's browser to
    # 127.0.0.1 with Host: evil.example, bypassing CORS/CSRF origin checks
    # (which compare against the attacker-controlled Host). Reject unknown
    # hosts outright. "testserver" is Starlette's TestClient default and is
    # not publicly resolvable.
    allowed_hosts = {
        f"localhost:{config.port}", f"127.0.0.1:{config.port}",
        "localhost", "127.0.0.1", "testserver",
    }
    if config.host not in ("127.0.0.1", "0.0.0.0", "localhost"):
        allowed_hosts.add(config.host)
        allowed_hosts.add(f"{config.host}:{config.port}")

    # Registered AFTER CORSMiddleware so it wraps it (Starlette: last-added
    # middleware is outermost). Host validation must run before CORS —
    # CORSMiddleware short-circuits OPTIONS preflights without calling the
    # inner app, which would otherwise bypass this check.
    @app.middleware("http")
    async def _validate_host(request: Request, call_next):
        host = request.headers.get("host", "")
        # A single detected LAN IP breaks on offline or multi-homed
        # machines — check against the full candidate set gathered at
        # startup (tiro/cli.py cmd_run), not just the first one found.
        lan_ips = getattr(app.state, "lan_ips", None) or set()
        if host not in allowed_hosts and not any(
            host == f"{ip}:{config.port}" for ip in lan_ips
        ):
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": f"Unrecognized Host header: {host!r}"},
            )
        return await call_next(request)

    # API routers
    from tiro.api.routes_auth import router as auth_router
    from tiro.api.routes_articles import router as articles_router
    from tiro.api.routes_digest import router as digest_router
    from tiro.api.routes_ingest import router as ingest_router
    from tiro.api.routes_search import router as search_router
    from tiro.api.routes_classify import router as classify_router
    from tiro.api.routes_decay import router as decay_router
    from tiro.api.routes_sources import router as sources_router
    from tiro.api.routes_stats import router as stats_router
    from tiro.api.routes_export import router as export_router
    from tiro.api.routes_digest_email import router as digest_email_router
    from tiro.api.routes_settings import router as settings_router
    from tiro.api.routes_audio import router as audio_router
    from tiro.api.routes_graph import router as graph_router
    from tiro.api.routes_filters import router as filters_router
    from tiro.api.routes_tokens import router as tokens_router

    app.include_router(auth_router)
    protected = [
        ingest_router, articles_router, sources_router, digest_router,
        digest_email_router, search_router, classify_router, decay_router,
        stats_router, export_router, settings_router, audio_router,
        graph_router, filters_router, tokens_router,
    ]
    for r in protected:
        app.include_router(r, dependencies=[Depends(auth.require_auth)])

    # Static files and templates
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR / "static")), name="static")
    # Serve custom themes from library/themes/ if directory exists
    library_themes = config.library / "themes"
    library_themes.mkdir(parents=True, exist_ok=True)
    # Open by design: serves user-authored theme CSS only (no user data).
    # Listed in the Phase 0 allowlist; route-walk test enforces the rest.
    app.mount("/library/themes", StaticFiles(directory=str(library_themes)), name="library_themes")
    templates = Jinja2Templates(directory=str(FRONTEND_DIR / "templates"))

    @app.get("/healthz")
    async def healthz(request: Request):
        body = {"status": "ok", "version": app.version}
        config = app.state.config
        if config.auth_password_hash and not auth.is_authenticated(request):
            return body  # open readiness probe: no detail leak

        conn = get_connection(config.db_path)
        try:
            articles = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
        finally:
            conn.close()
        audio_dir = config.library / "audio"
        body["uptime_seconds"] = int(time.monotonic() - app.state.started_at)
        body["stores"] = {
            "articles": articles,
            "db_bytes": config.db_path.stat().st_size if config.db_path.exists() else 0,
            "chroma_bytes": _cached_dir_bytes(config.chroma_dir),
            "audio_files": len(list(audio_dir.glob("*.mp3"))) if audio_dir.exists() else 0,
            "audio_bytes": _cached_dir_bytes(audio_dir),
        }

        def _running(name):
            task = getattr(app.state, name, None)
            return bool(task and not task.done())

        body["tasks"] = {
            "imap": _running("imap_task"),
            "digest": _running("digest_task"),
            "vector_retry": _running("vector_retry_task"),
        }
        return body

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html")

    @app.exception_handler(auth.NotAuthenticated)
    async def _not_authenticated(request: Request, exc: auth.NotAuthenticated):
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/login", status_code=302)

    @app.get("/", response_class=HTMLResponse)
    async def index_redirect():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/inbox")

    @app.get("/inbox", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def inbox_page(request: Request):
        return templates.TemplateResponse(request, "inbox.html")

    @app.get("/digest", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def digest_page(request: Request):
        return templates.TemplateResponse(request, "digest.html")

    @app.get("/articles/{article_id}", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def reader(request: Request, article_id: int):
        return templates.TemplateResponse(request, "reader.html", {"article_id": article_id})

    @app.get("/stats", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def stats_page(request: Request):
        return templates.TemplateResponse(request, "stats.html")

    @app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def settings_page(request: Request):
        return templates.TemplateResponse(request, "settings.html")

    @app.get("/graph", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def graph_page(request: Request):
        return templates.TemplateResponse(request, "graph.html")

    return app
