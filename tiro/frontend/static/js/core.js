/* Tiro — shared frontend module (M2.0).
 *
 * Never loaded by a template tag directly — reached only via relative
 * `import ... from "./core.js"` from every other frontend module
 * (sidebar.js, inbox.js, digest.js, reader.js, sources.js, wiki.js), per
 * the ES-modules migration (see
 * docs/plans/2026-07-05-m2-0-frontend-modules-plan.md and
 * .superpowers/sdd/task-1-report.md through task-5-report.md for the
 * per-task migration history). base.html's import map (added in T5) maps
 * the resolved "/static/js/core.js" specifier to a cache-busted URL so this
 * file's updates invalidate stale clients like every other static asset.
 *
 * Pure functions (esc/num/formatDate/timeAgo) are covered by node:test in
 * js/tests/core.test.mjs with no DOM. renderMarkdown/apiFetch/showToast/
 * confirmDialog touch window/document/fetch and are intentionally excluded
 * from the node harness (per the plan's decision: no jsdom, no DOM
 * simulation) — they are exercised by Playwright once wired into templates.
 */

/**
 * Escape `&`, literal U+00A0 (NO-BREAK SPACE), `<`, `>`, `"`, `'` for safe
 * HTML text/attribute interpolation.
 *
 * Pure reimplementation of the historical DOM-trick version used across
 * app.js/reader.js/sources.js/wiki.js:
 *
 *   function esc(str) {
 *       const el = document.createElement("span");
 *       el.textContent = str;
 *       return el.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
 *   }
 *
 * Byte-identical output was verified against that exact DOM version across
 * a wide input table (empty string, plain text, `&amp;` needing
 * double-escape, `<script>`, quotes, backticks, `null`/`undefined`/numbers/
 * booleans, literal U+00A0) using headless Chromium via Playwright — see
 * .superpowers/sdd/task-1-report.md for the method and full case table.
 *
 * Order matters and mirrors the browser's own text-node HTML serialization:
 * `&` is escaped FIRST, so an input that already contains `&amp;` becomes
 * `&amp;amp;` (matching the DOM version) rather than staying `&amp;`.
 * `null` and `undefined` both become `""` (assigning either to
 * `Node.textContent` — a `[LegacyNullToEmptyString]` IDL attribute — yields
 * an empty string; only `null` is special-cased in the Web IDL spec text,
 * but `undefined` on a nullable type converts the same way in practice, and
 * this was verified empirically above).
 *
 * Backticks are intentionally NOT escaped — neither was the DOM version.
 */
export function esc(str) {
    const s = str === null || str === undefined ? "" : String(str);
    return s
        .replace(/&/g, "&amp;")
        .replace(/\u00A0/g, "&nbsp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

/**
 * Coerce to a finite number for display, falling back to "?" for
 * non-finite/non-numeric input. Matches reader.js/sources.js/wiki.js's num().
 */
export function num(x) {
    const n = Number(x);
    return Number.isFinite(n) ? n : "?";
}

/**
 * "Jul 5" if isoStr falls in the current year, "Jul 5, 2025" otherwise.
 * Matches app.js's formatDate(). Empty string for falsy input.
 */
export function formatDate(isoStr) {
    if (!isoStr) return "";
    const d = new Date(isoStr);
    const now = new Date();
    const months = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];

    if (d.getFullYear() === now.getFullYear()) {
        return `${months[d.getMonth()]} ${d.getDate()}`;
    }
    return `${months[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
}

/**
 * "just now" / "5m ago" / "3h ago" / "yesterday" / "N days ago" relative to
 * now. Matches app.js's/reader.js's timeAgo(). `then` must be a Date.
 */
export function timeAgo(then) {
    const diffMs = new Date() - then;
    const diffMin = Math.floor(diffMs / 60000);
    const diffHr = Math.floor(diffMin / 60);
    const diffDay = Math.floor(diffHr / 24);

    if (diffMin < 1) return "just now";
    if (diffMin < 60) return `${diffMin}m ago`;
    if (diffHr < 24) return `${diffHr}h ago`;
    if (diffDay === 1) return "yesterday";
    return `${diffDay} days ago`;
}

/**
 * Render markdown to sanitized HTML: marked.parse() -> DOMPurify.sanitize().
 * Config copied VERBATIM from app.js's renderMarkdown() — FORBID_TAGS/
 * FORBID_ATTR/ADD_ATTR unchanged. This stays the ONLY body-render path per
 * the XSS invariant recorded in CLAUDE.md.
 *
 * Depends on window.marked / window.DOMPurify. Vendor scripts stay classic
 * `<script>` tags (not modules) per the plan's "vendor-stays-classic"
 * constraint, and classic scripts execute before deferred modules, so by
 * the time any module calls this, both are guaranteed to be on window.
 * Guarded with a clear error if that invariant is ever violated.
 *
 * marked.setOptions() is applied once (lazily, on first render) rather than
 * per-call, matching the originals' load-time configuration — still safe if
 * marked finishes loading after this module.
 */
let _markedConfigured = false;

export function renderMarkdown(md) {
    if (typeof window === "undefined" || !window.marked || !window.DOMPurify) {
        throw new Error(
            "renderMarkdown() requires window.marked and window.DOMPurify " +
            "(vendor scripts) to be loaded before this module runs"
        );
    }
    if (!_markedConfigured) {
        window.marked.setOptions({ breaks: false, gfm: true });
        _markedConfigured = true;
    }
    const raw = window.marked.parse(md || "");
    return window.DOMPurify.sanitize(raw, {
        FORBID_TAGS: ["script", "iframe", "object", "embed", "form", "style"],
        FORBID_ATTR: ["onerror", "onclick", "onload", "onmouseover"],
        ADD_ATTR: ["loading"],
    });
}

/**
 * fetch() wrapper normalizing to a `{success, data, error, status}`
 * envelope. ADDITIVE helper: no existing call site uses this yet (M2.0 is
 * scaffolding only) — call sites migrate opportunistically in later tasks,
 * not big-bang. If the server response body already has a `success` key
 * (the API's existing envelope shape), it is passed through unchanged.
 * Network failures and non-2xx responses are normalized to
 * `{success: false, error, status}`.
 */
export async function apiFetch(url, opts = {}) {
    let res;
    try {
        res = await fetch(url, opts);
    } catch (err) {
        return { success: false, error: "Connection error", status: 0 };
    }

    let json = null;
    try {
        json = await res.json();
    } catch (err) {
        json = null;
    }

    if (!res.ok) {
        const error = (json && (json.detail || json.error)) || `Request failed (${res.status})`;
        return { success: false, error, status: res.status, data: json };
    }

    if (json && typeof json === "object" && "success" in json) {
        return json;
    }

    return { success: true, data: json, status: res.status };
}

/**
 * Transient toast notification. Extracted from sources.js's showToast() —
 * same DOM structure and CSS classes (.settings-toast[.show],
 * .settings-toast-{success,error,warning,info}) so existing styles.css
 * rules apply unchanged once callers migrate to this.
 */
export function showToast(message, type) {
    // Exclude #undo-toast (Finding 7, M3.2 final review): inbox.js's undo
    // binder (js/undo.js) manages that toast's own lifecycle -- an armed
    // undo slot, a live setTimeout, and a "u" key binding all still valid.
    // Removing it here (it also matches the plain ".settings-toast" class
    // for shared CSS) without clearing that state would silently orphan a
    // still-armed undo slot: the user could no longer see or reach the
    // Undo button, yet "u" would still fire it. Excluding it is the safer
    // of the two fixes (the alternative -- clearing the undo slot whenever
    // a plain toast displaces it -- would need this module to reach into
    // inbox.js's undo state); a plain toast now simply renders alongside
    // a live undo toast instead of silently disappearing it.
    const existing = document.querySelector(".settings-toast:not(#undo-toast)");
    if (existing) existing.remove();

    const toast = document.createElement("div");
    toast.className = "settings-toast settings-toast-" + (type || "info");
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => toast.classList.add("show"), 10);
    setTimeout(() => {
        toast.classList.remove("show");
        setTimeout(() => toast.remove(), 300);
    }, 3500);
}

/**
 * Promise-based confirm dialog. Extracted from the shared overlay pattern
 * behind app.js's showDeleteConfirm() and sources.js's confirmDeleteSource()
 * (same .export-overlay/.export-dialog/.export-dialog-actions/
 * .export-cancel-btn/.danger-confirm-btn classes — existing styles.css
 * rules apply unchanged). Resolves `true` on confirm, `false` on
 * cancel/Escape/backdrop click.
 *
 * @param {string} bodyHtml - dialog body; caller is responsible for
 *   escaping any interpolated values (e.g. via esc()) before passing HTML.
 * @param {object} [options]
 * @param {string} [options.title="Confirm"]
 * @param {string} [options.confirmText="Confirm"]
 * @param {string} [options.cancelText="Cancel"]
 * @param {boolean} [options.danger=true] - use the danger (red) confirm
 *   button style (.danger-confirm-btn) vs. the neutral accent style
 *   (.export-confirm-btn)
 * @returns {Promise<boolean>}
 */
export function confirmDialog(bodyHtml, options = {}) {
    const {
        title = "Confirm",
        confirmText = "Confirm",
        cancelText = "Cancel",
        danger = true,
    } = options;

    return new Promise((resolve) => {
        const existing = document.getElementById("core-confirm-overlay");
        if (existing) existing.remove();

        const overlay = document.createElement("div");
        overlay.id = "core-confirm-overlay";
        overlay.className = "export-overlay";
        overlay.innerHTML =
            '<div class="export-dialog">' +
                `<h3>${esc(title)}</h3>` +
                `<p>${bodyHtml}</p>` +
                '<div class="export-dialog-actions">' +
                    `<button class="export-cancel-btn" id="core-confirm-cancel">${esc(cancelText)}</button>` +
                    `<button class="${danger ? "danger-confirm-btn" : "export-confirm-btn"}" id="core-confirm-confirm">${esc(confirmText)}</button>` +
                "</div>" +
            "</div>";
        document.body.appendChild(overlay);

        function onKeydown(e) {
            if (e.key === "Escape") settle(false);
        }
        function settle(result) {
            overlay.remove();
            document.removeEventListener("keydown", onKeydown);
            resolve(result);
        }
        document.getElementById("core-confirm-cancel").addEventListener("click", () => settle(false));
        document.getElementById("core-confirm-confirm").addEventListener("click", () => settle(true));
        overlay.addEventListener("click", (e) => {
            if (e.target === overlay) settle(false);
        });
        document.addEventListener("keydown", onKeydown);
    });
}
