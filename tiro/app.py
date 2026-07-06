"""FastAPI application for Tiro."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tiro import __version__, auth
from tiro.config import TiroConfig, load_config
from tiro.database import dir_bytes, get_connection, init_db, migrate_db
from tiro.decay import recalculate_decay
from tiro.scheduler import Scheduler
from tiro.vectorstore import init_vectorstore

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent / "frontend"

# Single source of truth for static cache busting. Templates use
# `?v={{ static_v }}`; bump ONLY this constant when changing static JS/CSS.
STATIC_VERSION = "63"


def _theme_href(config: TiroConfig, name: str, fallback: str) -> str:
    """Resolve a configured theme name to a servable href.

    Builtin themes ship under frontend/static/themes/; custom themes live in
    the user's library under themes/ (served via the /library/themes mount).
    Falls back to the given builtin fallback name if neither exists.
    """
    builtin = FRONTEND_DIR / "static" / "themes" / f"{name}.css"
    custom = config.library / "themes" / f"{name}.css"
    if builtin.exists():
        return f"/static/themes/{name}.css?v={STATIC_VERSION}"
    if custom.exists():
        return f"/library/themes/{name}.css?v={STATIC_VERSION}"
    return f"/static/themes/{fallback}.css?v={STATIC_VERSION}"


def _theme_context(request: Request) -> dict:
    """Server-resolved context injected into every page template render:
    theme hrefs plus (M3.0 Task 4) the `insecure_lan_http` flag driving
    base.html's dismissable warning banner. Takes the request (not just
    config) so it can also read `app.state.insecure_lan_http`, which is
    computed once in create_app from the effective bind host + whether TLS
    is active — extend this single function for any future per-request
    page-context flag rather than threading a new kwarg through every route
    handler individually."""
    config = request.app.state.config
    return {
        "theme_light_href": _theme_href(config, config.theme_light, "papyrus"),
        "theme_dark_href": _theme_href(config, config.theme_dark, "roman-night"),
        "insecure_lan_http": getattr(request.app.state, "insecure_lan_http", False),
    }


def _qr_svg(data: str) -> str:
    """Render `data` as an inline SVG string (no XML declaration/namespace,
    no width/height so CSS controls sizing) — embedded directly into the
    /setup/qr template body, no external asset or data: URI needed. Fixed
    black-on-white module colors regardless of the active theme: QR
    scanners rely on maximum contrast, and this SVG is only ever shown
    inside a light card in the template (see qr_setup.html)."""
    import io

    import segno

    qr = segno.make(data, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", xmldecl=False, svgns=False, omitsize=True, border=2, scale=6)
    return buf.getvalue().decode()


def _login_qr_target(request: Request, token: str) -> str:
    """URL a phone should hit to redeem a QR login token. Deliberately uses
    the raw `Host` request header (not `request.url.netloc`) and the
    request's own scheme (honors LAN http / any future https) — by design,
    this embeds whatever host the USER'S browser reached the server through
    (LAN IP, Tailscale hostname, ...), matching how Phase 3 LAN access
    works. The Host-header validation middleware (see `_validate_host`
    below) has already rejected unrecognized hosts by the time this runs,
    so no additional validation is needed here."""
    host = request.headers.get("host", "")
    return f"{request.url.scheme}://{host}/login/qr?token={token}"


_DIR_SIZE_CACHE_TTL = 30  # seconds
_dir_size_cache: dict[Path, tuple[float, int]] = {}


def _cached_dir_bytes(path: Path) -> int:
    """`dir_bytes` behind a 30s TTL cache — avoids rglob-walking the chroma/audio
    dirs on every /healthz call. Not a monotonicity assumption (audio shrinks
    on delete): the cache is just a 30s staleness bound, which is fine for a
    health-check byte count."""
    now = time.monotonic()
    cached = _dir_size_cache.get(path)
    if cached is not None and now - cached[0] < _DIR_SIZE_CACHE_TTL:
        return cached[1]
    size = dir_bytes(path)
    _dir_size_cache[path] = (now, size)
    return size


def _detect_lan_ips() -> list[str]:
    """Detect all candidate LAN IPs for this machine.

    Shared by `create_app` (config-file `host: "0.0.0.0"` or any non-loopback
    host) and `cmd_run --lan` in tiro/cli.py — single implementation so both
    paths populate the Host-validation allowlist identically (finding I-2).
    A single detected IP breaks on offline or multi-homed machines (e.g. both
    Wi-Fi and Ethernet active, or no internet route at all), so this gathers
    via two independent methods and returns the union.
    """
    import socket

    candidate_ips: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        candidate_ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if "." in addr and not addr.startswith("127."):
                candidate_ips.add(addr)
    except Exception:
        pass
    return sorted(candidate_ips)


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
    now_utc = datetime.now(UTC)

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

    target_utc = target_local.astimezone(UTC)
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

        today = datetime.now(UTC).strftime("%Y-%m-%d")
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
    config.wiki_dir.mkdir(parents=True, exist_ok=True)

    # Initialize SQLite + run migrations
    init_db(config.db_path)
    migrate_db(config.db_path)

    # Reconcile the wiki derived index (Phase 1b) from what's on disk (files
    # win). Heals metadata drift that doctor's slug-presence-only check
    # can't see -- e.g. hand-edited frontmatter fields, or files dropped in
    # out-of-band -- without waiting for a manual `tiro doctor --fix`.
    # Cheap at wiki-library scale (one rglob + per-file frontmatter parse);
    # best-effort like every other startup step here, since a wiki index
    # hiccup must never block the server from coming up.
    try:
        from tiro.wiki import reconcile_wiki_index

        reconcile_wiki_index(config)
    except Exception as e:
        logger.error("Startup wiki reconcile failed (non-fatal): %s", e)

    # Reconcile the highlights/notes derived index (Phase 2 M2.1) from what's
    # on disk (files win) -- same rationale and error-isolation posture as
    # the wiki reconcile above: heals sidecar drift on startup without
    # waiting for a manual `tiro doctor --fix`, and must never block the
    # server from coming up.
    try:
        from tiro.annotations import reconcile_annotations

        reconcile_annotations(config)
    except Exception as e:
        logger.error("Startup annotations reconcile failed (non-fatal): %s", e)

    # Initialize ChromaDB with configured embedding model
    init_vectorstore(config.chroma_dir, config.default_embedding_model)

    # Recalculate content decay weights
    recalculate_decay(config)

    # Named background-task registry. start/stop mirror each task to
    # app.state.{name}_task so healthz, `tiro status`, and existing tests
    # keep reading the attributes they read today (see tiro/scheduler.py).
    scheduler = Scheduler(app.state)
    app.state.scheduler = scheduler
    app.state.imap_task = None
    app.state.digest_task = None
    app.state.vector_retry_task = None

    # Start IMAP sync background task if configured
    if config.imap_enabled and config.imap_sync_interval > 0:
        scheduler.start("imap", _imap_sync_loop(config))
        logger.info("IMAP sync started: every %d minutes", config.imap_sync_interval)

    # Start digest schedule background task if configured
    if config.digest_schedule_enabled:
        scheduler.start("digest", _digest_schedule_loop(config))
        logger.info("Digest schedule started: daily at %s", config.digest_schedule_time)

    # Start vector retry background task if configured
    if config.vector_retry_interval > 0:
        scheduler.start("vector_retry", _vector_retry_loop(config))
        logger.info("Vector retry started: every %d minutes", config.vector_retry_interval)

    # mDNS/Bonjour advertisement (Phase 3 M3.0): opt-in, and only meaningful
    # when the server is actually reachable from other devices on the LAN
    # -- a loopback-only bind gets nothing out of advertising itself, so
    # skip with a debug log rather than starting zeroconf for no reason.
    # Not an asyncio loop like the Scheduler's tasks above, so it's not
    # registered there; a direct call wrapped in try/except is the simplest
    # correct shape (mirrors the wiki/annotations reconcile calls above --
    # best-effort, must never block startup).
    #
    # MUST run via asyncio.to_thread, not a direct call: register_mdns()
    # constructs a real Zeroconf() and calls its blocking register_service(),
    # which internally does a thread-safe round-trip back onto whatever
    # asyncio loop Zeroconf attached itself to. Called directly from this
    # coroutine, that loop IS this lifespan's loop, on this same thread --
    # register_service() would block waiting for a callback that can only
    # run once THIS coroutine yields control, which it can't while blocked.
    # That's a self-deadlock (zeroconf's own EventLoopBlocked guard fires
    # after ~10s, and with the collision retry in mdns.py that's ~21s of a
    # server that isn't serving anything, after which registration still
    # fails). `tiro/mdns.py` additionally forces `use_asyncio=False` on its
    # Zeroconf() construction as a second, independent layer of defense --
    # see its docstring -- but running off the loop thread via to_thread is
    # required regardless, since a worker thread has no running loop for
    # zeroconf to autodetect and attach to in the first place.
    if config.mdns_enabled and app.state.lan_mode:
        try:
            from tiro.mdns import get_registered_hostname, register_mdns

            registered = await asyncio.to_thread(register_mdns, config, config.host, config.port)
            # get_registered_hostname() is a plain in-memory read (no
            # zeroconf I/O), so no to_thread needed here -- only
            # register_mdns/unregister_mdns touch zeroconf's blocking calls.
            # Stashing the ACTUAL registered name (which may carry the `-2`
            # collision suffix) on app.state, rather than assuming
            # config.mdns_hostname won, is what the Host-header allowlist
            # (`_validate_host` above) reads (finding 1, M3.0 final review).
            if registered:
                app.state.mdns_name = get_registered_hostname()
        except Exception as e:
            logger.warning("mDNS registration failed (non-fatal): %s", e)
    elif config.mdns_enabled:
        logger.debug("mDNS enabled but bind host %r is loopback; skipping advertisement", config.host)

    logger.info("Tiro is ready — library at %s", config.library)
    yield

    await scheduler.shutdown()

    # Unconditional and best-effort: unregister_mdns() is a no-op when
    # registration was never attempted or failed, so no need to track
    # whether the register call above actually succeeded. Runs via
    # asyncio.to_thread for the same self-deadlock reason as register_mdns
    # above -- unregister_service() has the same blocking round-trip shape.
    try:
        from tiro.mdns import unregister_mdns

        await asyncio.to_thread(unregister_mdns)
    except Exception as e:
        logger.warning("mDNS unregister failed (non-fatal): %s", e)
    app.state.mdns_name = None


def create_app(config: TiroConfig | None = None, tls_enabled: bool = False) -> FastAPI:
    """Create and configure the FastAPI application.

    `tls_enabled` (M3.0 Task 4): uvicorn — not create_app — owns the actual
    TLS handshake (it's given ssl_certfile/ssl_keyfile directly), so
    create_app has no way to observe whether TLS is active on its own. Both
    entry points that know (tiro/cli.py's cmd_run and run.py's main(), which
    decide this from --cert/--key) pass it in explicitly. Defaults to False
    so every existing caller (tests, MCP-adjacent code, anything constructing
    create_app() bare) keeps behaving as plain HTTP.
    """
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
    app.state.tls_enabled = tls_enabled

    # LAN mode: populate app.state.lan_ips whenever the EFFECTIVE bind host
    # is non-loopback — a config-file `host: "0.0.0.0"` (no --lan flag) is
    # just as exposed as `tiro run --lan` and must be accepted by the Host
    # allowlist the same way (finding I-2). cmd_run sets config.host to the
    # effective host before calling create_app, so both paths converge here.
    # Detection does a couple of socket calls, so skip it entirely for the
    # (overwhelmingly common) loopback case — tests create dozens of apps.
    app.state.lan_mode = config.host not in ("127.0.0.1", "localhost")
    app.state.lan_ips = set(_detect_lan_ips()) if app.state.lan_mode else set()
    app.state.lan_ip = sorted(app.state.lan_ips)[0] if app.state.lan_ips else None

    # mDNS-advertised hostname (finding 1, M3.0 final review): None until the
    # lifespan actually registers with zeroconf and learns which candidate
    # name won (the configured name, or its `-2` collision-retry sibling).
    # Set here (not left unset) so the `_validate_host` closure below can
    # read it unconditionally via getattr, same defensive pattern as
    # `lan_ips`. Registration happens in `lifespan`, AFTER this closure is
    # built, so the closure must read `app.state.mdns_name` fresh on every
    # request rather than capturing a value now -- mirrors how `lan_ips` is
    # re-read via `getattr(app.state, ...)` below, not closed over.
    app.state.mdns_name = None

    # insecure_lan_http (M3.0 Task 4): drives base.html's dismissable warning
    # banner via `_theme_context`. True only when the effective bind host is
    # non-loopback AND no TLS is active — LAN-over-HTTPS (--cert/--key) and
    # plain loopback HTTP are both fine and get no banner.
    app.state.insecure_lan_http = app.state.lan_mode and not tls_enabled

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
    # hosts outright. "testserver" (Starlette's TestClient default) is
    # deliberately NOT allowed here — a production allowlist must not carry
    # a test-only exemption (finding M-2); tests instead construct
    # TestClient(app, base_url="http://localhost") so Host: localhost is
    # sent honestly.
    allowed_hosts = {
        f"localhost:{config.port}", f"127.0.0.1:{config.port}",
        "localhost", "127.0.0.1",
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
        # app-creation time (`_detect_lan_ips`, above), not just the first
        # one found.
        lan_ips = getattr(app.state, "lan_ips", None) or set()
        # mDNS-advertised hostname (finding 1, M3.0 final review): read
        # dynamically, same as lan_ips above -- registration happens in the
        # lifespan AFTER this closure is built, so a value captured now
        # would always be None. Exact-match only against the name zeroconf
        # ACTUALLY registered (which may carry the `-2` collision suffix,
        # never the raw `config.mdns_hostname`) -- this must not become a
        # wildcard "any *.local passes" rule, or it would gut the DNS-
        # rebinding defense this middleware exists for (an attacker-chosen
        # Host is never the name this server itself registered on the LAN).
        # Host headers are case-insensitive (RFC 9110 §4.2.3); mDNS names
        # arrive lowercase in practice but normalize both sides to be safe.
        mdns_name = getattr(app.state, "mdns_name", None)
        mdns_match = False
        if mdns_name:
            expected = f"{mdns_name.lower()}.local"
            host_lower = host.lower()
            mdns_match = host_lower == expected or host_lower == f"{expected}:{config.port}"
        if (
            host not in allowed_hosts
            and not any(host == f"{ip}:{config.port}" for ip in lan_ips)
            and not mdns_match
        ):
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": f"Unrecognized Host header: {host!r}"},
            )
        return await call_next(request)

    # API routers
    from tiro.api.routes_annotations import router as annotations_router
    from tiro.api.routes_articles import router as articles_router
    from tiro.api.routes_audio import router as audio_router
    from tiro.api.routes_auth import router as auth_router
    from tiro.api.routes_authors import router as authors_router
    from tiro.api.routes_backup import router as backup_router
    from tiro.api.routes_classify import router as classify_router
    from tiro.api.routes_decay import router as decay_router
    from tiro.api.routes_digest import router as digest_router
    from tiro.api.routes_digest_email import router as digest_email_router
    from tiro.api.routes_export import router as export_router
    from tiro.api.routes_filters import router as filters_router
    from tiro.api.routes_graph import router as graph_router
    from tiro.api.routes_ingest import router as ingest_router
    from tiro.api.routes_search import router as search_router
    from tiro.api.routes_sessions import router as sessions_router
    from tiro.api.routes_settings import router as settings_router
    from tiro.api.routes_sources import router as sources_router
    from tiro.api.routes_stats import router as stats_router
    from tiro.api.routes_tokens import router as tokens_router
    from tiro.api.routes_views import router as views_router
    from tiro.api.routes_wiki import router as wiki_router

    app.include_router(auth_router)
    protected = [
        ingest_router, articles_router, sources_router, digest_router,
        digest_email_router, search_router, classify_router, decay_router,
        stats_router, export_router, settings_router, audio_router,
        graph_router, filters_router, tokens_router, backup_router,
        authors_router, views_router, wiki_router, annotations_router,
        sessions_router,
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
    templates.env.globals["static_v"] = STATIC_VERSION

    @app.get("/manifest.webmanifest")
    async def manifest():
        """PWA manifest (M3.1 Task 1): deliberately UNAUTHENTICATED, same
        ceremony as /login/qr below. A browser/OS evaluates installability
        (and a phone's "Add to Home Screen" prompt fetches this) before the
        user necessarily has a session -- an install prompt that 401s can't
        install anything. Unlike /login/qr there's no secret or user data
        here at all: this is a 100% static file (name/icons/colors only), so
        there's no confidentiality question, just the allowlist ceremony
        needed to keep the route-walk invariant (test_route_walk_everything_
        gated) honest about which routes are intentionally open. Served via
        an explicit route (not folded into the already-open /static mount)
        so the content-type is exactly `application/manifest+json`, the path
        matches what base.html's <link rel="manifest"> and the binding spec
        both expect, and this docstring has a place to live.
        """
        return FileResponse(
            FRONTEND_DIR / "static" / "manifest.webmanifest",
            media_type="application/manifest+json",
        )

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
        return templates.TemplateResponse(request, "login.html", _theme_context(request))

    @app.get("/login/qr")
    async def login_via_qr(request: Request, token: str = ""):
        """Redeem a one-time QR-login token minted by /setup/qr. Deliberately
        NOT gated behind require_page_auth (this IS how an unauthenticated
        phone gets its first session) and deliberately does NOT call
        auth._check_csrf — like /login above, this is a top-level browser
        navigation (the phone's camera/QR app opening a link), which
        legitimately carries Sec-Fetch-Site: cross-site; CSRF checks exist to
        stop *mutating* cross-site requests riding an existing session, not
        to stop a bare link click that presents its own bearer of proof (the
        token) instead of ambient cookie auth.

        Every failure path (missing token, garbage token, expired, already
        used) redirects to the same generic /login with no distinguishing
        detail — consume_login_token's docstring covers why (no oracle)."""
        from fastapi.responses import RedirectResponse

        config = request.app.state.config
        if not auth.consume_login_token(config, token):
            response = RedirectResponse(url="/login", status_code=302)
            response.headers["Cache-Control"] = "no-store"
            return response
        session_token = auth.create_session(config.db_path)
        response = RedirectResponse(url="/inbox", status_code=302)
        auth.attach_session_cookie(response, request, session_token)
        response.headers["Cache-Control"] = "no-store"
        return response

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
        return templates.TemplateResponse(request, "inbox.html", _theme_context(request))

    @app.get("/digest", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def digest_page(request: Request):
        return templates.TemplateResponse(request, "digest.html", _theme_context(request))

    @app.get("/articles/{article_id}", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def reader(request: Request, article_id: int):
        return templates.TemplateResponse(
            request,
            "reader.html",
            {
                "article_id": article_id,
                "reading_telemetry_enabled": request.app.state.config.reading_telemetry_enabled,
                **_theme_context(request),
            },
        )

    @app.get("/stats", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def stats_page(request: Request):
        return templates.TemplateResponse(request, "stats.html", _theme_context(request))

    @app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def settings_page(request: Request):
        return templates.TemplateResponse(request, "settings.html", _theme_context(request))

    @app.get("/graph", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def graph_page(request: Request):
        return templates.TemplateResponse(request, "graph.html", _theme_context(request))

    @app.get("/sources", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def sources_page(request: Request):
        return templates.TemplateResponse(request, "sources.html", _theme_context(request))

    async def _qr_setup_response(request: Request) -> HTMLResponse:
        config = request.app.state.config
        token = auth.create_login_token(config)
        qr_url = _login_qr_target(request, token)
        response = templates.TemplateResponse(
            request,
            "qr_setup.html",
            {
                "qr_svg": _qr_svg(qr_url),
                "qr_url": qr_url,
                "qr_ttl_minutes": auth.LOGIN_TOKEN_TTL_MINUTES,
                **_theme_context(request),
            },
        )
        # A fresh single-use login token is embedded in this page's markup on
        # every render (GET or POST) — a cached copy served back to the
        # browser (bfcache, a shared proxy, browser history) would leak a
        # token whose QR image a bystander could still scan. no-store is
        # unconditional, not just no-cache, for exactly that reason.
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/setup/qr", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def qr_setup_page(request: Request):
        """Every render mints a fresh login token (simplest correct option —
        old ones just expire naturally via their own 15-minute TTL, no
        cleanup needed here)."""
        return await _qr_setup_response(request)

    @app.post("/setup/qr", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def qr_setup_regenerate(request: Request):
        """'Generate new code' button: a same-origin form POST (CSRF-checked
        like any other authenticated mutation) that re-renders the whole
        page with a fresh token — a full reload is the simplest correct
        shape here (no separate JSON+SVG-fragment endpoint to keep in
        sync)."""
        auth._check_csrf(request)
        return await _qr_setup_response(request)

    @app.get("/wiki", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def wiki_list_page(request: Request):
        return templates.TemplateResponse(request, "wiki.html", _theme_context(request))

    @app.get("/wiki/{slug:path}", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def wiki_page_view(request: Request, slug: str):
        return templates.TemplateResponse(
            request, "wiki_page.html", {"wiki_slug": slug, **_theme_context(request)}
        )

    @app.get("/highlights", response_class=HTMLResponse, dependencies=[Depends(auth.require_page_auth)])
    async def highlights_page(request: Request):
        return templates.TemplateResponse(request, "highlights.html", _theme_context(request))

    return app
