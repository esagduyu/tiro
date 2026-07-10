"""Notify-only update check against GitHub Releases (spec D5).

The ONLY phone-home in Tiro: a version-string-free GET to
``https://api.github.com/repos/esagduyu/tiro/releases/latest`` at most once
every 24 h, gated by ``config.update_check_enabled`` (default True). Nothing
about the library is sent — no query params, no body, just the fixed headers
below. State is in-memory only (``app.state.update_state``); a restart re-checks
within its first cycle. Every check writes exactly one audit line
(``service="update-check"``, per the M6 every-external-call convention); audit
never raises into the check, and a network failure leaves the held state
unchanged and never raises into the scheduler loop.

The scheduler registers this as a run-first ``PeriodicTask`` (see
``tiro/app.py``) so a fresh start learns about a release within seconds and a
long-running install within a day. ``fetch_latest`` is the network worker and is
patched to a no-op by the test-suite autouse fixture so tests stay offline; the
dedicated tests exercise the real function directly with an injected transport.
"""

import logging
import time
from datetime import UTC, datetime

import httpx

from tiro import __version__
from tiro.audit import log_api_call
from tiro.config import TiroConfig

logger = logging.getLogger(__name__)

UPDATE_CHECK_INTERVAL_SECONDS = 86_400  # 24 h — fixed cadence (spec D5)
RELEASES_URL = "https://api.github.com/repos/esagduyu/tiro/releases/latest"
_TIMEOUT_SECONDS = 10.0


def _parse_version(tag: str) -> tuple[int, ...] | None:
    """Parse a dotted-int version (optionally ``v``-prefixed) into a tuple.

    Returns ``None`` for any malformed tag — never raises. ``/releases/latest``
    yields clean tags like ``v0.7.0`` (drafts/prereleases excluded by API
    contract), but a hostile or garbage ``tag_name`` must degrade to
    not-newer, not crash the comparison.
    """
    if not tag or not isinstance(tag, str):
        return None
    core = tag.strip()
    if core[:1].lower() == "v":
        core = core[1:]
    if not core:
        return None
    try:
        return tuple(int(p) for p in core.split("."))
    except ValueError:
        return None


def is_newer(tag: str, current: str) -> bool:
    """True iff ``tag`` names a strictly-newer release than ``current``.

    Pure and total: a malformed tag (or current version) compares as
    not-newer rather than raising.
    """
    latest = _parse_version(tag)
    cur = _parse_version(current)
    if latest is None or cur is None:
        return False
    return latest > cur


def update_context(state: dict | None, current: str | None = None) -> dict:
    """Banner context for ``_theme_context``/``base.html`` (spec D5 surfacing).

    Injects a positive result ONLY when a strictly-newer release than
    ``current`` is held in ``state`` — so ``base.html`` renders zero banner DOM
    otherwise (the LAN-banner pattern). ``update_version`` is the ``v``-stripped
    display string; the per-version dismissal key in ``sidebar.js`` keys off it.
    """
    current = current or __version__
    state = state or {}
    latest = state.get("latest_version")
    if isinstance(latest, str) and is_newer(latest, current):
        display = latest[1:] if latest[:1].lower() == "v" else latest
        return {
            "update_available": True,
            "update_version": display,
            "update_url": state.get("html_url"),
        }
    return {"update_available": False, "update_version": None, "update_url": None}


def fetch_latest(
    config: TiroConfig, state: dict | None, *, client: httpx.Client | None = None
) -> dict:
    """Perform one update check and return the (possibly-updated) state dict.

    State shape: ``{etag, latest_version, html_url, checked_at}``. On a ``200``
    the etag/version/url/timestamp are refreshed; a ``304`` (sent via
    ``If-None-Match`` from the held etag — costs no rate-limit quota) reuses the
    held result and only bumps ``checked_at``; ANY other status or a network
    error leaves the held result untouched (etag/version/url/checked_at all
    preserved) and never raises. Exactly one audit line is written per call.

    ``client`` is an injection seam for tests (an ``httpx.Client`` wrapping a
    ``MockTransport``); production passes nothing and a 10 s-timeout client is
    built and closed here.
    """
    new_state = dict(state or {})
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"Tiro/{__version__}",
    }
    if new_state.get("etag"):
        headers["If-None-Match"] = new_state["etag"]

    owns_client = client is None
    started = time.monotonic()
    success = True
    error: str | None = None
    try:
        if owns_client:
            client = httpx.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=True)
        try:
            resp = client.get(RELEASES_URL, headers=headers)
        finally:
            if owns_client:
                client.close()

        if resp.status_code == 304:
            new_state["checked_at"] = datetime.now(UTC).isoformat()
        elif resp.status_code == 200:
            data = resp.json()
            etag = resp.headers.get("ETag")
            if etag:
                new_state["etag"] = etag
            new_state["latest_version"] = (data.get("tag_name") or "").strip() or None
            new_state["html_url"] = data.get("html_url")
            new_state["checked_at"] = datetime.now(UTC).isoformat()
        else:
            success = False
            error = f"HTTP {resp.status_code}"
    except Exception as e:  # network error, malformed JSON, etc. — never propagate
        success = False
        error = f"{type(e).__name__}: {e}"

    duration_ms = int((time.monotonic() - started) * 1000)
    log_api_call(
        config,
        "update-check",
        endpoint=RELEASES_URL,
        duration_ms=duration_ms,
        success=success,
        error=error,
    )
    if not success:
        logger.debug("Update check failed (non-fatal): %s", error)
    return new_state
