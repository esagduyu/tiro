// Tiro service worker (M3.1 Task 2).
//
// VERSION INJECTION: this file lives on disk carrying a placeholder token
// everywhere the real cache-bust constant belongs (see the `VERSION`
// declaration a few lines down for its exact spelling), so it's still
// valid, lintable, diffable JS at rest — no partial Jinja syntax living
// inside a .js file. NOTE: that exact token must not be repeated verbatim
// anywhere else in THIS file's prose comments, because the substitution
// below is a dumb whole-file string replace with no awareness of code vs.
// comments — writing it again here would mangle this very sentence too.
// It is served at `GET /sw.js` through a tiny route in tiro/app.py that
// reads this exact file and does one `.replace(...)` swapping that token
// for the real STATIC_VERSION constant before responding — the same
// single-source cache-bust constant every other static asset's `?v=` query
// param already reads. Registering `navigator.serviceWorker.register(
// "/sw.js")` fetches THAT substituted response, never this raw file
// directly (the raw file is also harmlessly reachable at /static/sw.js
// through the pre-existing open /static mount, same as
// manifest.webmanifest in Task 1 — with the literal unsubstituted
// placeholder still in the cache names there, which is fine: nothing ever
// registers a service worker from that URL).
//
// `type: "module"` registration (see sidebar.js/login.html): evergreen
// browser support for module service workers is broad enough now that this
// is the simplest way to `import` the pure routing core below without an
// `importScripts()` build step. The import specifier is an ABSOLUTE path
// (`/static/js/sw-routing.js`), not a relative one — see that module's own
// header comment for why relative resolution would break here (this
// script's own URL is `/sw.js`, not anything under `/static/`).
import { swRouteFor } from "/static/js/sw-routing.js?v=__STATIC_VERSION__";

const VERSION = "__STATIC_VERSION__"; // substituted by the /sw.js route in tiro/app.py
const STATIC_CACHE = `tiro-${VERSION}-static`;
const ARTICLES_CACHE = `tiro-${VERSION}-articles`;

// Kept deliberately tight (per the binding spec): only what's needed to
// render /offline itself with zero network. offline.html is a standalone
// page (no base.html, no theme stylesheet, no d3/Chart.js) precisely so
// this list stays this short — see offline.html's own header comment.
const PRECACHE_URLS = [
    "/offline",
    "/static/js/core.js?v=__STATIC_VERSION__",
    "/static/js/icons.js?v=__STATIC_VERSION__",
    "/static/vendor/marked.min.js?v=__STATIC_VERSION__",
    "/static/vendor/purify.min.js?v=__STATIC_VERSION__",
];

// LRU cap for the articles cache. Cache API does not natively expose an
// access/insertion order guarantee we can rely on per spec, but every
// shipping engine (Chromium, Firefox, WebKit) enumerates `Cache.keys()` in
// INSERTION order in practice, and `Cache.put()` on an already-present
// request key re-inserts it (delete-then-add), which in every one of those
// implementations moves it to the tail. That combination gives a simple,
// honestly-documented approximation of LRU: the head of `keys()` is always
// the least-recently-(re)written entry, so trimming from the head after
// every successful put approximates evicting the least-recently-used
// article. This is NOT true access-order LRU (a cache HIT with no
// re-fetch does not bump an entry's position) and is NOT guaranteed by the
// Cache API spec — documented here and in the task report rather than
// silently assumed.
const ARTICLE_CACHE_LIMIT = 50;

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(STATIC_CACHE).then((cache) => cache.addAll(PRECACHE_URLS))
    );
    // Deliberately NOT calling self.skipWaiting(): this is the riskiest
    // task in the milestone (a bad service worker can brick the app until
    // caches are cleared), so a new SW version waits for all existing tabs
    // to close before activating, per the platform's normal, conservative
    // update lifecycle — no forced takeover of an already-open tab onto
    // code that hasn't proven itself yet. This also matches "no
    // update-nagging UI" from the binding spec: no UI ever prompts the user
    // to reload for an update, so there is no user expectation of
    // immediate takeover to break.
});

self.addEventListener("activate", (event) => {
    // THE upgrade path: a stale service worker (from a prior
    // STATIC_VERSION) must self-clean its own caches on next activation,
    // rather than accumulate tiro-<old-version>-* caches forever or -- far
    // worse -- keep serving old-version static assets cache-first under a
    // new deploy.
    event.waitUntil(
        caches.keys().then((names) =>
            Promise.all(
                names
                    .filter((name) => name.startsWith("tiro-") && name !== STATIC_CACHE && name !== ARTICLES_CACHE)
                    .map((name) => caches.delete(name))
            )
        )
    );
});

async function cacheFirst(request) {
    const cache = await caches.open(STATIC_CACHE);
    const cached = await cache.match(request);
    if (cached) return cached;
    const response = await fetch(request);
    // Only cache genuinely successful responses -- an opaque cross-origin
    // response (status 0) or a 4xx/5xx must never be cached and replayed
    // as if it were good static content.
    if (response.ok) cache.put(request, response.clone());
    return response;
}

async function trimArticleCache(cache) {
    const keys = await cache.keys();
    const excess = keys.length - ARTICLE_CACHE_LIMIT;
    if (excess <= 0) return;
    // See ARTICLE_CACHE_LIMIT's comment above: keys() enumeration order is
    // the LRU approximation. Trim from the head (oldest / least-recently
    // (re)written).
    await Promise.all(keys.slice(0, excess).map((key) => cache.delete(key)));
}

async function articleNetworkFirst(request) {
    const cache = await caches.open(ARTICLES_CACHE);
    let response;
    try {
        response = await fetch(request);
    } catch (err) {
        // Network failed outright -- fall back to whatever's cached.
        const cached = await cache.match(request);
        if (cached) return cached;
        throw err;
    }
    // The network succeeded: `response` is always returned below, no matter
    // what happens in the cache-write path. That write is isolated in its
    // own try/catch on purpose -- `cache.put()` (and the trim that follows
    // it) can throw QuotaExceededError, plausible on iOS Safari's much
    // tighter Cache Storage budget, and a cache-write failure must never
    // discard a good, already-in-hand network response by falling through
    // to a stale cached copy (or throwing) the way a shared try/block above
    // would. `clone()` before `put()` is required regardless: a Response
    // body can only be consumed once, and the caller below still needs the
    // original to read/return.
    if (response.ok) {
        try {
            await cache.put(request, response.clone());
            await trimArticleCache(cache);
        } catch (err) {
            console.debug("Tiro: article cache write failed, serving network response anyway", err);
        }
    }
    return response;
}

async function navigateWithOfflineFallback(request) {
    try {
        return await fetch(request);
    } catch (err) {
        const cache = await caches.open(STATIC_CACHE);
        const fallback = await cache.match("/offline");
        if (fallback) return fallback;
        throw err;
    }
}

self.addEventListener("fetch", (event) => {
    const request = event.request;
    const route = swRouteFor(request.url, request.method, request.mode);

    if (route === "network-only") {
        // Do not call respondWith at all -- this is a true passthrough,
        // not a cache strategy that happens to always miss. Covers every
        // mutation (POST/PATCH/DELETE/PUT), every other /api/* endpoint,
        // and /login*, /logout, /setup/* per the binding spec. Responses
        // to credentialed page requests (which could carry Set-Cookie)
        // are never routed through a cache-writing strategy here -- the
        // only two strategies that ever call cache.put() are
        // static-cache-first (versioned, cookie-free static assets) and
        // article-network-first (a JSON API response, not an HTML page,
        // so it never carries a session-establishing Set-Cookie either).
        return;
    }

    if (route === "static-cache-first") {
        event.respondWith(cacheFirst(request));
        return;
    }

    if (route === "article-network-first") {
        event.respondWith(articleNetworkFirst(request));
        return;
    }

    if (route === "navigate-offline-fallback") {
        event.respondWith(navigateWithOfflineFallback(request));
        return;
    }
});
