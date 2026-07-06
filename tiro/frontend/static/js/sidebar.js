/* Tiro — page chrome module (M2.0 split of app.js, Task 2).
 *
 * Owns everything that runs on EVERY page: theme toggle, mobile sidebar
 * open/close, the unread badge, the sidebar "Views" section, the "Save to
 * Tiro" modal, logout, and the keyboard-shortcuts overlay (shared markup
 * lives in base.html; only its content differs per page). Loaded as
 * `<script type="module">` from base.html.
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

import { esc } from "./core.js";
import { registerServiceWorker } from "./sw-register.js";

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
    document.querySelectorAll('#theme-toggle, #mobile-theme-toggle').forEach(btn => {
        const icon = btn.querySelector('.sidebar-icon');
        if (icon) {
            icon.textContent = mode === 'dark' ? '☽' : '☀';
        } else {
            btn.textContent = mode === 'dark' ? '☽' : '☀';
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

/* ---- Mobile sidebar ---- */

function openSidebar() {
    document.getElementById('sidebar')?.classList.add('open');
    document.getElementById('sidebar-overlay')?.classList.add('open');
}

function closeSidebar() {
    document.getElementById('sidebar')?.classList.remove('open');
    document.getElementById('sidebar-overlay')?.classList.remove('open');
}

/* ---- Unread badge ---- */

async function updateUnreadBadge() {
    try {
        const res = await fetch('/api/articles?is_read=false&include_decayed=false&count_only=true');
        const json = await res.json();
        const badge = document.getElementById('unread-badge');
        if (!badge) return;
        const count = json.data?.count ?? 0;
        if (count > 0) {
            badge.textContent = count > 99 ? '99+' : count;
            badge.style.display = '';
        } else {
            badge.style.display = 'none';
        }
    } catch (e) {}
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
                    <button class="sidebar-view-btn sidebar-view-rename-save" data-id="${view.id}" title="Save">&#10003;</button>
                    <button class="sidebar-view-btn sidebar-view-rename-cancel" title="Cancel">&times;</button>
                </span>
            </div>`;
        }
        return `<div class="sidebar-view-row">
            <a href="#" class="sidebar-view-link" data-id="${view.id}">${esc(view.name)}</a>
            <span class="sidebar-view-actions">
                <button class="sidebar-view-btn sidebar-view-up" data-id="${view.id}" title="Move up" ${idx === 0 ? "disabled" : ""}>&#9650;</button>
                <button class="sidebar-view-btn sidebar-view-down" data-id="${view.id}" title="Move down" ${idx === savedViews.length - 1 ? "disabled" : ""}>&#9660;</button>
                <button class="sidebar-view-btn sidebar-view-rename" data-id="${view.id}" title="Rename">&#9998;</button>
                <button class="sidebar-view-btn sidebar-view-delete" data-id="${view.id}" title="Delete">&times;</button>
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
    try {
        const res = await fetch('/api/ingest/url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url }),
        });
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
    document.getElementById('save-btn')?.addEventListener('click', openSaveModal);

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
    { section: "Actions" },
    { keys: ["s"], desc: "Toggle VIP on selected source" },
    { keys: ["1"], desc: "Rate dislike" },
    { keys: ["2"], desc: "Rate like" },
    { keys: ["3"], desc: "Rate love" },
    { keys: ["x"], desc: "Delete selected article" },
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
};

/* ---- Init (runs on every page) ---- */

document.addEventListener("DOMContentLoaded", () => {
    // Theme toggles
    document.getElementById('theme-toggle')?.addEventListener('click', toggleTheme);
    document.getElementById('mobile-theme-toggle')?.addEventListener('click', toggleTheme);

    // Mobile sidebar
    document.getElementById('mobile-menu-btn')?.addEventListener('click', openSidebar);
    document.getElementById('sidebar-overlay')?.addEventListener('click', closeSidebar);

    // Logout
    document.getElementById('logout-btn')?.addEventListener('click', () => {
        fetch('/api/auth/logout', { method: 'POST' }).finally(() => {
            window.location.href = '/login';
        });
    });

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

    // Service worker (M3.1 Task 2): feature-detected, silent, registered
    // once per page load (see sw-register.js's own header comment).
    registerServiceWorker();
});
