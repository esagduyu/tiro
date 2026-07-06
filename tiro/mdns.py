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
    global _zeroconf, _service_info

    if _zeroconf is not None:
        logger.debug("mDNS already registered; skipping duplicate registration")
        return True

    try:
        zc = Zeroconf()
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
    global _zeroconf, _service_info

    if _zeroconf is None:
        return
    zc, info = _zeroconf, _service_info
    _zeroconf, _service_info = None, None
    try:
        if info is not None:
            zc.unregister_service(info)
    except Exception as e:
        logger.warning("mDNS unregister_service failed: %s", e)
    try:
        zc.close()
    except Exception as e:
        logger.warning("mDNS close failed: %s", e)
