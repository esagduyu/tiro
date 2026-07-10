/* Tiro — page chrome module (M2.0 split of app.js, Task 2).
 *
 * Owns everything that runs on EVERY page: theme toggle, mobile sidebar
 * open/close, the unread badge, the sidebar "Views" section, the "Save to
 * Tiro" modal (now also the offline save queue and its retry/drain loop,
 * M3.1 Task 3 — pure queue logic lives in save-queue.js), the Add-to-Home-
 * Screen hint (M3.1 Task 3), logout, and the keyboard-shortcuts overlay
 * (shared markup lives in base.html; only its content differs per page).
 * Loaded as `<script type="module">` from base.html.
 *
 * Split from the historical app.js — see
 * docs/plans/2026-07-05-m2-0-frontend-modules-plan.md (Task 2) for the full
 * migration plan and .superpowers/sdd/task-2-report.md for the identifier
 * audit backing the window.* re-exposures below.
 *
 * `type="module"` scripts do not leak declarations onto `window`. As of
 * Task 3 (reader.js) and Task 4 (sources.js/wiki.js), all four former
 * classic scripts that consumed app.js's globals are now modules — reader.js
 * (the only consumer of showShortcuts/hideShortcuts) and inbox.js both
 * `import` the named exports directly from this file instead of reading
 * them off `window`. T5 (closeout) removed the `window.showShortcuts`/
 * `window.hideShortcuts` re-exposures accordingly (grep-confirmed
 * consumer-free across templates/*.html and every js/*.js — see
 * .superpowers/sdd/task-5-report.md). `window.timeAgo` was removed the same
 * way back in Task 4.
 */

import { esc, showToast } from "./core.js";
import { icon } from "./icons.js";
import { registerServiceWorker } from "./sw-register.js";
import {
    enqueueSave,
    dequeueForRetry,
    serializeQueue,
    deserializeQueue,
    SAVE_QUEUE_STORAGE_KEY,
} from "./save-queue.js";

/* ---- Theme management ---- */

function applyTheme(mode) {
    document.documentElement.setAttribute('data-theme', mode);
    const themeLink = document.getElementById('theme-css');
    if (themeLink) {
        // Read the server-resolved hrefs off the link element (set from
        // config.theme_light/theme_dark in the template context) instead of
        // hardcoding papyrus/roman-night, so a configured custom theme is
        // honored on toggle too. Falls back to the hardcoded pair if the
        // data attributes are absent (e.g. an older cached page).
        const href = mode === 'dark'
            ? themeLink.getAttribute('data-dark-href')
            : themeLink.getAttribute('data-light-href');
        if (href) {
            themeLink.href = href;
        } else {
            const themeName = mode === 'dark' ? 'roman-night' : 'papyrus';
            const currentVersion = new URL(themeLink.href, window.location.origin).searchParams.get('v');
            const versionSuffix = currentVersion ? `?v=${currentVersion}` : '';
            themeLink.href = `/static/themes/${themeName}.css${versionSuffix}`;
        }
    }
    // Show the mode you'd switch TO: sun in dark mode (→ light),
    // moon in light mode (→ dark). Each target keeps its own icon size: the
    // sidebar footer toggle renders at 17px, the phone More-sheet toggle at
    // 22px (matching its sheet siblings) — a single hardcoded size shrinks the
    // sheet icon after the first theme toggle.
    const glyphName = mode === 'dark' ? 'sun' : 'moon';
    // Sidebar footer toggle wraps its icon in a `.sidebar-icon` span (empty
    // in markup, filled here); the phone More-sheet toggle (#sheet-theme-toggle)
    // instead has a bare `.ti` icon followed by its own `<span>Theme</span>`
    // label — swap just the `.ti` there so the label survives.
    document.querySelectorAll('#theme-toggle, #sheet-theme-toggle').forEach(btn => {
        const themeGlyph = icon(glyphName, { size: btn.id === 'sheet-theme-toggle' ? 22 : 17 });
        const iconEl = btn.querySelector('.sidebar-icon');
        if (iconEl) {
            iconEl.innerHTML = themeGlyph;
        } else {
            const svg = btn.querySelector('.ti');
            if (svg) {
                svg.outerHTML = themeGlyph;
            } else {
                btn.innerHTML = themeGlyph;
            }
        }
    });
    const label = document.getElementById('theme-label');
    if (label) label.textContent = mode === 'dark' ? 'Dark' : 'Light';
    localStorage.setItem('tiro-mode', mode);
}

function toggleTheme() {
    const current = localStorage.getItem('tiro-mode') || 'light';
    applyTheme(current === 'dark' ? 'light' : 'dark');
}

/* ---- Phone bottom sheets (Library / More) ----
   Sheets are toggled by [data-sheet="<sheet-id>"] buttons in the tab bar.
   The `hidden` attribute controls presence; a `.open` class (added on the
   next frame) drives the slide-up + scrim fade transition. The scrim and
   any [data-sheet-close] element close it, as does Escape. */

function openSheet(id) {
    const sheet = document.getElementById(id);
    if (!sheet) return;
    // Close any other open sheet first (only one at a time).
    document.querySelectorAll('.sheet.open').forEach(closeSheet);
    sheet.hidden = false;
    // Force a reflow so the transition runs from the hidden state.
    void sheet.offsetWidth;
    sheet.classList.add('open');
}

function closeSheet(sheet) {
    if (!sheet) return;
    sheet.classList.remove('open');
    // Hide after the slide-down transition (200ms) so it animates out.
    setTimeout(() => {
        if (!sheet.classList.contains('open')) sheet.hidden = true;
    }, 220);
}

function closeAllSheets() {
    document.querySelectorAll('.sheet.open').forEach(closeSheet);
}

/* ---- Logout ---- */

// Shared by the sidebar's #logout-btn (all form factors, CSS-hidden on
// phones) and the phone More-sheet's #logout-btn-sheet — same function so
// behavior can never drift. Best-effort clears the SW article cache before
// ending the session (device-possession hardening, M3.2 Task 4); its own
// failure must NEVER block or delay the actual logout.
async function handleLogout() {
    if ('caches' in window) {
        try {
            const keys = await caches.keys();
            await Promise.all(
                keys
                    .filter((k) => /^tiro-.*-articles$/.test(k))
                    .map((k) => caches.delete(k))
            );
        } catch (e) {
            // Best-effort only -- never blocks logout.
        }
    }
    fetch('/api/auth/logout', { method: 'POST' }).finally(() => {
        window.location.href = '/login';
    });
}

/* ---- Unread count (shared with inbox.js's "N to zero" pill, M3.2 Task 4) ----
   `unreadCount` is the single source of truth for BOTH this sidebar badge
   (every page) and the inbox toolbar's triage-progress pill (/inbox only).
   inbox.js never runs its own parallel count -- it reads getUnreadCount()
   and mutates via adjustUnreadCount(), so the two can never drift apart.
   `null` means "not fetched yet" (distinct from a real 0), which inbox.js
   uses to decide whether to hide its pill for "data isn't loaded" rather
   than "zero unread". */

let unreadCount = null;

function renderUnreadBadge() {
    const hasUnread = unreadCount !== null && unreadCount > 0;
    const badge = document.getElementById('unread-badge');
    if (badge) {
        if (hasUnread) {
            badge.textContent = unreadCount > 99 ? '99+' : unreadCount;
            badge.style.display = '';
        } else {
            badge.style.display = 'none';
        }
    }
    // Phone tab-bar unread dot (no count, just presence).
    document.querySelectorAll('[data-unread-dot]').forEach((dot) => {
        dot.hidden = !hasUnread;
    });
}

async function updateUnreadBadge() {
    try {
        const res = await fetch('/api/articles?is_read=false&include_decayed=false&count_only=true');
        const json = await res.json();
        unreadCount = json.data?.count ?? 0;
        renderUnreadBadge();
    } catch (e) {}
    // Finding 2 (M3.2 Task 4 review): callers that trigger this (e.g. a save
    // from the chrome-level save modal) don't await it -- they fire-and-
    // forget it alongside a synchronous `notifyContentSaved()` dispatch, so
    // a page-specific consumer's own re-render (inbox.js's loadInbox, via
    // the "tiro:content-saved" listener) can resolve BEFORE this fetch does
    // and end up rendering a stale count. Dispatched here, after the count
    // is actually settled, so any listener re-renders from the true final
    // value regardless of ordering. Same decoupled CustomEvent pattern as
    // notifyContentSaved() -- this module stays free of a compile-time
    // dependency on inbox.js.
    document.dispatchEvent(new CustomEvent("tiro:unread-count-updated"));
}

function getUnreadCount() {
    return unreadCount;
}

// Applies a local delta (e.g. -1 when an unread article leaves the inbox
// via archive/snooze, +1 when undo or wake-now restores it) with no
// network round-trip -- the whole point of "live-updating on every triage
// action" (M3.2 Task 4 binding spec). No-op (returns null) if the count
// hasn't been fetched yet, since there is nothing correct to adjust from.
function adjustUnreadCount(delta) {
    if (unreadCount === null) return null;
    unreadCount = Math.max(0, unreadCount + delta);
    renderUnreadBadge();
    return unreadCount;
}

/* ---- Saved views (sidebar) ----
   Loaded via base.html on every page, mirroring updateUnreadBadge's pattern:
   guard on the sidebar element's presence rather than assuming a page type. */

let savedViews = []; // cached /api/views response, ordered by position
let renamingViewId = null; // id of the view currently showing an inline rename input

async function loadSavedViews() {
    const list = document.getElementById("sidebar-views-list");
    if (!list) return;
    try {
        const res = await fetch("/api/views");
        const json = await res.json();
        savedViews = json.success && Array.isArray(json.data) ? json.data : [];
    } catch (e) {
        savedViews = [];
    }
    renderSavedViews();
}

function viewToQueryString(view) {
    const params = new URLSearchParams();
    let parsed = {};
    try {
        parsed = JSON.parse(view.filter_json);
    } catch (e) {
        parsed = {};
    }
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        for (const [key, val] of Object.entries(parsed)) {
            if (val !== null && val !== undefined && val !== "") {
                params.set(key, val);
            }
        }
    }
    if (view.sort_mode) params.set("sort", view.sort_mode);
    return params.toString();
}

function renderSavedViews() {
    const section = document.getElementById("sidebar-views");
    const list = document.getElementById("sidebar-views-list");
    if (!section || !list) return;

    if (!savedViews.length) {
        section.style.display = "none";
        list.innerHTML = "";
        return;
    }
    section.style.display = "";

    list.innerHTML = savedViews.map((view, idx) => {
        if (view.id === renamingViewId) {
            return `<div class="sidebar-view-row">
                <input type="text" class="sidebar-view-rename-input" data-id="${view.id}" value="${esc(view.name)}">
                <span class="sidebar-view-actions sidebar-view-actions-visible">
                    <button class="sidebar-view-btn icon-btn icon-btn-sm sidebar-view-rename-save" data-id="${view.id}" title="Save">${icon("check", { size: 13 })}</button>
                    <button class="sidebar-view-btn icon-btn icon-btn-sm sidebar-view-rename-cancel" title="Cancel">${icon("close", { size: 13 })}</button>
                </span>
            </div>`;
        }
        return `<div class="sidebar-view-row">
            <a href="#" class="sidebar-view-link" data-id="${view.id}">${esc(view.name)}</a>
            <span class="sidebar-view-actions">
                <button class="sidebar-view-btn icon-btn icon-btn-sm sidebar-view-up" data-id="${view.id}" title="Move up" ${idx === 0 ? "disabled" : ""}>${icon("chevron-up", { size: 13 })}</button>
                <button class="sidebar-view-btn icon-btn icon-btn-sm sidebar-view-down" data-id="${view.id}" title="Move down" ${idx === savedViews.length - 1 ? "disabled" : ""}>${icon("chevron-down", { size: 13 })}</button>
                <button class="sidebar-view-btn icon-btn icon-btn-sm sidebar-view-rename" data-id="${view.id}" title="Rename">${icon("pencil", { size: 13 })}</button>
                <button class="sidebar-view-btn icon-btn icon-btn-sm sidebar-view-delete" data-id="${view.id}" title="Delete">${icon("close", { size: 13 })}</button>
            </span>
        </div>`;
    }).join("");

    const activeInput = list.querySelector(".sidebar-view-rename-input");
    if (activeInput) { activeInput.focus(); activeInput.select(); }
}

async function moveSavedView(id, direction) {
    const idx = savedViews.findIndex(v => v.id === id);
    const targetIdx = idx + direction;
    if (idx === -1 || targetIdx < 0 || targetIdx >= savedViews.length) return;
    const a = savedViews[idx];
    const b = savedViews[targetIdx];
    try {
        await fetch(`/api/views/${a.id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ position: b.position }),
        });
        await fetch(`/api/views/${b.id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ position: a.position }),
        });
    } catch (e) {}
    await loadSavedViews();
}

async function deleteSavedView(id) {
    try {
        await fetch(`/api/views/${id}`, { method: "DELETE" });
    } catch (e) {}
    await loadSavedViews();
}

function startRenameSavedView(id) {
    renamingViewId = id;
    renderSavedViews();
}

async function submitRenameSavedView(id) {
    const input = document.querySelector(`.sidebar-view-rename-input[data-id="${id}"]`);
    const name = input ? input.value.trim() : "";
    if (!name) {
        renamingViewId = null;
        renderSavedViews();
        return;
    }
    try {
        await fetch(`/api/views/${id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name }),
        });
    } catch (e) {}
    renamingViewId = null;
    await loadSavedViews();
}

function setupSidebarViews() {
    const list = document.getElementById("sidebar-views-list");
    if (!list) return;

    list.addEventListener("click", (e) => {
        const link = e.target.closest(".sidebar-view-link");
        if (link) {
            e.preventDefault();
            const view = savedViews.find(v => v.id === parseInt(link.dataset.id, 10));
            if (view) window.location.href = "/inbox?" + viewToQueryString(view);
            return;
        }
        const upBtn = e.target.closest(".sidebar-view-up");
        if (upBtn && !upBtn.disabled) {
            moveSavedView(parseInt(upBtn.dataset.id, 10), -1);
            return;
        }
        const downBtn = e.target.closest(".sidebar-view-down");
        if (downBtn && !downBtn.disabled) {
            moveSavedView(parseInt(downBtn.dataset.id, 10), 1);
            return;
        }
        const renameBtn = e.target.closest(".sidebar-view-rename");
        if (renameBtn) {
            startRenameSavedView(parseInt(renameBtn.dataset.id, 10));
            return;
        }
        const deleteBtn = e.target.closest(".sidebar-view-delete");
        if (deleteBtn) {
            deleteSavedView(parseInt(deleteBtn.dataset.id, 10));
            return;
        }
        const saveBtn = e.target.closest(".sidebar-view-rename-save");
        if (saveBtn) {
            submitRenameSavedView(parseInt(saveBtn.dataset.id, 10));
            return;
        }
        const cancelBtn = e.target.closest(".sidebar-view-rename-cancel");
        if (cancelBtn) {
            renamingViewId = null;
            renderSavedViews();
        }
    });

    list.addEventListener("keydown", (e) => {
        const input = e.target.closest(".sidebar-view-rename-input");
        if (!input) return;
        if (e.key === "Enter") { e.preventDefault(); submitRenameSavedView(parseInt(input.dataset.id, 10)); }
        if (e.key === "Escape") { e.preventDefault(); renamingViewId = null; renderSavedViews(); }
    });
}

/* ---- Offline save queue (M3.1 Task 3) ----
   Pure array logic lives in save-queue.js (node-tested); this section owns
   the only impure bits: the localStorage-backed in-memory copy, the DOM
   indicator in the save modal, and the sequential fetch() drain loop.

   In-memory `saveQueue` is the single source of truth for the current page
   load; every mutation is immediately persisted back to localStorage via
   persistQueue() so a reload (or another tab) picks up the same state. */

let saveQueue = deserializeQueue(localStorage.getItem(SAVE_QUEUE_STORAGE_KEY));
let draining = false;

function persistQueue() {
    localStorage.setItem(SAVE_QUEUE_STORAGE_KEY, serializeQueue(saveQueue));
}

function updateQueueIndicator() {
    const el = document.getElementById('save-queue-indicator');
    if (!el) return;
    if (saveQueue.length > 0) {
        el.textContent = saveQueue.length === 1 ? '1 queued' : `${saveQueue.length} queued`;
        el.style.display = '';
    } else {
        el.textContent = '';
        el.style.display = 'none';
    }
}

function queueOfflineSave(url, is_vip) {
    const { queue } = enqueueSave(saveQueue, { url, is_vip: !!is_vip, ts: Date.now() });
    saveQueue = queue;
    persistQueue();
    updateQueueIndicator();
}

// Drains the queue front-to-back, one POST at a time (never in parallel --
// a burst of parallel retries against a server that just came back up is
// exactly the kind of thing an offline queue should avoid). Per the binding
// spec:
//   - success (2xx)      -> remove from queue, toast success with title/url
//   - 409 already_saved  -> remove from queue silently (already in the library)
//   - other 4xx/5xx       -> remove from queue, toast failure (a poison entry
//                           that will never succeed must not be retried
//                           forever)
//   - network error again -> STOP draining and keep this entry (and
//                           everything behind it) queued for the next
//                           `online` event or page load
async function drainSaveQueue() {
    if (draining) return;
    draining = true;
    try {
        while (saveQueue.length > 0) {
            const { next, rest } = dequeueForRetry(saveQueue);

            let res;
            try {
                // NOTE: only `url` is sent -- IngestURLRequest (routes_ingest.py)
                // has no `is_vip` field, so `next.is_vip` (tracked in the queue
                // entry itself, see save-queue.js) was dead weight on the wire.
                res = await fetch('/api/ingest/url', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: next.url }),
                });
            } catch (e) {
                // Still offline (or the network flapped again) -- leave
                // `saveQueue` as-is (this entry, and everything queued
                // behind it, stays put) and stop for now.
                break;
            }

            // A response came back -- whatever it says, this entry is done
            // being retried (removed either way, per the spec above).
            saveQueue = rest;
            persistQueue();
            updateQueueIndicator();

            let json = null;
            try {
                json = await res.json();
            } catch (e) {
                json = null;
            }

            if (res.ok) {
                showToast(`Saved queued article: ${json?.data?.title || next.url}`, 'success');
                updateUnreadBadge();
                notifyContentSaved();
            } else if (res.status === 409 && (json === null || json?.error === 'already_saved')) {
                // already_saved -- silent, nothing to tell the user that
                // matters at this point. Checks the structured body's
                // `error` field (routes_ingest.py's only 409 shape) rather
                // than trusting the status code alone, with a status-only
                // fallback for the (should-never-happen) case where the
                // response body didn't parse as JSON at all.
            } else {
                showToast(`Failed to save queued article: ${next.url}`, 'error');
            }
        }
    } finally {
        draining = false;
    }
}

/* ---- Save modal ---- */

function openSaveModal() {
    const overlay = document.getElementById('save-overlay');
    if (!overlay) return;
    overlay.style.display = 'flex';
    // Reset state
    const urlInput = document.getElementById('save-url-input');
    if (urlInput) { urlInput.value = ''; urlInput.focus(); }
    const status = document.getElementById('save-status');
    if (status) status.style.display = 'none';
    const btn = document.getElementById('save-url-btn');
    if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
    updateQueueIndicator();
    // Reset to URL tab
    switchSaveTab('url');
}

function closeSaveModal() {
    const overlay = document.getElementById('save-overlay');
    if (overlay) overlay.style.display = 'none';
}

function switchSaveTab(tab) {
    document.querySelectorAll('.save-tab').forEach(t => {
        t.classList.toggle('active', t.dataset.tab === tab);
    });
    document.getElementById('save-tab-url').style.display = tab === 'url' ? '' : 'none';
    document.getElementById('save-tab-email').style.display = tab === 'email' ? '' : 'none';
    const status = document.getElementById('save-status');
    if (status) status.style.display = 'none';
    if (tab === 'url') {
        document.getElementById('save-url-input')?.focus();
    }
}

function showSaveStatus(msg, type) {
    const el = document.getElementById('save-status');
    if (!el) return;
    el.textContent = msg;
    el.className = 'save-status ' + type;
    el.style.display = '';
}

// Fired after a successful save (URL or .eml) so any page-specific module
// that's currently on screen (today: only inbox.js) can refresh its own
// state. Kept as a DOM CustomEvent rather than a direct import so the save
// modal (chrome, present on every page) doesn't need a compile-time
// dependency on inbox.js (present only on /inbox). Behavior-identical to the
// old app.js, which called `loadInbox()`/`loadFilters()` directly guarded on
// `document.getElementById('article-list')` — inbox.js's listener applies
// the same guard implicitly by only existing on the inbox page.
function notifyContentSaved() {
    document.dispatchEvent(new CustomEvent("tiro:content-saved"));
}

async function submitURL() {
    const input = document.getElementById('save-url-input');
    const btn = document.getElementById('save-url-btn');
    const url = input?.value.trim();
    if (!url) return;
    btn.disabled = true;
    btn.textContent = 'Saving...';
    showSaveStatus('Fetching and processing...', 'loading');

    // The fetch() call itself is isolated in its own try/catch so a NETWORK
    // error (fetch rejection -- offline, DNS failure, connection refused;
    // always a TypeError) can be told apart from a 4xx/5xx response, which
    // resolves fetch() normally and is handled below exactly as before this
    // task. Only a network error gets queued -- a real error response
    // (already_saved, 500, etc.) still surfaces inline as it always has.
    let res;
    try {
        res = await fetch('/api/ingest/url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url }),
        });
    } catch (e) {
        queueOfflineSave(url, false);
        showToast('Offline — queued; will retry when back online', 'info');
        closeSaveModal();
        return;
    }

    try {
        const json = await res.json();
        if (res.status === 409) {
            showSaveStatus('Already saved: ' + (json.data?.title || url), 'error');
            btn.disabled = false;
            btn.textContent = 'Save';
            return;
        }
        if (!res.ok) throw new Error(json.detail || 'Failed to save');
        showSaveStatus('Saved: ' + (json.data?.title || 'Article'), 'success');
        btn.textContent = 'Saved';
        input.value = '';
        updateUnreadBadge();
        notifyContentSaved();
    } catch (e) {
        showSaveStatus(e.message || 'Failed to save URL', 'error');
        btn.disabled = false;
        btn.textContent = 'Save';
    }
}

async function uploadEml(file) {
    if (!file || !file.name.endsWith('.eml')) {
        showSaveStatus('Please select a .eml file', 'error');
        return;
    }
    showSaveStatus('Processing ' + file.name + '...', 'loading');
    const formData = new FormData();
    formData.append('file', file);
    try {
        const res = await fetch('/api/ingest/email', {
            method: 'POST',
            body: formData,
        });
        const json = await res.json();
        if (!res.ok) throw new Error(json.detail || 'Failed to import');
        showSaveStatus('Saved: ' + (json.data?.title || file.name), 'success');
        updateUnreadBadge();
        notifyContentSaved();
    } catch (e) {
        showSaveStatus(e.message || 'Failed to import email', 'error');
    }
}

function setupSaveModal() {
    const overlay = document.getElementById('save-overlay');
    if (!overlay) return;

    // Open
    document.getElementById('sidebar-save-btn')?.addEventListener('click', openSaveModal);

    // Close
    document.getElementById('save-modal-close')?.addEventListener('click', closeSaveModal);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeSaveModal(); });

    // Tabs
    document.querySelectorAll('.save-tab').forEach(tab => {
        tab.addEventListener('click', () => switchSaveTab(tab.dataset.tab));
    });

    // URL submit
    document.getElementById('save-url-btn')?.addEventListener('click', submitURL);
    document.getElementById('save-url-input')?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); submitURL(); }
        if (e.key === 'Escape') { e.preventDefault(); closeSaveModal(); }
    });

    // File input
    const fileInput = document.getElementById('save-file-input');
    fileInput?.addEventListener('change', () => {
        if (fileInput.files.length > 0) uploadEml(fileInput.files[0]);
    });

    // Drag and drop
    const dropZone = document.getElementById('save-drop-zone');
    if (dropZone) {
        dropZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropZone.classList.add('dragover');
        });
        dropZone.addEventListener('dragleave', () => {
            dropZone.classList.remove('dragover');
        });
        dropZone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropZone.classList.remove('dragover');
            if (e.dataTransfer.files.length > 0) uploadEml(e.dataTransfer.files[0]);
        });
    }
}

/* ---- LAN-over-HTTP warning banner (M3.0 Task 4) ----
   The banner element only exists in the DOM at all when the server decided
   insecure_lan_http is true (base.html's Jinja conditional) -- so this is
   entirely about (a) hiding it again on same-tab navigation once dismissed
   this session, and (b) wiring the dismiss button. sessionStorage (not
   localStorage): a dismissal shouldn't silently suppress the warning
   forever across unrelated future sessions/tabs, just for the rest of
   this one. */

const LAN_BANNER_DISMISSED_KEY = "tiro-lan-banner-dismissed";

function setupLanBanner() {
    const banner = document.getElementById("lan-http-banner");
    if (!banner) return;

    if (sessionStorage.getItem(LAN_BANNER_DISMISSED_KEY) === "1") {
        banner.style.display = "none";
        document.body.classList.remove("has-lan-banner");
        return;
    }

    document.getElementById("lan-http-banner-dismiss")?.addEventListener("click", () => {
        sessionStorage.setItem(LAN_BANNER_DISMISSED_KEY, "1");
        banner.style.display = "none";
        document.body.classList.remove("has-lan-banner");
    });
}

/* ---- Legacy-library-path suggestion banner (Phase 5 D3) ----
   Nudges an existing install whose library still sits at the old CWD-relative
   ./tiro-library default toward `tiro migrate-library`. The element only exists
   in the DOM when the server decided library_at_legacy_default is true
   (base.html Jinja conditional) -- so this just wires the dismiss button and
   hides it after a prior dismissal. Dismissal is PERMANENT per-browser
   (localStorage, unlike the LAN banner's sessionStorage): this is a one-time
   suggestion, not a per-session security warning that should keep reappearing. */

const LIBMOVE_BANNER_DISMISSED_KEY = "tiro-libmove-dismissed";

function setupLibmoveBanner() {
    const banner = document.getElementById("libmove-banner");
    if (!banner) return;

    if (localStorage.getItem(LIBMOVE_BANNER_DISMISSED_KEY) === "1") {
        banner.style.display = "none";
        return;
    }

    document.getElementById("libmove-banner-dismiss")?.addEventListener("click", () => {
        localStorage.setItem(LIBMOVE_BANNER_DISMISSED_KEY, "1");
        banner.style.display = "none";
    });
}

/* ---- Add-to-Home-Screen hint (M3.1 Task 3) ----
   A one-time, dismissable nudge -- NOT a beforeinstallprompt capture (that
   event is Chromium-only, non-standard, and effectively unsupported on iOS
   Safari, which is the exact audience most likely to be reading Tiro on a
   phone without realizing it can be installed; a plain instructional toast
   works identically everywhere and doesn't need feature detection beyond
   matchMedia). Shown only on a mobile-ish viewport (matches the existing
   768px breakpoint used throughout styles.css) and never when already
   running installed/standalone (matchMedia('(display-mode: standalone)') --
   a device that's already home-screened has nothing to be nudged about).
   Dismissal is permanent (localStorage, not sessionStorage) since the whole
   point is to show it ONCE, ever, unlike the per-session LAN banner above. */

const A2HS_HINT_DISMISSED_KEY = "tiro-a2hs-hint-dismissed";
const A2HS_HINT_DELAY_MS = 3000;

function shouldShowA2HSHint() {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    if (localStorage.getItem(A2HS_HINT_DISMISSED_KEY) === "1") return false;
    if (window.matchMedia("(display-mode: standalone)").matches) return false;
    if (!window.matchMedia("(max-width: 768px)").matches) return false;
    return true;
}

function showA2HSHint() {
    if (document.getElementById("a2hs-hint")) return;

    const el = document.createElement("div");
    el.id = "a2hs-hint";
    el.className = "a2hs-hint";

    const text = document.createElement("span");
    text.className = "a2hs-hint-text";
    text.textContent = "Tip: add Tiro to your home screen for the full app experience";

    const dismissBtn = document.createElement("button");
    dismissBtn.id = "a2hs-hint-dismiss";
    dismissBtn.className = "a2hs-hint-dismiss";
    dismissBtn.setAttribute("aria-label", "Dismiss");
    dismissBtn.innerHTML = icon("close", { size: 13 });
    dismissBtn.addEventListener("click", () => {
        localStorage.setItem(A2HS_HINT_DISMISSED_KEY, "1");
        el.remove();
    });

    el.appendChild(text);
    el.appendChild(dismissBtn);
    document.body.appendChild(el);
}

function setupA2HSHint() {
    // Re-check at fire time, not just at schedule time -- e.g. a same-tab
    // dismissal of a hint shown on a prior page load already set the
    // localStorage key well before this timer fires on the next page.
    setTimeout(() => {
        if (shouldShowA2HSHint()) showA2HSHint();
    }, A2HS_HINT_DELAY_MS);
}

/* ---- Keyboard-shortcuts overlay (shared markup in base.html; content
   differs per page). Exported for inbox.js and reader.js, both of which
   `import` these directly — no window re-exposure needed or present. ---- */

const INBOX_SHORTCUTS = [
    { section: "Navigation" },
    { keys: ["j"], desc: "Move down" },
    { keys: ["k"], desc: "Move up" },
    { keys: ["Enter"], desc: "Open selected article" },
    { keys: ["/"], desc: "Focus search bar" },
    { keys: ["d"], desc: "Go to digest" },
    { keys: ["g"], desc: "Go to reading stats" },
    { keys: ["v"], desc: "Go to knowledge graph" },
    { keys: ["h"], desc: "Go to highlights" },
    { keys: ["Shift", "F"], desc: "Go to feeds" },
    { keys: ["a"], desc: "Toggle Library view (read + unread)" },
    { section: "Actions" },
    { keys: ["s"], desc: "Toggle VIP on selected source" },
    { keys: ["1"], desc: "Rate dislike" },
    { keys: ["2"], desc: "Rate like" },
    { keys: ["3"], desc: "Rate love" },
    { keys: ["x"], desc: "Delete selected article" },
    { keys: ["u"], desc: "Undo last triage action" },
    { hint: "Swipe a card right to archive, left to snooze (touch)" },
    { hint: "Click checkboxes to bulk-select articles for deletion" },
    { keys: ["n"], desc: "Save new item (URL or email)" },
    { keys: ["c"], desc: "Classify / reclassify inbox" },
    { keys: ["f"], desc: "Toggle filter panel" },
    { keys: ["r"], desc: "Regenerate digest (in digest view)" },
    { section: "General" },
    { keys: ["?"], desc: "Show this help" },
    { keys: ["Esc"], desc: "Blur search / close overlay" },
];

const READER_SHORTCUTS = [
    { section: "Navigation" },
    { keys: ["b", "Esc"], desc: "Back to inbox" },
    { keys: ["d"], desc: "Go to digest" },
    { keys: ["g"], desc: "Go to reading stats" },
    { keys: ["v"], desc: "Go to knowledge graph" },
    { keys: ["h"], desc: "Go to highlights" },
    { keys: ["Shift", "F"], desc: "Go to feeds" },
    { section: "Actions" },
    { keys: ["s"], desc: "Toggle VIP on source" },
    { keys: ["1"], desc: "Rate dislike" },
    { keys: ["2"], desc: "Rate like" },
    { keys: ["3"], desc: "Rate love" },
    { keys: ["x"], desc: "Delete article" },
    { keys: ["i"], desc: "Toggle analysis panel" },
    { keys: ["r"], desc: "Run / re-run analysis (panel open)" },
    { keys: ["p"], desc: "Play / pause audio" },
    { section: "General" },
    { keys: ["?"], desc: "Show this help" },
];

function showShortcuts(view) {
    const overlay = document.getElementById("shortcuts-overlay");
    const body = document.getElementById("shortcuts-body");
    if (!overlay || !body) return;

    const shortcuts = view === "reader" ? READER_SHORTCUTS : INBOX_SHORTCUTS;

    body.innerHTML = shortcuts
        .map((item) => {
            if (item.section) {
                return `<div class="shortcut-section">${item.section}</div>`;
            }
            if (item.hint) {
                return `<div class="shortcut-hint">${esc(item.hint)}</div>`;
            }
            const keys = item.keys
                .map((k) => `<kbd>${esc(k)}</kbd>`)
                .join(" / ");
            return `<div class="shortcut-row">
                <span class="shortcut-keys">${keys}</span>
                <span class="shortcut-desc">${esc(item.desc)}</span>
            </div>`;
        })
        .join("");

    overlay.style.display = "flex";
}

function hideShortcuts() {
    const overlay = document.getElementById("shortcuts-overlay");
    if (overlay) overlay.style.display = "none";
}

export {
    showShortcuts, hideShortcuts, INBOX_SHORTCUTS, READER_SHORTCUTS,
    loadSavedViews, openSaveModal, closeSaveModal,
    updateUnreadBadge, getUnreadCount, adjustUnreadCount,
};

/* ---- Init (runs on every page) ---- */

document.addEventListener("DOMContentLoaded", () => {
    // Theme toggles (sidebar footer + phone More-sheet)
    document.getElementById('theme-toggle')?.addEventListener('click', toggleTheme);
    document.getElementById('sheet-theme-toggle')?.addEventListener('click', toggleTheme);

    // Phone tab bar: Save circle opens the same save modal as the sidebar.
    document.getElementById('tab-save-btn')?.addEventListener('click', openSaveModal);

    // Phone bottom sheets: [data-sheet] buttons open, scrim / [data-sheet-close]
    // / Escape close.
    document.querySelectorAll('[data-sheet]').forEach((btn) => {
        btn.addEventListener('click', () => openSheet(btn.dataset.sheet));
    });
    document.querySelectorAll('[data-sheet-close]').forEach((el) => {
        el.addEventListener('click', () => closeSheet(el.closest('.sheet')));
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeAllSheets();
    });

    // About (phone More-sheet) mirrors the sidebar's About link — opens the
    // project page in a new tab.
    document.getElementById('about-btn-sheet')?.addEventListener('click', () => {
        window.open('https://github.com/esagduyu/project-tiro', '_blank', 'noopener');
    });

    // Logout — the sidebar #logout-btn (all form factors, CSS-hidden on
    // phones) and the phone More-sheet #logout-btn-sheet share handleLogout.
    document.getElementById('logout-btn')?.addEventListener('click', handleLogout);
    document.getElementById('logout-btn-sheet')?.addEventListener('click', handleLogout);

    // Keep the phone tab-bar unread dot in sync when any module settles the
    // shared count (same event inbox.js's pill listens to).
    document.addEventListener('tiro:unread-count-updated', renderUnreadBadge);

    // Apply stored theme (reinforces the inline script in base.html)
    const mode = localStorage.getItem('tiro-mode') || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    applyTheme(mode);

    // Unread badge
    updateUnreadBadge();

    // Saved views (sidebar section, present on every page)
    setupSidebarViews();
    loadSavedViews();

    // Save modal
    setupSaveModal();

    // LAN-over-HTTP warning banner
    setupLanBanner();

    // Legacy-library-path suggestion banner (Phase 5 D3)
    setupLibmoveBanner();

    // Offline save queue (M3.1 Task 3): reflect whatever survived from a
    // prior page load, then try to drain it right away -- covers the "user
    // reloads/reopens the app while still offline-then-online" case, not
    // just the live `online` event below (e.g. a tab that was closed
    // offline and reopened after connectivity was already back).
    updateQueueIndicator();
    window.addEventListener('online', drainSaveQueue);
    if (saveQueue.length > 0 && navigator.onLine) {
        drainSaveQueue();
    }

    // Add-to-Home-Screen hint (M3.1 Task 3): mobile-viewport-only, one-time,
    // delayed so it doesn't compete with the page's own initial load.
    setupA2HSHint();

    // Service worker (M3.1 Task 2): feature-detected, silent, registered
    // once per page load (see sw-register.js's own header comment).
    registerServiceWorker();
});
