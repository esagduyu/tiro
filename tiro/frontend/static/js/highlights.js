/* Tiro — /highlights review view (M2.2 Task 4).
 *
 * Leaf entry module (imports core.js only, nothing imports this file) — same
 * `?v={{ static_v }}` cache-bust convention as sources.js/wiki.js.
 *
 * Data source: the flat `GET /api/highlights` list (tiro/api/routes_annotations.py),
 * NOT the per-article `GET /api/articles/{id}/annotations` payload T2/T3 use inside
 * the reader — this page is a cross-article view. Every filter (`color`, `source_id`,
 * `date_from`, `date_to`) plus `limit`/`offset` is applied SERVER-SIDE via query
 * params on every (re)fetch; the client never filters a giant already-fetched list.
 *
 * Judgment calls (documented per the task brief's "judge; document"):
 *  - Color filter is a single-select toggle, not a multi-select checklist: the API
 *    exposes exactly one `color` param (not a list), so at most one color can be
 *    filtered server-side at a time. Clicking an already-active chip clears it.
 *  - The source `<select>` is populated from `GET /api/sources` (the full source
 *    list), not derived from the currently-loaded highlights page. This keeps the
 *    dropdown's option set stable as filters narrow the visible rows, rather than
 *    shrinking confusingly as you filter.
 *  - Grouping: the API returns a flat list ordered by `h.created_at DESC` (not
 *    pre-grouped). This page groups client-side by `article_id`, using each
 *    article's FIRST appearance in that globally-sorted list to order the groups
 *    — since the list is already newest-highlight-first, this yields "most
 *    recently highlighted article first" for free, with no extra sort. Pagination
 *    (`Load more`) fetches the next page and appends into the same running
 *    groups map rather than re-sorting from scratch.
 *  - Click-through + flash: a row click stores the highlight's uid in
 *    `sessionStorage['tiro:flash-highlight']` and navigates to `/articles/{id}`.
 *    reader.js's `loadAnnotations()` (M2.2 Task 3) checks for that key once its
 *    highlights are painted and calls the EXISTING `flashHighlightRange(uid)` —
 *    the same scrollIntoView-plus-transient-CSS-flash helper T3 built for
 *    panel-row clicks. No new flash mechanism was written; this page just hands
 *    off a uid for T3's reader-side helper to consume. If the uid can't be
 *    resolved to a painted Range (e.g. an unanchored highlight), that helper
 *    already no-ops gracefully — nothing to handle here.
 *  - Note excerpts are plain, escaped, truncated text — `renderMarkdown` is
 *    deliberately NOT used on this page (per the brief's safety constraint).
 */

import { esc, formatDate } from "./core.js";
import { icon } from "./icons.js";

const LIMIT = 50;

let offset = 0;
let total = 0;
let groups = new Map(); // article_id -> { article_id, article_title, source_name, items: [] }
let groupOrder = []; // article_id insertion order = first-seen order in the sorted list

// Latest-wins request token (review fix, M2.2 Task 4 wave 1). Concurrent
// fetches are allowed to run — no in-flight early-return — but a response is
// only applied (rendered + folded into offset/total bookkeeping) if its
// captured token is still the current `fetchToken` when it resolves. Any
// state-changing call (filter click, clear, initial load, or Load more) bumps
// the token, so a fast filter-flip that fires a second request before the
// first's response lands can no longer have the first (now-stale) response
// clobber the second's result — whichever response arrives, only the one
// matching the LATEST call's token gets rendered. This also makes Load more
// safe against a filter change landing mid-request: the filter change bumps
// the token (and resets offset/groups), so a late Load-more response for the
// abandoned filter combination is dropped instead of appending stale rows
// onto the freshly reset list.
let fetchToken = 0;

const filters = {
    color: null,
    sourceId: null,
    dateFrom: null,
    dateTo: null,
};

document.addEventListener("DOMContentLoaded", () => {
    loadSourceOptions();
    setupFilters();
    setupKeyboard();
    fetchHighlights(true);
});

/* --- Data loading --- */

function buildParams() {
    const params = new URLSearchParams();
    if (filters.color) params.set("color", filters.color);
    if (filters.sourceId) params.set("source_id", filters.sourceId);
    if (filters.dateFrom) params.set("date_from", filters.dateFrom);
    if (filters.dateTo) params.set("date_to", filters.dateTo);
    params.set("limit", String(LIMIT));
    params.set("offset", String(offset));
    return params;
}

async function fetchHighlights(reset) {
    const token = ++fetchToken;

    if (reset) {
        offset = 0;
        groups = new Map();
        groupOrder = [];
    }

    const statusEl = document.getElementById("highlights-status");
    const emptyEl = document.getElementById("highlights-empty");
    const loadMoreWrap = document.getElementById("highlights-load-more-wrap");
    const loadMoreBtn = document.getElementById("highlights-load-more");

    if (reset) {
        statusEl.style.display = "block";
        statusEl.innerHTML =
            '<div class="settings-loading"><div class="digest-spinner"></div><p>Loading highlights...</p></div>';
        emptyEl.style.display = "none";
        document.getElementById("highlights-groups").innerHTML = "";
    } else if (loadMoreBtn) {
        loadMoreBtn.disabled = true;
        loadMoreBtn.textContent = "Loading...";
    }

    try {
        const res = await fetch(`/api/highlights?${buildParams().toString()}`);
        const json = await res.json();
        if (!json.success) throw new Error("Invalid response");

        // Stale response guard: a newer call (filter click, clear, or another
        // Load more) already bumped fetchToken past ours — discard silently,
        // the newer call owns rendering the current state.
        if (token !== fetchToken) return;

        const rows = json.data.highlights || [];
        total = json.data.total || 0;
        offset += rows.length;

        for (const h of rows) {
            if (!groups.has(h.article_id)) {
                groups.set(h.article_id, {
                    article_id: h.article_id,
                    article_title: h.article_title,
                    source_name: h.source_name,
                    items: [],
                });
                groupOrder.push(h.article_id);
            }
            groups.get(h.article_id).items.push(h);
        }

        statusEl.style.display = "none";

        const totalLoaded = groupOrder.reduce((n, id) => n + groups.get(id).items.length, 0);
        if (totalLoaded === 0) {
            emptyEl.style.display = "block";
            const heading = emptyEl.querySelector("h2");
            const sub = emptyEl.querySelector("p");
            if (hasAnyFilter()) {
                if (heading) heading.textContent = "No highlights match these filters";
                if (sub) sub.textContent = "Try clearing a filter or widening the date range.";
            } else {
                if (heading) heading.textContent = "No highlights yet";
                if (sub) sub.textContent = "Select text in the reader and choose a color to create your first highlight.";
            }
        } else {
            emptyEl.style.display = "none";
        }

        renderGroups();
        loadMoreWrap.style.display = totalLoaded < total ? "flex" : "none";
    } catch (err) {
        if (token !== fetchToken) return; // stale error — a newer request already owns the view
        console.error("Failed to load highlights:", err);
        statusEl.style.display = "block";
        statusEl.innerHTML = '<p class="settings-error">Failed to load highlights.</p>';
    } finally {
        if (loadMoreBtn) {
            loadMoreBtn.disabled = false;
            loadMoreBtn.textContent = "Load more";
        }
    }
}

async function loadSourceOptions() {
    try {
        const res = await fetch("/api/sources");
        const json = await res.json();
        if (!json.success) return;
        const select = document.getElementById("highlights-source-select");
        if (!select) return;
        const options = (json.data || [])
            .map((s) => `<option value="${s.id}">${esc(s.name || "Unnamed")}</option>`)
            .join("");
        select.insertAdjacentHTML("beforeend", options);
    } catch (err) {
        console.error("Failed to load sources for filter:", err);
    }
}

/* --- Rendering --- */

function truncate(str, n) {
    if (!str) return "";
    return str.length > n ? `${str.slice(0, n).trimEnd()}…` : str;
}

function highlightRowHtml(h) {
    const quote = truncate(h.quote_text || "", 200);
    const noteExcerpt = h.note_markdown ? truncate(h.note_markdown, 140) : null;
    return `
        <div class="highlight-row hl-list-row" data-uid="${esc(h.uid)}" data-article-id="${h.article_id}">
            <div class="highlight-row-main">
                <span class="highlight-color-dot" data-color="${esc(h.color)}"></span>
                <span class="highlight-quote">${esc(quote)}</span>
            </div>
            ${noteExcerpt
                ? `<div class="hl-list-note"><span class="hl-list-note-icon">${icon("note", { size: 13 })}</span><span>${esc(noteExcerpt)}</span></div>`
                : ""}
            <div class="hl-list-meta">${esc(formatDate(h.created_at))}</div>
        </div>
    `;
}

function groupHtml(group) {
    const rows = group.items.map(highlightRowHtml).join("");
    return `
        <div class="highlights-group">
            <div class="highlights-group-header">
                <span class="highlights-group-title">
                    <a href="/articles/${group.article_id}">${esc(group.article_title || "Untitled")}</a>
                </span>
                ${group.source_name ? `<span class="highlights-group-source">${esc(group.source_name)}</span>` : ""}
            </div>
            <div class="highlights-list">${rows}</div>
        </div>
    `;
}

function renderGroups() {
    const container = document.getElementById("highlights-groups");
    container.innerHTML = groupOrder.map((id) => groupHtml(groups.get(id))).join("");

    container.querySelectorAll(".hl-list-row").forEach((row) => {
        row.addEventListener("click", () => {
            const uid = row.dataset.uid;
            const articleId = row.dataset.articleId;
            try {
                sessionStorage.setItem("tiro:flash-highlight", uid);
            } catch (err) {
                // sessionStorage unavailable (private mode / disabled) — navigation
                // still works, it just won't scroll-to-flash on arrival.
            }
            window.location.href = `/articles/${articleId}`;
        });
    });
}

/* --- Filters --- */

function hasAnyFilter() {
    return !!(filters.color || filters.sourceId || filters.dateFrom || filters.dateTo);
}

function updateClearButton() {
    const btn = document.getElementById("highlights-filter-clear");
    if (btn) btn.style.display = hasAnyFilter() ? "inline-block" : "none";
}

function setupFilters() {
    document.querySelectorAll(".hl-filter-chip").forEach((chip) => {
        chip.addEventListener("click", () => {
            const color = chip.dataset.color;
            filters.color = filters.color === color ? null : color;
            document.querySelectorAll(".hl-filter-chip").forEach((c) => {
                c.classList.toggle("active", c.dataset.color === filters.color);
            });
            updateClearButton();
            fetchHighlights(true);
        });
    });

    const sourceSelect = document.getElementById("highlights-source-select");
    sourceSelect?.addEventListener("change", () => {
        filters.sourceId = sourceSelect.value || null;
        updateClearButton();
        fetchHighlights(true);
    });

    const dateFrom = document.getElementById("highlights-date-from");
    dateFrom?.addEventListener("change", () => {
        filters.dateFrom = dateFrom.value || null;
        updateClearButton();
        fetchHighlights(true);
    });

    const dateTo = document.getElementById("highlights-date-to");
    dateTo?.addEventListener("change", () => {
        filters.dateTo = dateTo.value || null;
        updateClearButton();
        fetchHighlights(true);
    });

    document.getElementById("highlights-filter-clear")?.addEventListener("click", () => {
        filters.color = null;
        filters.sourceId = null;
        filters.dateFrom = null;
        filters.dateTo = null;
        document.querySelectorAll(".hl-filter-chip").forEach((c) => c.classList.remove("active"));
        if (sourceSelect) sourceSelect.value = "";
        if (dateFrom) dateFrom.value = "";
        if (dateTo) dateTo.value = "";
        updateClearButton();
        fetchHighlights(true);
    });

    document.getElementById("highlights-load-more")?.addEventListener("click", () => fetchHighlights(false));
}

/* --- Keyboard (same local pattern as sources.js/wiki.js — no shared
   showShortcuts import, since this page's own overlay content differs) --- */

function setupKeyboard() {
    document.addEventListener("keydown", (e) => {
        const tag = document.activeElement.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
            if (e.key === "Escape") {
                document.activeElement.blur();
                e.preventDefault();
            }
            return;
        }

        const overlay = document.getElementById("shortcuts-overlay");
        if (overlay && overlay.style.display !== "none") {
            if (e.key === "?" || e.key === "Escape") {
                overlay.style.display = "none";
                e.preventDefault();
            }
            return;
        }

        switch (e.key) {
            case "b":
            case "Escape":
                e.preventDefault();
                window.location.href = "/inbox";
                break;
            case "?":
                e.preventDefault();
                showHighlightsShortcuts();
                break;
        }
    });

    const closeBtn = document.getElementById("shortcuts-close");
    closeBtn?.addEventListener("click", () => {
        document.getElementById("shortcuts-overlay").style.display = "none";
    });
    const overlayEl = document.getElementById("shortcuts-overlay");
    overlayEl?.addEventListener("click", (e) => {
        if (e.target === overlayEl) overlayEl.style.display = "none";
    });
}

function showHighlightsShortcuts() {
    const overlay = document.getElementById("shortcuts-overlay");
    const body = document.getElementById("shortcuts-body");
    if (!overlay || !body) return;

    const shortcuts = [
        { section: "Navigation" },
        { keys: ["b", "Esc"], desc: "Back to inbox" },
        { section: "General" },
        { keys: ["?"], desc: "Show this help" },
    ];

    body.innerHTML = shortcuts
        .map((item) => {
            if (item.section) {
                return `<div class="shortcut-section">${esc(item.section)}</div>`;
            }
            const keys = item.keys.map((k) => `<kbd>${esc(k)}</kbd>`).join(" / ");
            return `<div class="shortcut-row"><span class="shortcut-keys">${keys}</span><span class="shortcut-desc">${esc(item.desc)}</span></div>`;
        })
        .join("");

    overlay.style.display = "flex";
}
