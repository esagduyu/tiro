/* Tiro — inbox (article list) module (M2.0 split of app.js, Task 2).
 *
 * Owns the /inbox page only: article list rendering + sort, search, tier
 * toolbar (classify/discard/archived/snoozed/VIP-only), the filter panel +
 * active filter pills, pagination, inbox keyboard navigation, single/bulk
 * delete, (M3.2 Task 1) the per-card overflow menu + snooze preset sheet +
 * wake-time chip, and (M3.2 Task 3) swipe triage (js/swipe.js pointer
 * wiring: right → archive, left → snooze sheet) plus the single-slot undo
 * binder (js/undo.js + 5s toast + `u` key). Loaded as
 * `<script type="module">` from inbox.html only.
 *
 * Digest logic (previously interleaved in the same app.js file) now lives
 * in its own js/digest.js module — see that file's header for why the split
 * was clean rather than the "keep entangled" fallback the plan allowed.
 *
 * Two small pieces of dead code from the pre-M7 single-page (inbox+digest
 * tabs) layout were dropped rather than carried over, since the DOM they
 * target no longer exists in any template (verified via grep across
 * templates/*.html — zero matches) and dropping them is a true no-op:
 *   - `setupViewTabs()` / the `.view-tab` / `#view-articles` / `#view-digest`
 *     branch (digest moved to its own /digest route in Checkpoint 22).
 *   - The `if (document.querySelector(".digest-tab")) loadDigest(...)`
 *     branch inside the "r" keyboard case — `.digest-tab` never appears on
 *     /inbox, so this never fired; the `e.preventDefault()` for "r" is kept
 *     verbatim for behavior parity, the dead body is not.
 * See .superpowers/sdd/task-2-report.md for the full audit.
 */

import { esc, num, formatDate, showToast } from "./core.js";
import { icon } from "./icons.js";
import {
    showShortcuts, hideShortcuts, openSaveModal, closeSaveModal,
    loadSavedViews as refreshSidebarViews,
    updateUnreadBadge, getUnreadCount, adjustUnreadCount,
} from "./sidebar.js";
import { createSwipeState, swipeEvent } from "./swipe.js";
import { createUndoManager, pushUndoable, takeUndo, clearUndo } from "./undo.js";

let currentSort = "unread"; // "unread" | "newest" | "oldest" | "importance"
let cachedArticles = []; // store articles for re-sorting without re-fetching
let selectedIndex = -1; // keyboard-selected article index
let showArchived = false; // whether to include decayed articles
let showSnoozed = false; // whether to include snoozed articles (M3.2)
// Library view (owner UX wave 1). The inbox is UNREAD-FIRST by default: the
// list fetch pins is_read=false so triage only ever surfaces what's still
// unread. "Library view" is the escape hatch to your whole collection —
// read + unread. Composition decision (documented): Library view = drop the
// is_read filter AND include decayed/archived articles (so your full history
// is reachable in one place), while "Show archived" stays a distinct, finer
// toggle that reveals decayed/discarded rows WITHIN whichever mode is active.
// The two are orthogonal axes: read/unread (Library) vs. active/decayed
// (Show archived). Linkable + reload-durable via ?view=library.
let libraryView = false;
let showVIPOnly = false; // whether to filter to VIP articles only
let currentPage = 1;
let perPage = 50; // default page size
let activeFilters = {}; // e.g. { is_read: "false", ai_tier: "must-read", tag: "AI" }
let filterPanelOpen = false;
let filterData = null; // cached /api/filters response
let selectedForDelete = new Set(); // article ids checked for bulk delete
let searchActive = false; // whether a live search query is showing results (M3.2 Task 4)
// Sticky flag (M3.2 Task 4): true once ANY loadInbox() fetch this session has
// proven the library has at least one article (in any view -- filtered or
// not). Distinct from `cachedArticles.length`, which the triage-removal
// paths trim locally without a re-fetch: cachedArticles going to 0 by
// swiping every loaded card away is exactly the "reached inbox zero"
// condition worth celebrating, while a *server* response with zero rows on
// a never-touched page is the ambiguous "could be a fresh install" case
// that must NOT flip this (see loadInbox()'s early-return branch, which
// never sets this true). Never reset to false once true this session.
let libraryEverHadArticles = false;

/* ---- Inbox (articles list) ---- */

function buildQueryString() {
    const params = new URLSearchParams();

    // Pagination
    if (perPage > 0) {
        params.set("per_page", perPage);
        params.set("page", currentPage);
    }

    // Sort
    params.set("sort", currentSort);

    // Unread-first default (owner UX wave 1): the inbox shows only unread
    // articles unless the user is in Library view. An explicit filter-panel
    // is_read choice (activeFilters.is_read, applied below) overrides this
    // baseline. Snoozed articles are already excluded server-side by default.
    if (!libraryView && activeFilters.is_read === undefined) {
        params.set("is_read", "false");
    }

    // Archived / decayed. Library view forces decayed rows visible (your full
    // history lives there); the triage inbox hides them unless "Show archived".
    if (!showArchived && !libraryView) {
        params.set("include_decayed", "false");
    }

    // Snoozed (M3.2) — API defaults to excluding snoozed articles from the
    // inbox, so only set the param when the toggle is ON (opposite polarity
    // from include_decayed, whose default is permissive and needs an
    // explicit "false" to hide).
    if (showSnoozed) {
        params.set("include_snoozed", "true");
    }

    // Active filters
    for (const [key, val] of Object.entries(activeFilters)) {
        if (val !== null && val !== undefined && val !== "") {
            params.set(key, val);
        }
    }

    return params.toString();
}

function syncURLWithFilters() {
    const params = new URLSearchParams();
    for (const [key, val] of Object.entries(activeFilters)) {
        if (val !== null && val !== undefined && val !== "") {
            params.set(key, val);
        }
    }
    if (currentSort !== "newest") params.set("sort", currentSort);
    if (currentPage > 1) params.set("page", currentPage);
    // Library view (owner UX wave 1) — keep the URL linkable + reload-durable.
    if (libraryView) params.set("view", "library");
    const qs = params.toString();
    const url = "/inbox" + (qs ? "?" + qs : "");
    window.history.replaceState({}, "", url);
}

function restoreFiltersFromURL() {
    const params = new URLSearchParams(window.location.search);
    const filterKeys = [
        "is_read", "is_vip", "ai_tier", "author", "source_id", "tag",
        "rating", "ingestion_method", "min_reading_time", "max_reading_time",
        "has_audio", "date_from", "date_to",
    ];
    for (const key of filterKeys) {
        if (params.has(key)) {
            activeFilters[key] = params.get(key);
        }
    }
    if (params.has("sort")) currentSort = params.get("sort");
    if (params.has("page")) currentPage = parseInt(params.get("page"), 10) || 1;
    // Library view (owner UX wave 1) — ?view=library survives reload and makes
    // the inbox-zero "Browse your library" link a real deep link.
    if (params.get("view") === "library") libraryView = true;
    // Update tab indicator after restoring filters
    setTimeout(() => updateFilterTabIndicator(), 0);
}

async function loadInbox() {
    const listEl = document.getElementById("article-list");
    if (!listEl) return;
    const emptyEl = document.getElementById("empty-state");
    const toolbar = document.getElementById("inbox-toolbar");

    try {
        const qs = buildQueryString();
        const res = await fetch(`/api/articles?${qs}`);
        const json = await res.json();

        if (!json.success || !json.data.length) {
            // Never the inbox-zero celebratory state here (M3.2 Task 4) --
            // a server-side zero-results response is exactly the ambiguous
            // case that state must NOT claim (could be a genuinely fresh,
            // never-saved-anything library, which "no articles yet" already
            // handles below). The celebratory state only ever turns on from
            // the client-side triage-removal paths in performArchive/
            // performSnooze; resetting cachedArticles + hiding it here
            // keeps a stale banner from lingering across an unrelated
            // reload/filter/toggle.
            cachedArticles = [];
            const zeroEl = document.getElementById("inbox-zero-state");
            if (zeroEl) zeroEl.style.display = "none";
            emptyEl.style.display = Object.keys(activeFilters).length ? "none" : "block";
            if (toolbar) toolbar.style.display = Object.keys(activeFilters).length ? "flex" : "none";
            listEl.innerHTML = Object.keys(activeFilters).length
                ? '<div class="filter-loading">No articles match these filters.</div>'
                : "";
            renderTriagePill();
            renderPagination(null);
            return;
        }

        cachedArticles = json.data;
        libraryEverHadArticles = true;
        emptyEl.style.display = "none";
        renderArticleList(cachedArticles);
        updateToolbar(cachedArticles);
        renderPagination(json.pagination || null);
        renderActiveFilters();
        syncURLWithFilters();
    } catch (err) {
        console.error("Failed to load articles:", err);
        emptyEl.style.display = "block";
    }
}

function renderArticleList(articles) {
    const listEl = document.getElementById("article-list");
    // When using server-side pagination + sort, render as-is (already sorted by server)
    // Only client-sort when per_page=0 (all articles loaded)
    const toRender = perPage > 0 ? articles : sortArticles(articles, currentSort);
    // NOTE: `.map(renderArticle)` (not a wrapped arrow fn) is intentional —
    // it matches the historical app.js exactly, including Array.map's
    // implicit (item, index, array) call passing the loop index into
    // renderArticle's second param (`showScore`). That's inert today since
    // plain /api/articles rows never carry `similarity_score` (only search
    // results do, via the explicit `renderArticle(a, true)` call in
    // runSearch), but preserving the exact call shape avoids any risk of
    // behavior drift here.
    listEl.innerHTML = toRender.map(renderArticle).join("");
    attachListeners();
    selectedIndex = -1;

    const sortSelect = document.getElementById("sort-select");
    if (sortSelect) sortSelect.value = currentSort;
}

function renderSortedInbox() {
    // Re-render with client-side sort (used when per_page=0)
    renderArticleList(cachedArticles);
}

function sortArticles(articles, mode) {
    const copy = [...articles];
    const dateOf = (a) => new Date(a.published_at || a.ingested_at);
    const vipCmp = (a, b) => (b.is_vip ? 1 : 0) - (a.is_vip ? 1 : 0);

    if (mode === "unread") {
        copy.sort((a, b) => {
            // Unread first, then VIP, then newest
            if (a.is_read !== b.is_read) return a.is_read ? 1 : -1;
            const v = vipCmp(a, b);
            if (v !== 0) return v;
            return dateOf(b) - dateOf(a);
        });
    } else if (mode === "newest") {
        copy.sort((a, b) => {
            // Pure recency, VIP as tiebreaker
            const d = dateOf(b) - dateOf(a);
            if (d !== 0) return d;
            return vipCmp(a, b);
        });
    } else if (mode === "oldest") {
        copy.sort((a, b) => {
            const d = dateOf(a) - dateOf(b);
            if (d !== 0) return d;
            return vipCmp(a, b);
        });
    } else if (mode === "importance") {
        const tierOrder = { "must-read": 0, "summary-enough": 1, "discard": 2 };
        copy.sort((a, b) => {
            const ta = tierOrder[a.ai_tier] ?? 3;
            const tb = tierOrder[b.ai_tier] ?? 3;
            if (ta !== tb) return ta - tb;
            const v = vipCmp(a, b);
            if (v !== 0) return v;
            return dateOf(b) - dateOf(a);
        });
    }
    return copy;
}

// Whether `a` is currently hidden from the default inbox view by an active
// (future) snooze. Single definition shared by renderArticle's dimming,
// performSnooze's prior-state capture, and countsAsUnread() below --
// previously each computed this inline with its own copy of the same
// Safari-safe date compare (Finding 1, M3.2 Task 4 review).
// .replace(" ", "T"): snoozed_until is a naive-UTC "YYYY-MM-DD HH:MM:SS"
// string; Safari rejects the space-separated form (Invalid Date) — same
// guard digest.js/wiki.js use for their timestamp comparisons.
function isCurrentlySnoozed(a) {
    return !!(a && a.snoozed_until && new Date(a.snoozed_until.replace(" ", "T")) > new Date());
}

// Whether `a` is currently included in the shared unread count (sidebar
// badge + inbox triage pill): unread AND not hidden by an active snooze.
// The base count_only fetch (sidebar.js's updateUnreadBadge) excludes
// snoozed-unread articles server-side (include_snoozed defaults false), so
// they were never part of the count in the first place -- archiving,
// deleting, or snoozing one must NOT decrement it, and undoing that action
// must NOT re-increment it either. Used by performArchive and performDelete
// below (Finding 1 fix).
function countsAsUnread(a) {
    return !!(a && !a.is_read && !isCurrentlySnoozed(a));
}

/* ---- Per-entity action sequence tokens (Finding 2, M3.2 final review) ----

   Follows the highlights.js `fetchToken` idiom, but keyed PER ENTITY (an
   article id or a source id) rather than one page-global counter -- an
   action on a DIFFERENT article/source must never be blocked or
   invalidated by this. Every undo-adjacent triage action (rate, archive,
   snooze, wake, VIP toggle) bumps its entity's token BEFORE its first
   await; after EVERY subsequent await in that action -- both the success
   continuation and the failure/rollback branch -- the action re-checks its
   captured token against the entity's CURRENT token and skips all further
   state writes (cache mutation, DOM update, unread-count adjustment,
   offerUndo) if a newer action on the same entity started meanwhile. This
   is what stops a held/delayed response from a stale first action (e.g. a
   rapid rate-1-then-2 where 1's response is held and lands AFTER 2's
   completes) from clobbering the cache/UI/undo-slot a newer, already-
   resolved action already established. */
const actionTokens = new Map(); // entity key -> monotonic counter

function articleTokenKey(articleId) {
    return `article:${Number(articleId)}`;
}

function sourceTokenKey(sourceId) {
    return `source:${Number(sourceId)}`;
}

function bumpActionToken(key) {
    const next = (actionTokens.get(key) || 0) + 1;
    actionTokens.set(key, next);
    return next;
}

function isStaleActionToken(key, token) {
    return actionTokens.get(key) !== token;
}

function renderArticle(a, showScore) {
    const classes = ["article-card"];
    if (a.is_read) classes.push("is-read");
    if (a.is_vip) classes.push("is-vip");
    if (a.ai_tier) classes.push(`tier-${a.ai_tier}`);

    // Snoozed (M3.2): only ever present in the payload when the Snoozed
    // toggle asked for include_snoozed=true (the default fetch hides these
    // articles server-side entirely). A snoozed_until in the past means the
    // snooze already expired — treat that as a normal, non-dimmed card
    // (mirrors the server's own auto-reappear semantics in
    // build_article_filters()).
    const isSnoozed = isCurrentlySnoozed(a);
    if (isSnoozed) classes.push("is-snoozed");

    const date = formatDate(a.published_at || a.ingested_at);
    const summary = a.summary || "";
    const tags = (a.tags || [])
        .map((t) => `<span class="tag-chip clickable-tag" data-tag="${esc(t)}">${esc(t)}</span>`)
        .join("");

    const ratingMap = { "-1": "dislike", 1: "like", 2: "love" };
    const activeRating = ratingMap[String(a.rating)] || "";

    const sourceType = a.source_type || "web";
    const sourceTypeLabel = sourceType === "email" ? "email" : sourceType === "rss" ? "rss" : "saved";
    const sourceTypePill = `<span class="pill source-type-pill source-type-${sourceType} clickable-tag" data-tag="${esc(sourceTypeLabel)}">${sourceTypeLabel}</span>`;

    const tierBadge = a.ai_tier === "must-read"
        ? '<span class="pill pill-tier tier-badge tier-badge-must-read">Must Read</span>'
        : a.ai_tier === "summary-enough"
        ? '<span class="pill pill-tier tier-badge tier-badge-summary-enough">Summary</span>'
        : "";

    const isSelectedForDelete = selectedForDelete.has(Number(a.id));
    const checked = isSelectedForDelete ? "checked" : "";
    if (isSelectedForDelete) classes.push("bulk-selected");

    // Wake-time chip (M3.2) — only rendered while actively snoozed. Short
    // date via core's formatDate(), same as every other date shown in this
    // card; esc()'d even though formatDate() only ever returns a fixed
    // "Mon D[, YYYY]" shape, per the "esc() every new server-derived string"
    // convention.
    const snoozedChip = isSnoozed
        ? `<div class="snoozed-chip">
            <span class="snoozed-chip-label">Snoozed until ${esc(formatDate(a.snoozed_until.replace(" ", "T")))}</span>
            <button class="wake-now-btn" data-article-id="${a.id}">Wake now</button>
        </div>`
        : "";

    return `
    <article class="${classes.join(" ")}" data-id="${a.id}">
        <span class="swipe-hint-icon swipe-hint-right" aria-hidden="true">${icon("archive", { size: 22 })}</span>
        <span class="swipe-hint-icon swipe-hint-left" aria-hidden="true">${icon("clock", { size: 22 })}</span>
        <input type="checkbox" class="bulk-select-checkbox" data-id="${a.id}" title="Select for bulk delete" ${checked}>
        <div class="article-main">
            <div class="article-meta">
                ${tierBadge}
                ${sourceTypePill}
                <span class="source-name">${esc(a.source_name || a.domain || "unknown")}</span>
                <span class="vip-star ${a.is_vip ? "active" : ""}"
                      data-source-id="${a.source_id}"
                      title="Toggle VIP">${icon("star", { size: 12, cls: "vip-star-icon" })}</span>
                <span class="meta-sep">&middot;</span>
                <span>${date}</span>
                <span class="meta-sep">&middot;</span>
                <span>${a.reading_time_min || "?"} min</span>
                ${showScore && a.similarity_score ? `<span class="meta-sep">&middot;</span><span class="similarity-badge">${Math.round(a.similarity_score * 100)}% match</span>` : ""}
            </div>
            <h2 class="article-title">
                <a href="/articles/${a.id}" data-id="${a.id}">${esc(a.title)}</a>
            </h2>
            ${summary ? `<p class="article-summary"><strong>TL;DR</strong> &ndash; <em>${esc(summary)}</em></p>` : ""}
            ${tags ? `<div class="article-tags">${tags}</div>` : ""}
            ${snoozedChip}
        </div>
        <div class="article-actions">
            <div class="card-menu">
                <button class="card-menu-btn icon-btn" data-article-id="${a.id}"
                        aria-haspopup="true" aria-expanded="false"
                        title="More actions">${icon("kebab", { size: 15 })}</button>
                <div class="card-menu-dropdown" hidden>
                    <button class="card-menu-item card-menu-snooze" data-action="snooze" data-article-id="${a.id}">Snooze&hellip;</button>
                </div>
            </div>
            <button class="rate-btn icon-btn love ${activeRating === "love" ? "active" : ""}"
                    data-article-id="${a.id}" data-rating="2"
                    title="Love">${icon("heart", { size: 15 })}</button>
            <button class="rate-btn icon-btn like ${activeRating === "like" ? "active" : ""}"
                    data-article-id="${a.id}" data-rating="1"
                    title="Like">${icon("thumb-up", { size: 15 })}</button>
            <button class="rate-btn icon-btn dislike ${activeRating === "dislike" ? "active" : ""}"
                    data-article-id="${a.id}" data-rating="-1"
                    title="Dislike">${icon("thumb-down", { size: 15 })}</button>
        </div>
    </article>`;
}

function attachListeners() {
    // Bulk-select checkboxes
    document.querySelectorAll(".bulk-select-checkbox").forEach((cb) => {
        cb.addEventListener("click", (e) => e.stopPropagation());
        cb.addEventListener("change", () => {
            const id = Number(cb.dataset.id);
            const card = cb.closest(".article-card");
            if (cb.checked) {
                selectedForDelete.add(id);
                if (card) card.classList.add("bulk-selected");
            } else {
                selectedForDelete.delete(id);
                if (card) card.classList.remove("bulk-selected");
            }
            updateBulkDeleteToolbar();
        });
    });

    // Rating buttons
    document.querySelectorAll(".rate-btn").forEach((btn) => {
        btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            const articleId = btn.dataset.articleId;
            const rating = parseInt(btn.dataset.rating, 10);

            try {
                const res = await fetch(`/api/articles/${articleId}/rate`, {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ rating }),
                });
                const json = await res.json();
                if (json.success) {
                    // Keep cachedArticles in sync (Finding 4, M3.2 final
                    // review): this mouse-click handler never touched the
                    // cache, so a subsequent keyboard undo capture
                    // (rateSelected's `priorRating`) trusted a stale value
                    // that no longer matched the server. A miss here (the
                    // card isn't in the currently-cached page — e.g. a live
                    // search result, Finding 1) is a harmless no-op.
                    const article = cachedArticles.find(
                        (a) => Number(a.id) === Number(articleId)
                    );
                    if (article) article.rating = rating;
                    // Update active state within this card
                    const card = btn.closest(".article-card");
                    card.querySelectorAll(".rate-btn").forEach((b) =>
                        b.classList.remove("active")
                    );
                    btn.classList.add("active");
                }
            } catch (err) {
                console.error("Rating failed:", err);
            }
        });
    });

    // Card overflow menu (M3.2) — "⋯" button toggles a per-card dropdown;
    // its first (and for T1, only) item opens the snooze preset sheet. T3
    // extends this same dropdown with swipe-triage's other actions.
    document.querySelectorAll(".card-menu-btn").forEach((btn) => {
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            const dropdown = btn.nextElementSibling;
            const wasOpen = dropdown && !dropdown.hidden;
            closeAllCardMenus();
            if (dropdown && !wasOpen) {
                dropdown.hidden = false;
                btn.setAttribute("aria-expanded", "true");
            }
        });
    });

    document.querySelectorAll('.card-menu-item[data-action="snooze"]').forEach((item) => {
        item.addEventListener("click", (e) => {
            e.stopPropagation();
            closeAllCardMenus();
            openSnoozeSheet(Number(item.dataset.articleId));
        });
    });

    // Wake now (M3.2) — clears snoozed_until immediately via PATCH {until: null}.
    document.querySelectorAll(".wake-now-btn").forEach((btn) => {
        btn.addEventListener("click", async (e) => {
            e.stopPropagation();
            const articleId = btn.dataset.articleId;
            // Wake-now only ever renders on a currently-snoozed card (see
            // renderArticle's snoozedChip), so this article was excluded
            // from the unread count while snoozed (countsAsUnread(article)
            // is guaranteed false beforehand); waking an unread one brings
            // it back in (M3.2 Task 4 pill live-update). Equivalent to
            // countsAsUnread() gated on that guarantee -- audited per
            // Finding 1, left as `wasUnread` since it was already correct.
            const article = cachedArticles.find((a) => Number(a.id) === Number(articleId));
            const wasUnread = !!(article && !article.is_read);

            // Sequence token (Finding 2, M3.2 final review) -- same shape as
            // performSnooze/performArchive/rateSelected: a newer action on
            // this article (e.g. a rapid re-snooze or wake-now double click)
            // must invalidate this continuation.
            const key = articleTokenKey(articleId);
            const token = bumpActionToken(key);

            let ok = false;
            try {
                const res = await fetch(`/api/articles/${articleId}/snooze`, {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ until: null }),
                });
                const json = await res.json();
                ok = !!json.success;
            } catch (err) {
                console.error("Wake now failed:", err);
                ok = false;
            }

            if (isStaleActionToken(key, token)) return; // superseded by a newer action

            if (ok) {
                if (wasUnread) adjustUnreadCount(1);
                showToast("Article woken up", "success");
                await loadInbox();
            } else {
                showToast("Failed to wake article", "error");
            }
        });
    });

    // VIP star toggle
    document.querySelectorAll(".vip-star").forEach((star) => {
        star.addEventListener("click", async (e) => {
            e.stopPropagation();
            const sourceId = star.dataset.sourceId;

            try {
                const res = await fetch(`/api/sources/${sourceId}/vip`, {
                    method: "PATCH",
                });
                const json = await res.json();
                if (json.success) {
                    // Reload to reflect VIP reordering
                    loadInbox();
                }
            } catch (err) {
                console.error("VIP toggle failed:", err);
            }
        });
    });

    // Tag click — search by tag
    document.querySelectorAll(".clickable-tag").forEach((tag) => {
        tag.addEventListener("click", (e) => {
            e.stopPropagation();
            const q = tag.dataset.tag;
            const input = document.getElementById("search-input");
            if (input) {
                input.value = q;
                document.getElementById("search-clear").style.display = "block";
                runSearch(q);
            }
        });
    });

    // Article title click — mark as read
    document.querySelectorAll(".article-title a").forEach((link) => {
        link.addEventListener("click", async (e) => {
            e.preventDefault();
            const articleId = link.dataset.id;

            try {
                await fetch(`/api/articles/${articleId}/read`, {
                    method: "PATCH",
                });
            } catch (err) {
                console.error("Mark-read failed:", err);
            }

            window.location.href = link.href;
        });
    });
}

/* ---- Card overflow menu + snooze (M3.2 Task 1) ---- */

function closeAllCardMenus() {
    document.querySelectorAll(".card-menu-dropdown").forEach((d) => {
        d.hidden = true;
    });
    document.querySelectorAll(".card-menu-btn").forEach((b) => {
        b.setAttribute("aria-expanded", "false");
    });
}

const SNOOZE_PRESET_LABELS = {
    tonight: "Tonight",
    tomorrow: "Tomorrow",
    weekend: "Weekend",
    next_week: "Next week",
};

function openSnoozeSheet(articleId) {
    closeSnoozeSheet();

    const overlay = document.createElement("div");
    overlay.id = "snooze-sheet-overlay";
    overlay.className = "export-overlay";
    overlay.innerHTML =
        '<div class="export-dialog snooze-sheet">' +
            "<h3>Snooze until&hellip;</h3>" +
            '<div class="snooze-preset-grid">' +
                Object.entries(SNOOZE_PRESET_LABELS).map(([preset, label]) =>
                    `<button class="snooze-preset-btn" data-preset="${preset}">${esc(label)}</button>`
                ).join("") +
            "</div>" +
            '<div class="export-dialog-actions">' +
                '<button class="export-cancel-btn" id="snooze-sheet-cancel">Cancel</button>' +
            "</div>" +
        "</div>";
    document.body.appendChild(overlay);

    function close() {
        overlay.remove();
    }

    document.getElementById("snooze-sheet-cancel").addEventListener("click", close);
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) close();
    });
    overlay.querySelectorAll(".snooze-preset-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
            close();
            await performSnooze(articleId, btn.dataset.preset);
        });
    });
}

function closeSnoozeSheet() {
    document.getElementById("snooze-sheet-overlay")?.remove();
}

async function performSnooze(articleId, preset) {
    // Prior value captured BEFORE the action (from cachedArticles) so undo
    // can restore it: re-snoozing an already-snoozed article (visible via
    // the Snoozed toggle) restores its previous future wake time; snoozing
    // a normal article restores "not snoozed". A prior timestamp already in
    // the past is treated as not-snoozed (the server 400s past `until`
    // values, and an expired snooze is semantically awake anyway).
    const prior = cachedArticles.find((a) => Number(a.id) === Number(articleId));
    const priorUntil = prior ? prior.snoozed_until : null;
    const priorStillFuture = isCurrentlySnoozed(prior);

    // Live pill update (M3.2 Task 4): snoozing only changes the unread
    // count when it's a genuine visible-unread -> hidden-snoozed
    // transition. An article that was ALREADY snoozed (re-snoozed to a new
    // preset via the Snoozed toggle) was already excluded from the count,
    // so re-snoozing it is a no-op for this counter. Equivalent to
    // countsAsUnread(prior) -- spelled out via priorWasUnread/
    // priorStillFuture (rather than one countsAsUnread() call) since
    // priorStillFuture is also needed below to pick the undo restore target.
    const priorWasUnread = !!(prior && !prior.is_read);
    const leavesUnreadCount = priorWasUnread && !priorStillFuture;

    // Sequence token (Finding 2, M3.2 final review) -- bumped BEFORE the
    // await so a newer action on this same article invalidates this one's
    // eventual continuation. See the token helpers' header comment.
    const key = articleTokenKey(articleId);
    const token = bumpActionToken(key);

    let json = null;
    let ok = false;
    try {
        const res = await fetch(`/api/articles/${articleId}/snooze`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ preset }),
        });
        json = await res.json();
        ok = !!json.success;
    } catch (err) {
        console.error("Snooze failed:", err);
        ok = false;
    }

    if (isStaleActionToken(key, token)) return; // a newer action on this article now owns state

    if (!ok) {
        showToast("Failed to snooze article", "error");
        return;
    }

    if (leavesUnreadCount) adjustUnreadCount(-1);

    if (searchActive) {
        // Finding 1 (M3.2 final review): a live search's results are NOT
        // cachedArticles (runSearch() renders straight from the API
        // response without populating it) -- filtering/re-rendering
        // cachedArticles here would replace the visible search results with
        // a stale inbox snapshot mid-search. Remove just this card in
        // place; the rest of the search results stay exactly as rendered.
        document.querySelector(`.article-card[data-id="${Number(articleId)}"]`)?.remove();
    } else if (showSnoozed) {
        // Toggle is on — re-fetch so the card re-renders in place with
        // the wake-time chip rather than disappearing.
        await loadInbox();
    } else {
        // Default view: the card simply leaves the list.
        cachedArticles = cachedArticles.filter((a) => Number(a.id) !== Number(articleId));
        renderArticleList(cachedArticles);
        updateToolbar(cachedArticles);
    }

    const label = `Snoozed until ${formatDate((json.data.snoozed_until || "").replace(" ", "T"))}`;

    if (!prior) {
        // Fabricated-prior guard (Finding 1): `prior` missing means this
        // card was never in cachedArticles (a search hit outside the cached
        // page) -- there is no real prior state to restore, so offering
        // undo would silently un-snooze or pick the wrong restore target.
        // The (already-committed, correct) snooze stands; just no Undo
        // button.
        showToast(label, "success");
        return;
    }

    // The undo toast replaces T1's plain success toast — same message,
    // now with the Undo affordance (single toast slot either way).
    offerUndo(label, async () => {
        await fetch(`/api/articles/${articleId}/snooze`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ until: priorStillFuture ? priorUntil : null }),
        });
        if (isStaleActionToken(key, token)) return;
        if (leavesUnreadCount) adjustUnreadCount(1);
        await loadInbox();
    });
}

function toggleSnoozed() {
    showSnoozed = !showSnoozed;
    loadInbox();
}

/* ---- Library view (owner UX wave 1) ----
   Flip between the unread-first triage inbox and the whole-collection Library
   view. Resets to page 1 and reloads; syncURLWithFilters() (called from
   loadInbox's happy path) writes/clears ?view=library so the state is
   linkable and survives reload. `force` lets the inbox-zero "Browse your
   library" affordance enter Library unconditionally rather than toggling. */
function setLibraryView(on) {
    if (libraryView === on) {
        // Already in the requested mode — still reload so a stale zero-state
        // or filtered list refreshes to the right view.
        loadInbox();
        return;
    }
    libraryView = on;
    currentPage = 1;
    if (!libraryView) {
        // Leaving Library: drop the ?view=library param eagerly (loadInbox's
        // syncURLWithFilters would too, but only on its happy path).
        window.history.replaceState({}, "", "/inbox");
    }
    loadInbox();
}

function toggleLibrary() {
    setLibraryView(!libraryView);
}

// Registered once (not per render, unlike the per-card listeners in
// attachListeners()) so clicking anywhere outside an open card menu closes
// it. Card-menu-btn/item clicks stopPropagation() so they never reach here.
function setupCardMenuOutsideClick() {
    document.addEventListener("click", () => closeAllCardMenus());
}

/* ---- Undo binder (M3.2 Task 3) ----
   DOM/timer layer over js/undo.js's pure single-slot manager: an undoable
   triage action (swipe-archive, snooze, keyboard 1/2/3 rate, keyboard s
   VIP toggle) shows a toast with an Undo button for UNDO_WINDOW_MS; the
   `u` key or the button runs the entry's undo callback. A second action
   within the window displaces (finalizes) the first — all actions here are
   already committed server-side at push time, so finalizing needs no
   server work, the binder just drops the old toast. x delete and bulk
   delete deliberately KEEP their confirm dialogs and get NO undo (deletion
   is irreversible across all four stores — the dialog is the safety). */

const UNDO_WINDOW_MS = 5000;
let undoMgr = createUndoManager();
let undoTimer = null;

function dismissUndoToast() {
    document.getElementById("undo-toast")?.remove();
}

function renderUndoToast(label) {
    // Same single-toast-at-a-time posture as core.js's showToast() — remove
    // whatever toast is showing (plain or undo) before rendering this one.
    document.querySelector(".settings-toast")?.remove();

    const toast = document.createElement("div");
    toast.id = "undo-toast";
    toast.className = "settings-toast settings-toast-info undo-toast";

    const text = document.createElement("span");
    text.className = "undo-toast-label";
    text.textContent = label; // textContent sink — no esc() needed

    const btn = document.createElement("button");
    btn.className = "undo-toast-btn";
    btn.textContent = "Undo";
    btn.addEventListener("click", (e) => {
        e.stopPropagation();
        triggerUndo();
    });

    toast.appendChild(text);
    toast.appendChild(btn);
    document.body.appendChild(toast);
    setTimeout(() => toast.classList.add("show"), 10);
}

function offerUndo(label, undoFn) {
    const { mgr } = pushUndoable(undoMgr, { label, undo: undoFn });
    // The displaced (finalized) entry needs no cleanup — see the section
    // comment above — so `finalized` is intentionally unused here.
    undoMgr = mgr;
    clearTimeout(undoTimer);
    renderUndoToast(label);
    undoTimer = setTimeout(() => {
        const { mgr: next } = clearUndo(undoMgr);
        undoMgr = next;
        dismissUndoToast();
    }, UNDO_WINDOW_MS);
}

async function triggerUndo() {
    const { entry, mgr } = takeUndo(undoMgr);
    undoMgr = mgr;
    clearTimeout(undoTimer);
    dismissUndoToast();
    if (!entry) return;
    try {
        await entry.undo();
    } catch (err) {
        console.error("Undo failed:", err);
        showToast("Undo failed", "error");
    }
}

/* ---- Swipe triage (M3.2 Task 3) ----
   Delegated pointer handlers on #article-list drive js/swipe.js's pure
   state machine. Right swipe past the threshold archives (mark-read +
   undo), left swipe opens the snooze preset sheet. Scroll protection is
   double: the state machine's permanent vertical lock (a scroll gesture
   can never become a swipe) AND `touch-action: pan-y` on the cards (the
   browser keeps native vertical scrolling without waiting on JS). */

// Live MediaQueryList — `.matches` reflects OS-setting changes mid-session.
// Under reduced motion the gesture still functions (release still acts);
// the card just doesn't visually track the finger, and the snap-back
// transition is disabled in CSS by the matching media query.
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

// Interactive descendants that own their own pointer/click behavior — a
// pointerdown on any of these never engages the swipe gesture (pointer
// capture would otherwise retarget pointerup to the card and break the
// child's click event).
const SWIPE_IGNORE_SELECTOR =
    "button, a, input, select, .vip-star, .clickable-tag, .card-menu, .snoozed-chip";

let swipeState = createSwipeState();
let swipeCard = null; // card element owning the in-flight gesture
let swipePointerId = null;
let swipeCardWidth = 0;

function setupSwipe() {
    const listEl = document.getElementById("article-list");
    if (!listEl) return;
    // Delegated on the persistent list container — card re-renders
    // (innerHTML replacement) never orphan these handlers.
    listEl.addEventListener("pointerdown", onSwipePointerDown);
    listEl.addEventListener("pointermove", onSwipePointerMove);
    listEl.addEventListener("pointerup", onSwipePointerUp);
    listEl.addEventListener("pointercancel", onSwipePointerCancel);
}

function onSwipePointerDown(e) {
    if (swipeCard) return; // one gesture at a time
    if (e.button !== 0) return; // primary button/touch only
    if (e.target.closest(SWIPE_IGNORE_SELECTOR)) return;
    const card = e.target.closest(".article-card");
    if (!card) return;

    // GUARD (T2 review edge): a 0/NaN cardWidth would make the 35%
    // act-threshold meaningless (0.35 * 0 = 0 → every release acts), so a
    // card that measures empty does NOT engage the gesture at all.
    const width = card.getBoundingClientRect().width;
    if (!Number.isFinite(width) || width <= 0) return;

    // Pointer capture keeps move/up delivery flowing (retargeted to the
    // card, so it still bubbles through #article-list's delegated
    // listeners) even when the pointer leaves the card mid-swipe. It can
    // throw for a pointerId with no active pointer (e.g. synthesized test
    // events) — non-fatal, the delegated listeners still see events
    // dispatched at the card/list themselves.
    try {
        card.setPointerCapture(e.pointerId);
    } catch (err) { /* enhancement only — see above */ }

    swipeCard = card;
    swipePointerId = e.pointerId;
    swipeCardWidth = width;
    const r = swipeEvent(swipeState, {
        type: "down", x: e.clientX, y: e.clientY, t: e.timeStamp, cardWidth: width,
    });
    swipeState = r.state;
}

function onSwipePointerMove(e) {
    if (!swipeCard || e.pointerId !== swipePointerId) return;
    const r = swipeEvent(swipeState, {
        type: "move", x: e.clientX, y: e.clientY, t: e.timeStamp,
        cardWidth: swipeCardWidth,
    });
    swipeState = r.state;
    if (r.transform) {
        // Horizontal lock engaged — the gesture owns this pointer now.
        // .swiping is only added here (not on pointerdown) so plain taps
        // and vertical scrolls never toggle card classes at all.
        swipeCard.classList.add("swiping");
        swipeCard.classList.toggle("swipe-right-hint", r.transform.dx > 0);
        swipeCard.classList.toggle("swipe-left-hint", r.transform.dx < 0);
        if (!reducedMotion.matches) {
            swipeCard.style.transform = `translateX(${r.transform.dx}px)`;
        }
    }
}

function onSwipePointerUp(e) {
    if (!swipeCard || e.pointerId !== swipePointerId) return;
    const r = swipeEvent(swipeState, {
        type: "up", x: e.clientX, y: e.clientY, t: e.timeStamp,
        cardWidth: swipeCardWidth,
    });
    swipeState = r.state;
    resolveSwipe(r.action);
}

function onSwipePointerCancel(e) {
    if (!swipeCard || e.pointerId !== swipePointerId) return;
    const r = swipeEvent(swipeState, {
        type: "cancel", x: e.clientX, y: e.clientY, t: e.timeStamp,
        cardWidth: swipeCardWidth,
    });
    swipeState = r.state;
    resolveSwipe(r.action); // always "cancelled" per the state machine
}

function resolveSwipe(action) {
    const card = swipeCard;
    swipeCard = null;
    swipePointerId = null;
    swipeCardWidth = 0;
    if (!card) return;

    const articleId = Number(card.dataset.id);
    card.classList.remove("swiping", "swipe-right-hint", "swipe-left-hint");

    if (action === "archive") {
        card.style.transform = "";
        performArchive(articleId);
        return;
    }
    if (action === "snooze-sheet") {
        card.style.transform = "";
        openSnoozeSheet(articleId);
        return;
    }

    // Cancelled: snap back. The transition class animates transform back
    // to rest; under prefers-reduced-motion the media query disables the
    // transition and this is an instant reset.
    if (card.style.transform) {
        card.classList.add("swipe-snap-back");
        card.style.transform = "";
        setTimeout(() => card.classList.remove("swipe-snap-back"), 250);
    }
}

async function performArchive(articleId) {
    const id = Number(articleId);
    const article = cachedArticles.find((a) => Number(a.id) === id);
    const wasRead = !!(article && article.is_read);
    // Captured HERE, before the optimistic is_read mutation just below and
    // before the await -- this is the last point the article's true
    // pre-archive state (including snoozed_until) is known. The undo
    // closure below reuses this exact boolean rather than recomputing it
    // (Finding 1, M3.2 Task 4 review): a snooze can expire in the window
    // between archive and undo, and recomputing at undo time would then
    // silently pick up the wrong answer.
    const wasCountedUnread = countsAsUnread(article);

    // Sequence token (Finding 2, M3.2 final review) -- bumped BEFORE the
    // optimistic mutation so a newer action on this same article
    // invalidates this one's eventual continuation. See the token helpers'
    // header comment.
    const key = articleTokenKey(id);
    const token = bumpActionToken(key);

    // Same reorder as rateSelected's fix: mutate the cached read state
    // optimistically before the await so a rapid second action on this
    // article reads this action's post-state as its "prior", not the stale
    // pre-action one. Rolled back below on PATCH failure.
    if (article) article.is_read = true;

    let ok = false;
    try {
        const res = await fetch(`/api/articles/${id}/read`, { method: "PATCH" });
        const json = await res.json();
        ok = !!json.success;
    } catch (err) {
        console.error("Archive failed:", err);
        ok = false;
    }

    if (isStaleActionToken(key, token)) return; // a newer action on this article now owns state

    if (!ok) {
        if (article) article.is_read = wasRead;
        showToast("Failed to archive article", "error");
        return;
    }

    // Live pill update (M3.2 Task 4): archiving only changes the unread
    // count when the article was actually counted as unread beforehand --
    // an already-read article, OR a snoozed-unread one (already excluded
    // from the count server-side -- Finding 1 fix), is a no-op here.
    // MUST run BEFORE updateToolbar() below (M3.2 final-review regression
    // caught in testing): adjustUnreadCount() only updates the shared
    // counter + the sidebar badge, it does NOT itself re-render this page's
    // #triage-pill -- that happens inside updateToolbar()'s
    // refreshTriageUI() call, which reads whatever the counter says AT THAT
    // MOMENT. Decrementing after updateToolbar() would render the pill with
    // the stale (pre-decrement) count and never repaint it again.
    if (wasCountedUnread) adjustUnreadCount(-1);

    if (searchActive) {
        // Finding 1 (M3.2 final review): a live search's results are NOT
        // cachedArticles (runSearch() renders straight from the API
        // response without populating it) -- filtering/re-rendering
        // cachedArticles here would replace the visible search results with
        // a stale inbox snapshot mid-search. Remove just this card in
        // place; the rest of the search results stay exactly as rendered.
        document.querySelector(`.article-card[data-id="${id}"]`)?.remove();
    } else {
        // Triage semantics: the card leaves the current view immediately
        // (same posture as performSnooze's default-view path). Archive IS
        // mark-read — there is no separate archived state — so a later
        // full reload may legitimately show the article again as a read
        // row.
        cachedArticles = cachedArticles.filter((a) => Number(a.id) !== id);
        renderArticleList(cachedArticles);
        updateToolbar(cachedArticles);
    }

    if (!article) {
        // Fabricated-prior guard (Finding 1): no `article` means this card
        // was never in cachedArticles (a search hit outside the cached
        // page) -- `wasRead`/`wasCountedUnread` above are computed from an
        // undefined article and are NOT this article's true prior state.
        // Offering undo on a fabricated prior would silently un-read an
        // already-read article, or mis-adjust a count that was never
        // really decremented. The (already-committed, correct) archive
        // stands; just no Undo button.
        showToast("Archived", "success");
        return;
    }

    offerUndo("Archived", async () => {
        // Restore the pre-archive state server-side, not just visually:
        // PATCH back to unread ONLY when the article wasn't already read
        // before the swipe (un-reading an article that was read before the
        // gesture would not be a restore). opened_count and reading stats
        // are monotonic by design and are not rolled back.
        //
        // Unread-first inbox note (owner UX wave 1): un-reading here makes the
        // article match the default is_read=false fetch again, so the trailing
        // loadInbox() below re-surfaces it VISIBLY — the web analogue of iOS's
        // "keep the row visible after undo". The one case it stays absent is
        // wasRead=true (archiving an already-read row, only reachable in
        // Library view): nothing changed, so nothing should reappear — the
        // correct no-op, not a lost row.
        if (!wasRead) {
            await fetch(`/api/articles/${id}/read`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ is_read: false }),
            });
        }
        if (isStaleActionToken(key, token)) return;
        // Mirrors wasCountedUnread captured above at archive time, NOT a
        // fresh countsAsUnread() recompute here and NOT nested under the
        // `!wasRead` PATCH guard above (Finding 1): those are two different
        // questions. A snoozed-unread article never decremented the count
        // on archive, so its undo must not increment it either, even though
        // `!wasRead` is true and the is_read PATCH above still runs.
        if (wasCountedUnread) adjustUnreadCount(1);
        await loadInbox();
    });
}

/* ---- Sort ---- */

function setupSort() {
    const sortSelect = document.getElementById("sort-select");
    if (!sortSelect) return;

    sortSelect.addEventListener("change", () => {
        currentSort = sortSelect.value;
        currentPage = 1;
        if (perPage > 0) {
            // Server-side sort with pagination — must re-fetch
            loadInbox();
        } else if (cachedArticles.length) {
            renderSortedInbox();
        }
    });
}

/* ---- Search ---- */

function setupSearch() {
    const input = document.getElementById("search-input");
    const clearBtn = document.getElementById("search-clear");
    if (!input) return;

    // Check for ?q= URL param (e.g. from reader tag click)
    const urlParams = new URLSearchParams(window.location.search);
    const initialQuery = urlParams.get("q");
    if (initialQuery) {
        input.value = initialQuery;
        clearBtn.style.display = "block";
        runSearch(initialQuery);
    }

    let debounceTimer = null;

    input.addEventListener("input", () => {
        const q = input.value.trim();
        clearBtn.style.display = q ? "block" : "none";

        clearTimeout(debounceTimer);
        if (!q) {
            exitSearch();
            return;
        }
        debounceTimer = setTimeout(() => runSearch(q), 300);
    });

    clearBtn.addEventListener("click", () => {
        input.value = "";
        clearBtn.style.display = "none";
        exitSearch();
        input.focus();
    });
}

async function runSearch(query) {
    const listEl = document.getElementById("article-list");
    const emptyEl = document.getElementById("empty-state");

    // A live search is never the "default view" (M3.2 Task 4) -- hide the
    // inbox-zero celebratory state immediately, before the request even
    // resolves, rather than leaving it visible behind/above search results.
    searchActive = true;
    updateInboxZeroState();

    try {
        const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
        const json = await res.json();

        if (!json.success || !json.data.length) {
            listEl.innerHTML = `<div class="search-results-header">
                <h3>No results for "${esc(query)}"</h3>
            </div>`;
            emptyEl.style.display = "none";
            return;
        }

        emptyEl.style.display = "none";
        const header = `<div class="search-results-header">
            <h3>Results for "${esc(query)}"</h3>
            <span class="search-results-count">${json.data.length} found</span>
        </div>`;
        listEl.innerHTML = header + json.data.map((a) => renderArticle(a, true)).join("");
        attachListeners();
    } catch (err) {
        console.error("Search failed:", err);
    }
}

function exitSearch() {
    searchActive = false;
    currentPage = 1;
    window.history.replaceState({}, "", "/inbox");
    loadInbox();
}

/* ---- Triage progress pill + inbox-zero state (M3.2 Task 4) ----

   The pill's count is deliberately NOT derived from `cachedArticles` (which
   only reflects whatever page/filter/sort is currently loaded, and is
   sometimes trimmed client-side without a re-fetch -- see performArchive's
   header comment). It mirrors the sidebar's global unread badge exactly via
   sidebar.js's shared getUnreadCount()/adjustUnreadCount() state, so the
   two can never drift: every triage call site below adjusts that ONE
   counter, and both this pill and the sidebar badge just render whatever it
   currently says.

   The inbox-zero celebratory state answers "has the user triaged every
   unread article away, in a view that has genuinely seen real data this
   session" -- gated on `libraryEverHadArticles` (sticky once true) rather
   than `cachedArticles.length`, deliberately: archive/snooze are mark-read/
   hide, not delete, so a real user's default view usually still has READ
   articles sitting in it even after the last unread one is triaged away,
   and the banner is meant to show alongside them (a "you're caught up"
   banner above older history), not only in the edge case where the loaded
   page happens to be visually empty too. It never fires from loadInbox()'s
   "server returned zero rows" branch (that's the pre-existing "no articles
   yet" / filtered-empty handling, untouched by this task, and the one
   place `libraryEverHadArticles` is deliberately never set) -- only once a
   loadInbox() fetch has proven real data exists, refreshed on every
   updateToolbar() call (loadInbox's happy path, and performArchive/
   performSnooze/performDelete's local-removal paths). */

function renderTriagePill() {
    const pill = document.getElementById("triage-pill");
    if (!pill) return;
    // Write the counter into a child span so the leading archive icon
    // (static markup in inbox.html) survives every re-render. Falls back to
    // the pill itself if the count span is somehow absent.
    const countEl = pill.querySelector(".triage-pill-count") || pill;
    const count = getUnreadCount();
    if (count === null || count <= 0) {
        pill.style.display = "none";
        countEl.textContent = "";
        return;
    }
    countEl.textContent = `${num(count)} to zero`;
    pill.style.display = "inline-flex";
}

// "Default view" for inbox-zero purposes: no live search, no filter-panel
// filters, and none of the toggles that pull extra (non-default) rows into
// view. Matches the binding spec's "no search/filters active AND the
// default view (not snoozed/archived toggles)" verbatim, plus VIP-only
// (not named in the spec, but the same kind of client-side view filter --
// showing the celebratory banner while the user is looking at a narrowed
// VIP subset would be misleading).
function isDefaultTriageView() {
    return !searchActive
        && Object.keys(activeFilters).length === 0
        && !showArchived && !showSnoozed && !showVIPOnly && !libraryView;
}

function updateInboxZeroState() {
    const zeroEl = document.getElementById("inbox-zero-state");
    if (!zeroEl) return;
    // `libraryEverHadArticles`, not `cachedArticles.length` -- a real user's
    // default view usually still has READ articles sitting in it (archive is
    // mark-read, not a separate hidden state) even after every unread one
    // has been triaged away, and the banner should show alongside them, not
    // only in the edge case where the loaded page happens to be visually
    // empty too. See the flag's own declaration comment for why a
    // server-confirmed empty response never sets it.
    const shouldShow = isDefaultTriageView()
        && libraryEverHadArticles
        && getUnreadCount() === 0;
    zeroEl.style.display = shouldShow ? "block" : "none";
}

function refreshTriageUI() {
    renderTriagePill();
    updateInboxZeroState();
}

/* ---- Tier toolbar (classify button + discard toggle) ---- */

// Writes the classify button's dynamic label into its `.classify-label`
// span so the leading zap icon (static markup in inbox.html) is never wiped
// by a whole-button textContent assignment.
function setClassifyLabel(btn, text) {
    const label = btn && btn.querySelector(".classify-label");
    if (label) label.textContent = text;
    else if (btn) btn.textContent = text;
}

function updateToolbar(articles) {
    const toolbar = document.getElementById("inbox-toolbar");
    const classifyBtn = document.getElementById("classify-btn");
    const discardToggle = document.getElementById("discard-toggle");
    const classifyInfo = document.getElementById("classify-info");
    if (!toolbar) return;

    const discardCount = articles.filter((a) => a.ai_tier === "discard").length;
    const unclassified = articles.filter((a) => !a.ai_tier).length;
    const ratedCount = articles.filter((a) => a.rating !== null).length;

    toolbar.style.display = "flex";

    // Classify button — always visible
    classifyBtn.style.display = "inline-flex";
    if (unclassified > 0) {
        setClassifyLabel(classifyBtn, `Classify inbox (${unclassified})`);
        classifyBtn.classList.remove("classify-btn-secondary");
    } else {
        setClassifyLabel(classifyBtn, "Reclassify");
        classifyBtn.classList.add("classify-btn-secondary");
    }

    // Info text — guide user if not enough ratings
    if (ratedCount < 5) {
        classifyInfo.textContent = `Rate ${5 - ratedCount} more article${5 - ratedCount === 1 ? "" : "s"} to enable classification`;
        classifyInfo.style.display = "inline";
        classifyBtn.disabled = true;
    } else {
        classifyInfo.style.display = "none";
        classifyBtn.disabled = false;
    }

    // Discard toggle
    if (discardCount > 0) {
        discardToggle.style.display = "inline-flex";
        discardToggle.textContent = `Show discarded (${discardCount})`;
    } else {
        discardToggle.style.display = "none";
    }

    // Archived toggle — only show when not already showing archived
    const archivedToggle = document.getElementById("archived-toggle");
    if (archivedToggle) {
        if (showArchived) {
            archivedToggle.style.display = "inline-flex";
            archivedToggle.textContent = "Hide archived";
            archivedToggle.classList.add("active");
        } else {
            // Always show the button so user can toggle
            archivedToggle.style.display = "inline-flex";
            archivedToggle.textContent = "Show archived";
            archivedToggle.classList.remove("active");
        }
    }

    // Snoozed toggle (M3.2) — mirrors the archived toggle exactly (always
    // visible so the user can turn it on, label/active-class flip on state).
    const snoozedToggle = document.getElementById("snoozed-toggle");
    if (snoozedToggle) {
        if (showSnoozed) {
            snoozedToggle.style.display = "inline-flex";
            snoozedToggle.textContent = "Hide snoozed";
            snoozedToggle.classList.add("active");
        } else {
            snoozedToggle.style.display = "inline-flex";
            snoozedToggle.textContent = "Show snoozed";
            snoozedToggle.classList.remove("active");
        }
    }

    // VIP toggle
    const vipToggle = document.getElementById("vip-toggle");
    if (vipToggle) {
        vipToggle.classList.toggle("active", showVIPOnly);
        vipToggle.onclick = toggleVIPOnly;
    }

    // Library toggle (owner UX wave 1) — flips the whole list between the
    // unread-first triage inbox and the whole-collection Library view. Label
    // + active class flip on state, same pattern as the archived/snoozed
    // toggles above.
    const libraryToggle = document.getElementById("library-toggle");
    if (libraryToggle) {
        libraryToggle.textContent = libraryView ? "Back to inbox" : "Library";
        libraryToggle.classList.toggle("active", libraryView);
        libraryToggle.onclick = toggleLibrary;
    }

    // Attach handlers (safe to call multiple times — we replace onclick)
    classifyBtn.onclick = classifyArticles;
    discardToggle.onclick = toggleDiscarded;
    if (archivedToggle) archivedToggle.onclick = toggleArchived;
    if (snoozedToggle) snoozedToggle.onclick = toggleSnoozed;

    // Bulk-delete toolbar button reflects current selection
    updateBulkDeleteToolbar();

    // Triage pill + inbox-zero state (M3.2 Task 4) -- called from every
    // updateToolbar() invocation (loadInbox's happy path, performArchive/
    // performSnooze/performDelete's local-removal paths) so both stay
    // fresh no matter which call site reached here.
    refreshTriageUI();
}

async function classifyArticles() {
    const classifyBtn = document.getElementById("classify-btn");
    const classifyInfo = document.getElementById("classify-info");
    const isReclassify = classifyBtn.classList.contains("classify-btn-secondary");

    classifyBtn.disabled = true;
    setClassifyLabel(classifyBtn, "Classifying...");
    classifyInfo.textContent = "Opus 4.6 is learning your preferences — this may take 30–60s";
    classifyInfo.style.display = "inline";

    try {
        const body = isReclassify ? JSON.stringify({ refresh: true }) : undefined;
        const headers = isReclassify ? { "Content-Type": "application/json" } : {};
        const res = await fetch("/api/classify", { method: "POST", headers, body });
        const json = await res.json();

        if (!json.success) {
            classifyInfo.textContent = json.error || "Classification failed";
            classifyBtn.disabled = false;
            setClassifyLabel(classifyBtn, isReclassify ? "Reclassify" : "Classify inbox");
            return;
        }

        classifyInfo.textContent = `Classified ${json.data.classified_count} articles`;

        // Switch to importance sort and reload
        currentSort = "importance";
        currentPage = 1;
        await loadInbox();
        loadFilters(); // refresh filter counts
    } catch (err) {
        console.error("Classification failed:", err);
        classifyInfo.textContent = "Classification failed — check console";
        classifyBtn.disabled = false;
        setClassifyLabel(classifyBtn, isReclassify ? "Reclassify" : "Classify inbox");
    }
}

function toggleArchived() {
    showArchived = !showArchived;
    loadInbox().then(() => {
        if (!showArchived) return;
        // When showing archived, also force-show discarded so archived+discarded articles appear
        const listEl = document.getElementById("article-list");
        if (listEl) listEl.classList.add("show-discarded");
        const discardToggle = document.getElementById("discard-toggle");
        if (discardToggle) {
            const count = listEl.querySelectorAll(".tier-discard").length;
            discardToggle.textContent = `Hide discarded (${count})`;
            discardToggle.classList.add("active");
        }
    });
}

function toggleDiscarded() {
    const listEl = document.getElementById("article-list");
    const toggle = document.getElementById("discard-toggle");
    if (!listEl || !toggle) return;

    const showing = listEl.classList.toggle("show-discarded");
    toggle.classList.toggle("active", showing);

    // Update label
    const count = listEl.querySelectorAll(".tier-discard").length;
    toggle.textContent = showing
        ? `Hide discarded (${count})`
        : `Show discarded (${count})`;
}

function toggleVIPOnly() {
    showVIPOnly = !showVIPOnly;
    const toggle = document.getElementById("vip-toggle");
    if (toggle) toggle.classList.toggle("active", showVIPOnly);

    if (showVIPOnly) {
        // Client-side filter: show only VIP articles from cached list
        const vipArticles = cachedArticles.filter(a => a.is_vip);
        renderArticleList(vipArticles);
    } else {
        renderArticleList(cachedArticles);
    }

    // VIP-only is a view filter same as search/archived/snoozed (M3.2 Task
    // 4) -- the inbox-zero celebratory state must not show while it's on.
    updateInboxZeroState();
}

/* ---- Filter panel ---- */

function setupFilterPanel() {
    const filterTab = document.getElementById("filter-tab");
    const closeBtn = document.getElementById("filter-panel-close");
    const clearAllBtn = document.getElementById("filter-clear-all");

    function togglePanel() {
        filterPanelOpen = !filterPanelOpen;
        const panel = document.getElementById("filter-panel");
        if (panel) panel.classList.toggle("open", filterPanelOpen);
    }

    if (filterTab) {
        filterTab.addEventListener("click", togglePanel);
    }

    // Phone-only Filters icon-button in the toolbar (the vertical edge tab is
    // desktop/tablet-only; they never both show at once — see the CSS).
    const filterIconBtn = document.getElementById("filter-icon-btn");
    if (filterIconBtn) {
        filterIconBtn.addEventListener("click", togglePanel);
    }

    if (closeBtn) {
        closeBtn.addEventListener("click", () => {
            filterPanelOpen = false;
            const panel = document.getElementById("filter-panel");
            if (panel) panel.classList.remove("open");
        });
    }

    if (clearAllBtn) {
        clearAllBtn.addEventListener("click", () => {
            activeFilters = {};
            currentPage = 1;
            loadInbox();
            if (filterData) renderFilterPanelContent(filterData);
            updateFilterTabIndicator();
        });
    }

    // Save view
    const saveViewBtn = document.getElementById("filter-save-view-btn");
    const saveViewForm = document.getElementById("filter-save-view-form");
    const saveViewInput = document.getElementById("filter-save-view-input");
    const saveViewSubmit = document.getElementById("filter-save-view-submit");
    const saveViewCancel = document.getElementById("filter-save-view-cancel");

    function closeSaveViewForm() {
        if (saveViewForm) saveViewForm.style.display = "none";
        if (saveViewBtn) saveViewBtn.style.display = Object.keys(activeFilters).length ? "" : "none";
    }

    if (saveViewBtn && saveViewForm) {
        saveViewBtn.addEventListener("click", () => {
            saveViewBtn.style.display = "none";
            saveViewForm.style.display = "flex";
            if (saveViewInput) { saveViewInput.value = ""; saveViewInput.focus(); }
        });
    }

    if (saveViewCancel) {
        saveViewCancel.addEventListener("click", closeSaveViewForm);
    }

    async function submitSaveView() {
        const name = saveViewInput ? saveViewInput.value.trim() : "";
        if (!name) return;
        try {
            const res = await fetch("/api/views", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    name,
                    filter_json: JSON.stringify(activeFilters),
                    sort_mode: currentSort,
                }),
            });
            const json = await res.json();
            if (!res.ok || !json.success) throw new Error(json.detail || "Failed to save view");
            closeSaveViewForm();
            // Sidebar (chrome, every page) owns the saved-views list DOM;
            // ask it to reload+re-render itself rather than duplicating that
            // logic here.
            await refreshSidebarViews();
        } catch (e) {
            alert(e.message || "Failed to save view");
        }
    }

    if (saveViewSubmit) saveViewSubmit.addEventListener("click", submitSaveView);
    if (saveViewInput) {
        saveViewInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); submitSaveView(); }
            if (e.key === "Escape") { e.preventDefault(); closeSaveViewForm(); }
        });
    }
}

function updateFilterTabIndicator() {
    const tab = document.getElementById("filter-tab");
    if (!tab) return;
    const hasFilters = Object.keys(activeFilters).length > 0;
    tab.classList.toggle("has-filters", hasFilters);

    const saveViewBtn = document.getElementById("filter-save-view-btn");
    const saveViewForm = document.getElementById("filter-save-view-form");
    const formOpen = !!(saveViewForm && saveViewForm.style.display === "flex");
    if (!hasFilters && formOpen) {
        saveViewForm.style.display = "none";
    }
    if (saveViewBtn && !formOpen) {
        saveViewBtn.style.display = hasFilters ? "" : "none";
    }
}

async function loadFilters() {
    try {
        const res = await fetch("/api/filters");
        const json = await res.json();
        if (json.success) {
            filterData = json.data;
            renderFilterPanelContent(json.data);
        }
    } catch (err) {
        console.error("Failed to load filters:", err);
    }
}

function renderFilterPanelContent(data) {
    const body = document.getElementById("filter-panel-body");
    if (!body) return;

    let html = "";

    // Read status
    html += renderFilterSection("Status", [
        { label: "Unread", value: "false", key: "is_read", count: data.read_status?.unread || 0 },
        { label: "Read", value: "true", key: "is_read", count: data.read_status?.read || 0 },
    ]);

    // AI Tiers
    if (data.tiers?.length) {
        const tierLabels = { "must-read": "Must Read", "summary-enough": "Summary", "discard": "Discard", "unclassified": "Unclassified" };
        const tierItems = data.tiers.map(t => ({
            label: tierLabels[t.name] || t.name,
            value: t.name,
            key: "ai_tier",
            count: t.count,
        }));
        html += renderFilterSection("Tier", tierItems);
    }

    // Ratings
    if (data.ratings?.length) {
        const ratingLabels = { loved: "Loved", liked: "Liked", disliked: "Disliked", unrated: "Unrated" };
        const ratingIcons = {
            loved: icon("heart", { size: 12 }),
            liked: icon("thumb-up", { size: 12 }),
            disliked: icon("thumb-down", { size: 12 }),
        };
        const ratingItems = data.ratings.map(r => ({
            label: ratingLabels[r.name] || r.name,
            iconHtml: ratingIcons[r.name] || "",
            value: r.name,
            key: "rating",
            count: r.count,
        }));
        html += renderFilterSection("Rating", ratingItems);
    }

    // Sources
    if (data.sources?.length) {
        const sourceItems = data.sources.map(s => ({
            label: s.name,
            iconHtml: s.is_vip ? icon("star", { size: 12, cls: "vip-star-icon" }) : "",
            value: String(s.id),
            key: "source_id",
            count: s.count,
        }));
        html += renderFilterSection("Source", sourceItems, true);
    }

    // Tags
    if (data.tags?.length) {
        const tagItems = data.tags.map(t => ({
            label: t.name,
            value: t.name,
            key: "tag",
            count: t.count,
        }));
        html += renderFilterSection("Tag", tagItems, true);
    }

    // Ingestion method
    if (data.ingestion_methods?.length) {
        const methodLabels = { manual: "Manual", extension: "Extension", email: "Email", imap: "IMAP" };
        const methodItems = data.ingestion_methods.map(m => ({
            label: methodLabels[m.name] || m.name,
            value: m.name,
            key: "ingestion_method",
            count: m.count,
        }));
        html += renderFilterSection("Source Type", methodItems);
    }

    // Reading time
    if (data.reading_time) {
        html += renderFilterSection("Reading Time", [
            { label: "Quick (<5 min)", value: "quick", key: "_reading_time", count: data.reading_time.quick },
            { label: "Medium (5-15 min)", value: "medium", key: "_reading_time", count: data.reading_time.medium },
            { label: "Long (>15 min)", value: "long", key: "_reading_time", count: data.reading_time.long },
        ]);
    }

    // Has audio
    if (data.has_audio > 0) {
        html += renderFilterSection("Audio", [
            { label: "Has audio", value: "true", key: "has_audio", count: data.has_audio },
        ]);
    }

    body.innerHTML = html;

    // Wire up filter option clicks
    body.querySelectorAll(".filter-option").forEach(opt => {
        opt.addEventListener("click", () => {
            const key = opt.dataset.key;
            const value = opt.dataset.value;
            toggleFilter(key, value);
        });
    });

    // Show more toggles
    body.querySelectorAll(".filter-show-more").forEach(btn => {
        btn.addEventListener("click", () => {
            const section = btn.closest(".filter-section");
            const scroll = section?.querySelector(".filter-options-scroll");
            if (scroll) {
                scroll.style.maxHeight = scroll.style.maxHeight === "none" ? "200px" : "none";
                btn.textContent = scroll.style.maxHeight === "none" ? "Show less" : "Show more";
            }
        });
    });
}

function renderFilterSection(title, items, scrollable) {
    const isActive = (item) => {
        if (item.key === "_reading_time") {
            // Special handling for reading time compound filter
            if (item.value === "quick") return activeFilters.max_reading_time === "4";
            if (item.value === "medium") return activeFilters.min_reading_time === "5" && activeFilters.max_reading_time === "15";
            if (item.value === "long") return activeFilters.min_reading_time === "16";
            return false;
        }
        return activeFilters[item.key] === item.value;
    };

    const optionsHtml = items.map(item =>
        `<button class="filter-option${isActive(item) ? " active" : ""}"
                data-key="${item.key}" data-value="${esc(item.value)}">
            <span class="filter-option-label">${item.iconHtml || ""}${esc(item.label)}</span>
            <span class="filter-option-count">${item.count}</span>
        </button>`
    ).join("");

    const showMore = scrollable && items.length > 8
        ? '<button class="filter-show-more">Show more</button>'
        : "";

    const wrapClass = scrollable ? "filter-options filter-options-scroll" : "filter-options";

    return `<div class="filter-section">
        <div class="filter-section-title">${title}</div>
        <div class="${wrapClass}">${optionsHtml}</div>
        ${showMore}
    </div>`;
}

function toggleFilter(key, value) {
    if (key === "_reading_time") {
        // Special reading time handling
        const wasActive = (value === "quick" && activeFilters.max_reading_time === "4")
            || (value === "medium" && activeFilters.min_reading_time === "5" && activeFilters.max_reading_time === "15")
            || (value === "long" && activeFilters.min_reading_time === "16");

        delete activeFilters.min_reading_time;
        delete activeFilters.max_reading_time;

        if (!wasActive) {
            if (value === "quick") { activeFilters.max_reading_time = "4"; }
            else if (value === "medium") { activeFilters.min_reading_time = "5"; activeFilters.max_reading_time = "15"; }
            else if (value === "long") { activeFilters.min_reading_time = "16"; }
        }
    } else {
        // Toggle: if same value, remove; otherwise set
        if (activeFilters[key] === value) {
            delete activeFilters[key];
        } else {
            activeFilters[key] = value;
        }
    }

    currentPage = 1;
    loadInbox();
    if (filterData) renderFilterPanelContent(filterData);
    updateFilterTabIndicator();
}

function removeFilter(key) {
    if (key === "_reading_time") {
        delete activeFilters.min_reading_time;
        delete activeFilters.max_reading_time;
    } else {
        delete activeFilters[key];
    }
    currentPage = 1;
    loadInbox();
    if (filterData) renderFilterPanelContent(filterData);
    updateFilterTabIndicator();
}

/* ---- Active filter pills ---- */

function renderActiveFilters() {
    const container = document.getElementById("active-filters");
    if (!container) return;

    const pills = [];
    const labelMap = {
        is_read: v => v === "true" ? "Read" : "Unread",
        is_vip: v => v === "true" ? "VIP" : "Not VIP",
        ai_tier: v => ({ "must-read": "Must Read", "summary-enough": "Summary", "discard": "Discard", "unclassified": "Unclassified" })[v] || v,
        author: v => `Author: ${v}`,
        source_id: v => {
            if (filterData?.sources) {
                const s = filterData.sources.find(s => String(s.id) === v);
                return s ? `Source: ${s.name}` : `Source #${v}`;
            }
            return `Source #${v}`;
        },
        tag: v => `Tag: ${v}`,
        rating: v => ({ loved: "Loved", liked: "Liked", disliked: "Disliked", unrated: "Unrated" })[v] || v,
        ingestion_method: v => ({ manual: "Manual", extension: "Extension", email: "Email", imap: "IMAP" })[v] || v,
        has_audio: () => "Has audio",
    };
    // Leading icons for pills whose glyph moved into the icon set (rating).
    const iconMap = {
        rating: v => ({
            loved: icon("heart", { size: 12 }),
            liked: icon("thumb-up", { size: 12 }),
            disliked: icon("thumb-down", { size: 12 }),
        })[v] || "",
    };

    for (const [key, val] of Object.entries(activeFilters)) {
        if (val === null || val === undefined || val === "") continue;
        // Skip reading time sub-keys, handle as compound
        if (key === "min_reading_time" || key === "max_reading_time") continue;
        const label = labelMap[key] ? labelMap[key](val) : `${key}: ${val}`;
        const leadIcon = iconMap[key] ? iconMap[key](val) : "";
        pills.push(`<span class="filter-pill" data-key="${key}" title="Click to remove">
            ${leadIcon}${esc(label)} <span class="filter-pill-x">${icon("close", { size: 12 })}</span>
        </span>`);
    }

    // Compound reading time pill
    if (activeFilters.min_reading_time || activeFilters.max_reading_time) {
        let label = "Reading time";
        if (activeFilters.max_reading_time === "4") label = "Quick (<5 min)";
        else if (activeFilters.min_reading_time === "5" && activeFilters.max_reading_time === "15") label = "Medium (5-15 min)";
        else if (activeFilters.min_reading_time === "16") label = "Long (>15 min)";
        pills.push(`<span class="filter-pill" data-key="_reading_time" title="Click to remove">
            ${esc(label)} <span class="filter-pill-x">${icon("close", { size: 12 })}</span>
        </span>`);
    }

    if (pills.length) {
        container.innerHTML = pills.join("");
        container.style.display = "flex";
        container.querySelectorAll(".filter-pill").forEach(pill => {
            pill.addEventListener("click", () => removeFilter(pill.dataset.key));
        });
    } else {
        container.style.display = "none";
        container.innerHTML = "";
    }
}

/* ---- Pagination ---- */

function toRoman(num) {
    const vals = [1000, 900, 500, 400, 100, 90, 50, 40, 10, 9, 5, 4, 1];
    const syms = ["M", "CM", "D", "CD", "C", "XC", "L", "XL", "X", "IX", "V", "IV", "I"];
    let result = "";
    for (let i = 0; i < vals.length; i++) {
        while (num >= vals[i]) { result += syms[i]; num -= vals[i]; }
    }
    return result;
}

function renderPagination(pagination) {
    const container = document.getElementById("pagination");
    if (!container) return;

    if (!pagination || pagination.total_pages <= 1) {
        container.style.display = "none";
        return;
    }

    const { page, total_pages, total } = pagination;
    let html = "";

    // Previous
    html += `<button class="page-link page-nav${page <= 1 ? " disabled" : ""}" data-page="${page - 1}" aria-label="Previous page">${icon("chevron-left", { size: 15 })}</button>`;

    // Page numbers (show max 7, with ellipsis)
    const pages = getPageRange(page, total_pages, 7);
    for (const p of pages) {
        if (p === "...") {
            html += `<span class="page-info">…</span>`;
        } else {
            html += `<button class="page-link serif-num${p === page ? " active" : ""}" data-page="${p}">${toRoman(p)}</button>`;
        }
    }

    // Next
    html += `<button class="page-link page-nav${page >= total_pages ? " disabled" : ""}" data-page="${page + 1}" aria-label="Next page">${icon("chevron-right", { size: 15 })}</button>`;

    // Info
    html += `<span class="page-info">${total} articles</span>`;

    container.innerHTML = html;
    container.style.display = "flex";

    // Wire clicks
    container.querySelectorAll(".page-link:not(.disabled)").forEach(link => {
        link.addEventListener("click", () => {
            const p = parseInt(link.dataset.page, 10);
            if (p >= 1 && p <= total_pages) {
                currentPage = p;
                loadInbox();
                window.scrollTo({ top: 0, behavior: "smooth" });
            }
        });
    });
}

function getPageRange(current, total, maxVisible) {
    if (total <= maxVisible) {
        return Array.from({ length: total }, (_, i) => i + 1);
    }

    const pages = [];
    const half = Math.floor(maxVisible / 2);
    let start = Math.max(1, current - half);
    let end = Math.min(total, start + maxVisible - 1);

    if (end - start < maxVisible - 1) {
        start = Math.max(1, end - maxVisible + 1);
    }

    if (start > 1) {
        pages.push(1);
        if (start > 2) pages.push("...");
    }

    for (let i = start; i <= end; i++) {
        pages.push(i);
    }

    if (end < total) {
        if (end < total - 1) pages.push("...");
        pages.push(total);
    }

    return pages;
}

/* ---- Keyboard navigation ---- */

function setupKeyboard() {
    // Only activate on the inbox page (not reader) — inbox.js is only ever
    // loaded from inbox.html, so this guard is defensive parity with the
    // historical app.js rather than a live requirement.
    if (!document.getElementById("article-list")) return;

    document.addEventListener("keydown", handleInboxKeydown);

    // Shortcuts overlay close
    const closeBtn = document.getElementById("shortcuts-close");
    if (closeBtn) {
        closeBtn.addEventListener("click", hideShortcuts);
    }
    const overlay = document.getElementById("shortcuts-overlay");
    if (overlay) {
        overlay.addEventListener("click", (e) => {
            if (e.target === overlay) hideShortcuts();
        });
    }
}

function handleInboxKeydown(e) {
    // Don't capture when typing in inputs
    const tag = document.activeElement.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
        if (e.key === "Escape") {
            document.activeElement.blur();
            e.preventDefault();
        }
        return;
    }

    // Don't capture when save modal is open (except Escape to close)
    const saveOverlay = document.getElementById("save-overlay");
    if (saveOverlay && saveOverlay.style.display !== "none") {
        if (e.key === "Escape") { closeSaveModal(); e.preventDefault(); }
        return;
    }

    // Don't capture when shortcuts overlay is open (except ? and Escape to close)
    const overlay = document.getElementById("shortcuts-overlay");
    if (overlay && overlay.style.display !== "none") {
        if (e.key === "?" || e.key === "Escape") {
            hideShortcuts();
            e.preventDefault();
        }
        return;
    }

    // Don't capture when delete-confirm overlay is open (except Escape to close)
    const deleteOverlay = document.getElementById("delete-overlay");
    if (deleteOverlay) {
        if (e.key === "Escape") {
            document.getElementById("delete-cancel")?.click();
            e.preventDefault();
        }
        return;
    }

    // Don't capture when the snooze preset sheet is open (except Escape to close)
    const snoozeSheetOverlay = document.getElementById("snooze-sheet-overlay");
    if (snoozeSheetOverlay) {
        if (e.key === "Escape") {
            document.getElementById("snooze-sheet-cancel")?.click();
            e.preventDefault();
        }
        return;
    }

    // Don't capture when a card overflow menu is open (except Escape to close)
    const openCardMenu = document.querySelector(".card-menu-dropdown:not([hidden])");
    if (openCardMenu) {
        if (e.key === "Escape") {
            closeAllCardMenus();
            e.preventDefault();
        }
        return;
    }

    const cards = getVisibleCards();

    switch (e.key) {
        case "n":
            e.preventDefault();
            openSaveModal();
            break;
        case "F":
            // Shift+F -> feeds. `n` (the spec's suggested key) is already the
            // "save new item" shortcut here, so feeds nav falls back to Shift+F.
            e.preventDefault();
            window.location.href = "/feeds";
            break;
        case "j":
            e.preventDefault();
            moveSelection(cards, 1);
            break;
        case "k":
            e.preventDefault();
            moveSelection(cards, -1);
            break;
        case "Enter":
            e.preventDefault();
            openSelectedArticle(cards);
            break;
        case "s":
            e.preventDefault();
            toggleSelectedVip(cards);
            break;
        case "1":
            e.preventDefault();
            rateSelected(cards, -1); // dislike
            break;
        case "2":
            e.preventDefault();
            rateSelected(cards, 1); // like
            break;
        case "3":
            e.preventDefault();
            rateSelected(cards, 2); // love
            break;
        case "x":
            e.preventDefault();
            deleteSelectedArticle(cards);
            break;
        case "u":
            e.preventDefault();
            triggerUndo(); // no-op when no undo window is live
            break;
        case "/":
            e.preventDefault();
            document.getElementById("search-input")?.focus();
            break;
        case "d":
            e.preventDefault();
            window.location.href = "/digest";
            break;
        case "r":
            // "r" regenerates the digest from the digest page (js/digest.js);
            // `.digest-tab` never exists on /inbox so this was always a
            // no-op here beyond swallowing the keypress — see file header.
            e.preventDefault();
            break;
        case "c":
            e.preventDefault();
            {
                const btn = document.getElementById("classify-btn");
                if (btn && !btn.disabled) btn.click();
            }
            break;
        case "g":
            e.preventDefault();
            window.location.href = "/stats";
            break;
        case "v":
            e.preventDefault();
            window.location.href = "/graph";
            break;
        case "h":
            e.preventDefault();
            window.location.href = "/highlights";
            break;
        case "a":
            // Toggle Library view (owner UX wave 1). `a` had no binding on the
            // inbox before this wave; it now flips the whole-collection Library
            // view on/off, coherent with the roadmap's legacy "a = articles"
            // meaning (the read+unread collection).
            e.preventDefault();
            toggleLibrary();
            break;
        case "f":
            e.preventDefault();
            {
                const tab = document.getElementById("filter-tab");
                if (tab) tab.click();
            }
            break;
        case "?":
            e.preventDefault();
            showShortcuts("inbox");
            break;
    }
}

function getVisibleCards() {
    const listEl = document.getElementById("article-list");
    if (!listEl) return [];
    // Get only visible cards (not hidden discards)
    return Array.from(listEl.querySelectorAll(".article-card")).filter(
        (card) => card.offsetParent !== null
    );
}

function moveSelection(cards, direction) {
    if (!cards.length) return;

    // Clear previous selection
    const prev = document.querySelector(".article-card.kb-selected");
    if (prev) prev.classList.remove("kb-selected");

    // Calculate new index
    if (selectedIndex === -1) {
        selectedIndex = direction === 1 ? 0 : cards.length - 1;
    } else {
        selectedIndex += direction;
    }

    // Clamp
    selectedIndex = Math.max(0, Math.min(cards.length - 1, selectedIndex));

    // Apply selection
    const card = cards[selectedIndex];
    card.classList.add("kb-selected");
    card.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function openSelectedArticle(cards) {
    if (selectedIndex < 0 || selectedIndex >= cards.length) return;
    const card = cards[selectedIndex];
    const link = card.querySelector(".article-title a");
    if (link) link.click();
}

/* Keyboard `s` / `1`/`2`/`3` go through their own fetch paths (rather than
   clicking the per-card button like they historically did) so the undo
   binder can capture the prior value BEFORE the action and offer a restore.
   Mouse clicks on the star / rate buttons keep their original handlers and
   deliberately do NOT get undo (binding spec: keyboard actions + swipe/menu
   triage actions are the undoable set). */

async function toggleSelectedVip(cards) {
    if (selectedIndex < 0 || selectedIndex >= cards.length) return;
    const card = cards[selectedIndex];
    const star = card.querySelector(".vip-star");
    if (!star) return;
    const sourceId = star.dataset.sourceId;

    // Sequence token (Finding 2, M3.2 final review), keyed by SOURCE (this
    // toggle mutates a source, not the article) -- same shape as
    // rateSelected/performArchive/performSnooze. See the token helpers'
    // header comment.
    const key = sourceTokenKey(sourceId);
    const token = bumpActionToken(key);

    const nowVip = await patchVipToggle(sourceId);
    if (isStaleActionToken(key, token)) return; // a newer action on this source now owns state
    if (nowVip === null) return;
    offerUndo(nowVip ? "Source marked VIP" : "Source VIP removed", async () => {
        await patchVipToggle(sourceId); // toggle back
        if (isStaleActionToken(key, token)) return;
        await loadInbox();
    });
    await loadInbox();
}

async function patchVipToggle(sourceId) {
    // Returns the new is_vip state, or null on failure.
    try {
        const res = await fetch(`/api/sources/${sourceId}/vip`, { method: "PATCH" });
        const json = await res.json();
        return json.success ? !!json.data.is_vip : null;
    } catch (err) {
        console.error("VIP toggle failed:", err);
        return null;
    }
}

async function rateSelected(cards, rating) {
    if (selectedIndex < 0 || selectedIndex >= cards.length) return;
    const card = cards[selectedIndex];
    const id = Number(card.dataset.id);
    const article = cachedArticles.find((a) => Number(a.id) === id);
    // Prior rating from cachedArticles BEFORE the action; null == unrated
    // (restored via the rate API's `{"rating": null}` clear).
    const priorRating = article && article.rating !== undefined ? article.rating : null;

    // Sequence token (Finding 2, M3.2 final review) -- bumped BEFORE the
    // optimistic mutation so a newer rating action on this same article
    // invalidates this one's eventual continuation, both the success
    // continuation below AND the failure/rollback branch. This is what
    // stops a held/delayed response from a stale first rate action from
    // clobbering the cache/UI/undo-slot after a second, faster rate action
    // on the same card has already resolved. See the token helpers' header
    // comment.
    const key = articleTokenKey(id);
    const token = bumpActionToken(key);

    // Mutate the cache optimistically IMMEDIATELY (before the await) so a
    // second rapid rating keypress on the same card — fired before this
    // PATCH round-trip resolves — captures THIS action's rating as its own
    // "prior" value, not the stale pre-action one (the race the undo target
    // corruption bug came from). Rolled back below on PATCH failure.
    if (article) article.rating = rating;

    const ok = await patchRating(id, rating);

    if (isStaleActionToken(key, token)) return; // a newer action on this article now owns state

    if (!ok) {
        if (article) article.rating = priorRating;
        showToast("Failed to rate article", "error");
        return;
    }
    updateCardRatingUI(card, rating);

    const labels = { "-1": "Rated: dislike", 1: "Rated: like", 2: "Rated: love" };
    const label = labels[String(rating)] || "Rated";

    if (!article) {
        // Fabricated-prior guard (Finding 1, M3.2 final review): no
        // `article` means this card was never in cachedArticles -- a
        // search hit outside the cached page, since runSearch() renders
        // straight from the API response without populating
        // cachedArticles. `priorRating` above therefore defaulted to null
        // rather than this article's real prior rating; offering undo on
        // that fabricated prior would silently clear a real rating. The
        // (already-committed, correct) rating stands; just no Undo button.
        showToast(label, "success");
        return;
    }

    offerUndo(label, async () => {
        if (!(await patchRating(id, priorRating))) return;
        if (isStaleActionToken(key, token)) return;
        if (article) article.rating = priorRating;
        // The card may have been re-rendered since — look it up live.
        const liveCard = document.querySelector(`.article-card[data-id="${id}"]`);
        if (liveCard) updateCardRatingUI(liveCard, priorRating);
    });
}

async function patchRating(articleId, rating) {
    try {
        const res = await fetch(`/api/articles/${articleId}/rate`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ rating }),
        });
        const json = await res.json();
        return !!json.success;
    } catch (err) {
        console.error("Rating failed:", err);
        return false;
    }
}

function updateCardRatingUI(card, rating) {
    card.querySelectorAll(".rate-btn").forEach((b) => {
        b.classList.toggle(
            "active",
            rating !== null && Number(b.dataset.rating) === Number(rating),
        );
    });
}


/* ---- Delete (single via keyboard + bulk via checkboxes) ---- */

function setupBulkDeleteToolbar() {
    const toolbar = document.getElementById("inbox-toolbar");
    if (!toolbar || document.getElementById("bulk-delete-btn")) return;

    const btn = document.createElement("button");
    btn.id = "bulk-delete-btn";
    btn.className = "btn btn-danger";
    btn.style.display = "none";
    // Trailing label span so the leading trash icon survives count updates.
    btn.innerHTML = `${icon("trash", { size: 14 })}<span class="bulk-delete-label"></span>`;
    btn.addEventListener("click", showBulkDeleteConfirm);
    toolbar.appendChild(btn);
}

function updateBulkDeleteToolbar() {
    const btn = document.getElementById("bulk-delete-btn");
    if (!btn) return;
    const n = selectedForDelete.size;
    if (n > 0) {
        const label = btn.querySelector(".bulk-delete-label");
        if (label) label.textContent = `Delete selected (${n})`;
        btn.style.display = "inline-flex";
    } else {
        btn.style.display = "none";
    }
}

function deleteSelectedArticle(cards) {
    if (selectedIndex < 0 || selectedIndex >= cards.length) return;
    const card = cards[selectedIndex];
    const id = Number(card.dataset.id);
    const article = cachedArticles.find((a) => Number(a.id) === id);
    const title = article ? article.title : "this article";

    showDeleteConfirm(
        `Permanently delete <strong>${esc(title)}</strong> from your library? This cannot be undone.`,
        () => performDelete([id])
    );
}

function showBulkDeleteConfirm() {
    const n = selectedForDelete.size;
    if (!n) return;

    showDeleteConfirm(
        `Permanently delete <strong>${n}</strong> selected article${n === 1 ? "" : "s"} from your library? This cannot be undone.`,
        () => performDelete(Array.from(selectedForDelete))
    );
}

function showDeleteConfirm(bodyHtml, onConfirm) {
    const existing = document.getElementById("delete-overlay");
    if (existing) existing.remove();

    const overlay = document.createElement("div");
    overlay.id = "delete-overlay";
    overlay.className = "export-overlay";
    overlay.innerHTML =
        '<div class="export-dialog">' +
            "<h3>Delete article</h3>" +
            `<p>${bodyHtml}</p>` +
            '<div class="export-dialog-actions">' +
                '<button class="export-cancel-btn" id="delete-cancel">Cancel</button>' +
                '<button class="danger-confirm-btn" id="delete-confirm">Delete</button>' +
            "</div>" +
        "</div>";
    document.body.appendChild(overlay);

    function onDeleteOverlayKeydown(e) {
        if (e.key === "Escape") close();
    }
    function close() {
        overlay.remove();
        document.removeEventListener("keydown", onDeleteOverlayKeydown);
    }
    document.getElementById("delete-cancel").addEventListener("click", close);
    document.getElementById("delete-confirm").addEventListener("click", () => {
        close();
        onConfirm();
    });
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) close();
    });
    document.addEventListener("keydown", onDeleteOverlayKeydown);
}

async function performDelete(ids) {
    const failed = [];
    for (const id of ids) {
        try {
            const res = await fetch(`/api/articles/${id}`, { method: "DELETE" });
            if (!res.ok) failed.push(id);
        } catch (err) {
            console.error("Delete failed:", err);
            failed.push(id);
        }
    }

    const succeeded = ids.filter((id) => !failed.includes(id));
    if (succeeded.length) {
        // Live pill update (M3.2 Task 4): deleting an article that was
        // actually counted as unread (unread AND not currently snoozed --
        // Finding 1 fix) removes it from the unread count same as archiving
        // one -- it's no longer in the library at all. A snoozed-unread
        // article was already excluded from the count and deleting it is a
        // no-op here. Not part of the undoable triage set (delete keeps its
        // confirm dialog, by design), so this is a one-way adjustment with
        // no undo counterpart.
        const deletedUnread = succeeded.filter((id) => {
            const a = cachedArticles.find((c) => Number(c.id) === Number(id));
            return countsAsUnread(a);
        }).length;
        if (deletedUnread > 0) adjustUnreadCount(-deletedUnread);

        cachedArticles = cachedArticles.filter((a) => !succeeded.includes(Number(a.id)));
        succeeded.forEach((id) => selectedForDelete.delete(id));
        renderArticleList(cachedArticles);
        updateToolbar(cachedArticles);
        updateBulkDeleteToolbar();
    }

    if (failed.length) {
        showToast(
            succeeded.length
                ? `Deleted ${succeeded.length}, failed to delete ${failed.length}`
                : `Failed to delete ${failed.length === 1 ? "article" : "articles"}`,
            "error"
        );
    } else {
        showToast(`Deleted ${succeeded.length} article${succeeded.length === 1 ? "" : "s"}`, "success");
    }
}

/* ---- Init ---- */

// Sidebar.js's own DOMContentLoaded handler runs its own unread-count fetch
// (for its badge) on every page load, including this one -- but module init
// order gives no guarantee it has resolved by the time this page's first
// render wants the number. Awaiting this module's own call to the same
// shared getter/fetch (sidebar.js's updateUnreadBadge()) is idempotent
// (same endpoint, same shared state) and avoids racing it.
async function initTriagePill() {
    await updateUnreadBadge();
    refreshTriageUI();
}

document.addEventListener("DOMContentLoaded", () => {
    if (!document.getElementById("article-list")) return;

    restoreFiltersFromURL();
    loadInbox();
    loadFilters();
    initTriagePill();
    setupSearch();
    setupSort();
    setupFilterPanel();
    setupBulkDeleteToolbar();
    setupCardMenuOutsideClick();
    setupSwipe();
    setupKeyboard();

    // Inbox-zero "Browse your library" affordance (owner UX wave 1). The
    // celebratory state (#inbox-zero-state) shows once triage empties the
    // default unread-first view; this button drops the user straight into
    // Library view (read + unread). It's also an <a href="/inbox?view=library">
    // so it's a real link (right-click, copy, no-JS), but we intercept the
    // click for a smooth in-page switch with no full reload.
    const zeroLibraryBtn = document.getElementById("inbox-zero-library-btn");
    if (zeroLibraryBtn) {
        zeroLibraryBtn.addEventListener("click", (e) => {
            e.preventDefault();
            setLibraryView(true);
        });
    }

    // Refresh after a save from the chrome-level save modal (sidebar.js).
    document.addEventListener("tiro:content-saved", () => {
        loadInbox();
        loadFilters();
    });

    // Finding 2 (M3.2 Task 4 review): re-render the pill/zero-state whenever
    // sidebar.js's shared count actually finishes refetching, not just on
    // "tiro:content-saved" -- that event fires synchronously, BEFORE
    // updateUnreadBadge()'s own fetch (called alongside it, unawaited) has
    // resolved, so a save-while-on-inbox could otherwise leave the pill one
    // fetch behind whatever loadInbox()'s own re-render happened to see.
    document.addEventListener("tiro:unread-count-updated", refreshTriageUI);
});
