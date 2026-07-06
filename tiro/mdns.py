"""mDNS/Bonjour advertisement for Tiro (Phase 3 M3.0, opt-in).

Advertises Tiro on the local network as `{mdns_hostname}.local` via
zeroconf's `_http._tcp.local.` service type, so phones on the same Wi-Fi
can find the server by name instead of typing a raw LAN IP.

This module never decides WHETHER to advertise -- that's the caller's job
(tiro/app.py's lifespan checks `config.mdns_enabled` and the effective bind
host before calling `register_mdns`). Every zeroconf operation in here is
wrapped: a failure logs a warning and returns/no-ops rather than raising,
since a misbehaving multicast socket (sandboxed network namespace, VPN
weirdness, no network at all) must never block server startup or shutdown.

Registration state is held at module scope (a single Tiro process only
ever runs one server), mirroring the module-level dir-size cache pattern
used elsewhere in this codebase rather than threading a class instance
through app.state -- zeroconf is not an asyncio loop like the Scheduler's
background tasks, so it doesn't fit that abstraction.
"""

import logging
import socket

from zeroconf import ServiceInfo, Zeroconf

from tiro.app import _detect_lan_ips

logger = logging.getLogger(__name__)

_SERVICE_TYPE = "_http._tcp.local."

_zeroconf: Zeroconf | None = None
_service_info: ServiceInfo | None = None
# The hostname actually accepted by zeroconf (may carry the `-2` collision
# suffix from register_mdns()'s retry below) -- distinct from
# `config.mdns_hostname`, which is only the FIRST candidate tried. Callers
# that need to know what `.local` name the server is actually reachable at
# (tiro/app.py's Host-header allowlist, QR code generation) must read this
# via `get_registered_hostname()` rather than assuming the configured name
# won.
_registered_hostname: str | None = None


def _resolve_addresses(host: str) -> list[bytes]:
    """Candidate LAN IPs to advertise, as packed addresses for ServiceInfo.

    Primarily `_detect_lan_ips()` (shared with the Host-validation
    allowlist in tiro/app.py); the effective bind `host` is folded in too
    when it's already a concrete address (not a wildcard/loopback), which
    is harmless when it duplicates a detected IP and helpful on the rare
    multi-homed machine where the two disagree.
    """
    ips = set(_detect_lan_ips())
    if host not in ("0.0.0.0", "127.0.0.1", "localhost", ""):
        ips.add(host)
    return [socket.inet_aton(ip) for ip in sorted(ips)]


def _build_service_info(hostname: str, host: str, port: int) -> ServiceInfo:
    return ServiceInfo(
        _SERVICE_TYPE,
        f"{hostname}.{_SERVICE_TYPE}",
        addresses=_resolve_addresses(host),
        port=port,
        server=f"{hostname}.local.",
    )


def register_mdns(config, host: str, port: int) -> bool:
    """Advertise Tiro via mDNS. Returns True on success, False otherwise.

    Idempotent: a second call while already registered is a no-op that
    returns True without touching zeroconf again. Collision-tolerant: if
    `{mdns_hostname}._http._tcp.local.` is already taken on the LAN (a
    zeroconf `NonUniqueNameException` or similar), retries once with a
    `-2` numeric suffix, then gives up quietly.
    """
    global _zeroconf, _service_info, _registered_hostname

    if _zeroconf is not None:
        logger.debug("mDNS already registered; skipping duplicate registration")
        return True

    try:
        # use_asyncio=False is load-bearing, not cosmetic. zeroconf 0.150.0's
        # Zeroconf.__init__ autodetects a running asyncio loop via
        # get_running_loop() and, if found, attaches itself to it (see
        # zeroconf/_core.py: `self.loop = None if use_asyncio is False else
        # get_running_loop()`). register_mdns() is called from inside
        # tiro/app.py's async lifespan, so without this flag Zeroconf would
        # attach to the SAME event loop this coroutine is running on; then
        # register_service()'s blocking `run_coroutine_threadsafe(...).result()`
        # would deadlock waiting on that loop's own thread, which can't
        # service the coroutine while it's blocked waiting -- zeroconf's own
        # 10s EventLoopBlocked guard fires (~21s wall time across the
        # collision retry below) and registration ALWAYS fails. Forcing
        # `use_asyncio=False` makes Zeroconf run its own private event loop
        # on its own background thread instead, so `register_service()`'s
        # blocking wait resolves normally. Callers additionally run
        # `register_mdns`/`unregister_mdns` via `asyncio.to_thread(...)`
        # (tiro/app.py) so this function never executes on the FastAPI
        # event-loop thread in the first place -- belt and suspenders.
        # DO NOT remove use_asyncio=False on the theory that "we're already
        # off the loop thread via to_thread" -- to_thread workers can still
        # see a running loop via contextvars in some asyncio internals, and
        # this constructor argument is the one zeroconf actually documents
        # and tests against. Reproduced-and-verified by a Phase 3 M3.0
        # reviewer against a real zeroconf install: without this flag,
        # startup froze for ~21s and registration never succeeded; with it,
        # register_service() completed in ~1.6s.
        zc = Zeroconf(use_asyncio=False)
    except Exception as e:
        logger.warning("mDNS unavailable, skipping registration: %s", e)
        return False

    hostname = config.mdns_hostname
    for candidate in (hostname, f"{hostname}-2"):
        try:
            info = _build_service_info(candidate, host, port)
            zc.register_service(info)
        except Exception as e:
            logger.warning("mDNS registration failed for candidate %r: %s", candidate, e)
            continue
        _zeroconf = zc
        _service_info = info
        _registered_hostname = candidate
        logger.info("mDNS advertised as %s on port %d", info.server, port)
        return True

    logger.warning("mDNS registration gave up after retry for hostname %r", hostname)
    try:
        zc.close()
    except Exception as e:
        logger.warning("mDNS cleanup after failed registration errored: %s", e)
    return False


def unregister_mdns() -> None:
    """Unregister and close the mDNS advertisement, if any.

    Safe to call unconditionally (e.g. every server shutdown) even when
    registration was never attempted or failed -- a no-op in that case.
    """
    global _zeroconf, _service_info, _registered_hostname

    if _zeroconf is None:
        return
    zc, info = _zeroconf, _service_info
    _zeroconf, _service_info, _registered_hostname = None, None, None
    try:
        if info is not None:
            zc.unregister_service(info)
    except Exception as e:
        logger.warning("mDNS unregister_service failed: %s", e)
    try:
        zc.close()
    except Exception as e:
        logger.warning("mDNS close failed: %s", e)


def get_registered_hostname() -> str | None:
    """The hostname actually registered with zeroconf, or None.

    None both before registration is attempted and after it fails/is
    unregistered. May differ from `config.mdns_hostname` when the `-2`
    collision-retry candidate is the one that succeeded (see
    `register_mdns`). A plain in-memory read of module state -- no zeroconf
    I/O -- so callers do not need `asyncio.to_thread` to call this safely.
    """
    return _registered_hostname
