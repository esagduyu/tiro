// Tiro service worker — pure routing core (M3.1 Task 2).
//
// Decides WHAT strategy a given fetch should use, without touching the
// Cache API, `fetch()`, or any service-worker-only global. That keeps this
// module plain-value-in, plain-value-out so `node --test` can cover the
// adversarial routing table directly (see
// js/tests/sw-routing.test.mjs) with no ServiceWorkerGlobalScope shim —
// same "pure functions get node coverage" posture as core.js's esc/num/
// formatDate/timeAgo.
//
// Imported into sw.js via an ABSOLUTE specifier
// (`/static/js/sw-routing.js?v=...`), not a relative one. sw.js itself is
// served from a synthetic route at `/sw.js` (not physically under
// `/static/`), so a relative `./sw-routing.js` import would resolve against
// `/sw-routing.js` at the document root and 404. The absolute path sidesteps
// that entirely and is the single copy of this file either way (no
// duplication, node and the browser both load the exact same bytes from
// `tiro/frontend/static/js/sw-routing.js`).

/**
 * @param {string} url - request URL (absolute or relative to same-origin)
 * @param {string} method - HTTP method, e.g. "GET"
 * @param {string} mode - Request.mode, e.g. "navigate", "cors", "same-origin"
 * @returns {"static-cache-first"|"article-network-first"|"navigate-offline-fallback"|"network-only"}
 */
export function swRouteFor(url, method, mode) {
    // NEVER cache mutations. This single early return is what makes "never
    // cache POST/PATCH/DELETE/PUT" structurally true rather than an
    // easy-to-forget convention: every other branch below only runs for GET.
    if (method !== "GET") return "network-only";

    const pathname = new URL(url, "https://tiro.invalid/").pathname;

    // The service worker's own script is always fetched fresh by the
    // browser's own SW-update algorithm; nothing here should intercept it.
    if (pathname === "/sw.js") return "network-only";

    // Static assets are version-stamped via `?v=` query params (see
    // STATIC_VERSION in tiro/app.py) — a cache-first strategy is safe
    // because a changed file always arrives at a changed URL.
    if (pathname.startsWith("/static/")) return "static-cache-first";

    // Exact `/api/articles/{id}` only — NOT the list endpoint
    // (`/api/articles`) and NOT any nested sub-resource
    // (`/api/articles/{id}/annotations`, `/related`, `/audio`, ...), which
    // carry per-request query state or aren't meant for offline reading.
    if (/^\/api\/articles\/\d+$/.test(pathname)) return "article-network-first";

    // Everything that must NEVER fall back to the offline page, even as a
    // top-level navigation: auth/setup flows (a stale cached /login or a
    // surprise redirect to /offline mid-setup would be actively wrong) and
    // every other /api/* endpoint (mutations, search, digest, etc. — none
    // of these are cacheable page navigations either way, but a navigation
    // -mode fetch to a JSON endpoint should still never happen here in
    // practice; this is belt-and-suspenders).
    const NEVER_FALLBACK_PREFIXES = ["/login", "/logout", "/setup", "/api/"];
    if (NEVER_FALLBACK_PREFIXES.some((p) => pathname.startsWith(p))) {
        return "network-only";
    }

    // A real page navigation (address bar, link click, reload) to anything
    // else in the app (/inbox, /articles/5, /digest, ...) — try the
    // network, fall back to the precached /offline page if it fails.
    if (mode === "navigate") return "navigate-offline-fallback";

    return "network-only";
}
