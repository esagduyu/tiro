// node:test coverage for js/sw-routing.js — the service worker's PURE
// routing core. See sw-routing.js's own header comment for why this is a
// plain module rather than something exercised only inside a real
// ServiceWorkerGlobalScope: it takes/returns plain values, so it runs
// directly under node exactly like core.js's esc/num/formatDate/timeAgo.
//
// The adversarial table below is the binding spec's own required cases
// (M3.1 Task 2 brief) plus a few extra boundary checks in the same spirit.

import { test } from "node:test";
import assert from "node:assert/strict";
import { swRouteFor } from "../sw-routing.js";

test("swRouteFor: adversarial table", () => {
    const cases = [
        // [url, method, mode, expected]
        ["https://tiro.local/static/x", "POST", "same-origin", "network-only"],
        ["https://tiro.local/api/articles/5/annotations", "GET", "cors", "network-only"],
        ["https://tiro.local/api/articles", "GET", "cors", "network-only"],
        ["https://tiro.local/api/articles/5", "GET", "cors", "article-network-first"],
        ["https://tiro.local/login/qr", "GET", "navigate", "network-only"],
        ["https://tiro.local/inbox", "GET", "navigate", "navigate-offline-fallback"],
        ["https://tiro.local/sw.js", "GET", "same-origin", "network-only"],
    ];
    for (const [url, method, mode, expected] of cases) {
        assert.equal(
            swRouteFor(url, method, mode),
            expected,
            `swRouteFor(${url}, ${method}, ${mode})`
        );
    }
});

test("swRouteFor: static assets are cache-first regardless of extension", () => {
    assert.equal(swRouteFor("https://tiro.local/static/js/core.js?v=63", "GET", "same-origin"), "static-cache-first");
    assert.equal(swRouteFor("https://tiro.local/static/vendor/marked.min.js", "GET", "same-origin"), "static-cache-first");
    assert.equal(swRouteFor("https://tiro.local/static/themes/papyrus.css", "GET", "same-origin"), "static-cache-first");
});

test("swRouteFor: mutations to the article endpoint are still network-only", () => {
    assert.equal(swRouteFor("https://tiro.local/api/articles/5", "PATCH", "same-origin"), "network-only");
    assert.equal(swRouteFor("https://tiro.local/api/articles/5", "DELETE", "same-origin"), "network-only");
    assert.equal(swRouteFor("https://tiro.local/api/articles/5", "PUT", "same-origin"), "network-only");
    assert.equal(swRouteFor("https://tiro.local/api/articles", "POST", "same-origin"), "network-only");
});

test("swRouteFor: auth/setup surfaces never fall back to /offline, even as navigations", () => {
    assert.equal(swRouteFor("https://tiro.local/login", "GET", "navigate"), "network-only");
    assert.equal(swRouteFor("https://tiro.local/logout", "GET", "navigate"), "network-only");
    assert.equal(swRouteFor("https://tiro.local/setup/qr", "GET", "navigate"), "network-only");
    assert.equal(swRouteFor("https://tiro.local/setup/remote", "GET", "navigate"), "network-only");
    // /welcome is the first-run onboarding wizard — a failed nav must NOT
    // serve /offline mid-setup (M3.1 NEVER_FALLBACK convention).
    assert.equal(swRouteFor("https://tiro.local/welcome", "GET", "navigate"), "network-only");
});

test("swRouteFor: non-navigation same-origin fetches to app pages are network-only (not fallback-eligible)", () => {
    // e.g. a `fetch('/digest')` from JS (mode !== 'navigate') must not be
    // treated as a page navigation eligible for the offline fallback.
    assert.equal(swRouteFor("https://tiro.local/digest", "GET", "cors"), "network-only");
    assert.equal(swRouteFor("https://tiro.local/digest", "GET", "same-origin"), "network-only");
});

test("swRouteFor: relative URLs resolve the same as absolute ones", () => {
    assert.equal(swRouteFor("/api/articles/42", "GET", "same-origin"), "article-network-first");
    assert.equal(swRouteFor("/static/styles.css", "GET", "same-origin"), "static-cache-first");
});

test("swRouteFor: article id must be purely numeric", () => {
    assert.equal(swRouteFor("https://tiro.local/api/articles/abc", "GET", "cors"), "network-only");
    assert.equal(swRouteFor("https://tiro.local/api/articles/5/", "GET", "cors"), "network-only");
    assert.equal(swRouteFor("https://tiro.local/api/articles/5x", "GET", "cors"), "network-only");
});

test("swRouteFor: /offline itself is a normal navigable page (navigate-offline-fallback)", () => {
    assert.equal(swRouteFor("https://tiro.local/offline", "GET", "navigate"), "navigate-offline-fallback");
});
