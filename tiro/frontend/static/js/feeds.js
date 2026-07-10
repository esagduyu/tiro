/* Tiro — Feeds management (Phase 4 M4.1).
 *
 * Per-page LEAF entry module (M2.0): imports esc/num/showToast/timeAgo/
 * confirmDialog from core.js and icon from icons.js via the import map; nothing
 * imports feeds.js, so it keeps the normal `?v={{ static_v }}` cache-bust query
 * in feeds.html's <script type="module"> tag (same reasoning as sources.js).
 *
 * esc() discipline: EVERY server-derived string (feed title, folder, site_url,
 * last_error, url) passes through esc() at its innerHTML sink — server strings
 * are never interpolated raw. Numbers go through num().
 */

import { esc, num, showToast, timeAgo, confirmDialog } from "./core.js";
import { icon } from "./icons.js";

let feedsData = [];

const INTERVAL_OPTIONS = [
    [15, "15 min"],
    [30, "30 min"],
    [60, "1 hour"],
    [180, "3 hours"],
    [360, "6 hours"],
];

document.addEventListener("DOMContentLoaded", () => {
    setupAddForm();
    setupImportExport();
    setupKeyboard();
    loadFeeds();
});

/* --- Load + render ------------------------------------------------------- */

async function loadFeeds() {
    const statusEl = document.getElementById("feeds-status");
    const groupsEl = document.getElementById("feeds-groups");
    const emptyEl = document.getElementById("feeds-empty");

    try {
        const res = await fetch("/api/feeds");
        const json = await res.json();
        if (!json.success) throw new Error("bad response");

        feedsData = json.data || [];
        statusEl.style.display = "none";

        if (feedsData.length === 0) {
            groupsEl.style.display = "none";
            emptyEl.style.display = "block";
            document.getElementById("feed-url-input")?.focus();
            return;
        }
        emptyEl.style.display = "none";
        groupsEl.style.display = "block";
        renderFeeds();
    } catch (err) {
        statusEl.innerHTML = '<p class="settings-error">Failed to load feeds.</p>';
    }
}

function groupFeeds(feeds) {
    // Grouped by folder in first-seen order; the ungrouped bucket sorts last.
    const groups = new Map();
    for (const f of feeds) {
        const key = f.folder || "";
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(f);
    }
    const keys = [...groups.keys()].sort((a, b) => {
        if (a === "") return 1;
        if (b === "") return -1;
        return a.localeCompare(b);
    });
    return keys.map((k) => [k, groups.get(k)]);
}

function renderFeeds() {
    const groupsEl = document.getElementById("feeds-groups");
    groupsEl.innerHTML = groupFeeds(feedsData)
        .map(([folder, feeds]) => {
            const heading = folder
                ? `<div class="feeds-group-label">${esc(folder)}</div>`
                : "";
            return `<div class="feeds-group">${heading}${feeds.map(feedCardHtml).join("")}</div>`;
        })
        .join("");

    groupsEl.querySelectorAll("[data-action]").forEach((el) => {
        const id = parseInt(el.dataset.id, 10);
        const action = el.dataset.action;
        el.addEventListener("click", (e) => {
            e.preventDefault();
            if (action === "check") checkFeed(id);
            else if (action === "toggle") togglePause(id);
            else if (action === "rename") openRenameModal(id);
            else if (action === "delete") confirmDeleteFeed(id);
        });
    });

    groupsEl.querySelectorAll("[data-interval-for]").forEach((sel) => {
        sel.addEventListener("change", () => {
            changeInterval(parseInt(sel.dataset.intervalFor, 10), parseInt(sel.value, 10));
        });
    });
}

function statusPill(f) {
    if (f.status === "paused") {
        return '<span class="pill feeds-pill-paused">Paused</span>';
    }
    if (f.status === "error") {
        const tip = f.last_error ? ` title="${esc(f.last_error)}"` : "";
        return `<span class="pill feeds-pill-error"${tip}>${icon("alert", { size: 12 })} Error</span>`;
    }
    return '<span class="pill feeds-pill-active">Active</span>';
}

function lastFetchedText(f) {
    if (!f.last_fetched_at) return "never fetched";
    // Safari-safe: a space-separated SQL timestamp needs the "T" to parse.
    const then = new Date(String(f.last_fetched_at).replace(" ", "T"));
    if (isNaN(then.getTime())) return "never fetched";
    return `checked ${esc(timeAgo(then))}`;
}

function intervalSelectHtml(f) {
    const opts = INTERVAL_OPTIONS.map(([mins, label]) => {
        const sel = f.fetch_interval_minutes === mins ? " selected" : "";
        return `<option value="${mins}"${sel}>${esc(label)}</option>`;
    }).join("");
    return `<select class="feeds-interval" data-interval-for="${f.id}" title="Poll interval">${opts}</select>`;
}

function feedCardHtml(f) {
    const title = esc(f.title || f.url || "Untitled feed");
    const site = f.site_url
        ? `<a class="feeds-site-link" href="${esc(f.site_url)}" target="_blank" rel="noopener">${esc(f.site_url)}</a>`
        : `<span class="feeds-site-link muted">${esc(f.url || "")}</span>`;
    const paused = f.status === "paused";
    const toggleLabel = paused ? "Resume" : "Pause";
    const toggleIcon = paused ? "play" : "pause";

    return `
        <div class="feeds-card" data-feed-id="${f.id}">
            <div class="feeds-card-main">
                <div class="feeds-card-title">${title} ${statusPill(f)}</div>
                <div class="feeds-card-meta">
                    ${site}
                    <span class="feeds-dot">·</span>
                    <span>${num(f.article_count)} article${f.article_count === 1 ? "" : "s"}</span>
                    <span class="feeds-dot">·</span>
                    <span>${lastFetchedText(f)}</span>
                </div>
            </div>
            <div class="feeds-card-actions">
                ${intervalSelectHtml(f)}
                <button type="button" class="icon-btn" data-action="check" data-id="${f.id}" title="Check now">${icon("refresh", { size: 15 })}</button>
                <button type="button" class="btn btn-ghost" data-action="toggle" data-id="${f.id}">${icon(toggleIcon, { size: 14 })}${esc(toggleLabel)}</button>
                <button type="button" class="icon-btn" data-action="rename" data-id="${f.id}" title="Rename / move">${icon("pencil", { size: 15 })}</button>
                <button type="button" class="btn btn-danger" data-action="delete" data-id="${f.id}">${icon("trash", { size: 14 })}Delete</button>
            </div>
        </div>
    `;
}

/* --- Add feed ------------------------------------------------------------ */

function setupAddForm() {
    const form = document.getElementById("feeds-add-form");
    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const input = document.getElementById("feed-url-input");
        const btn = document.getElementById("feed-add-btn");
        const url = input.value.trim();
        if (!url) return;

        btn.disabled = true;
        try {
            const res = await fetch("/api/feeds", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ url }),
            });
            const json = await res.json().catch(() => ({}));
            if (res.status === 409 && json.error === "already_subscribed") {
                showToast("Already subscribed to that feed", "error");
                return;
            }
            if (!res.ok) {
                showToast(json.detail || "Could not add feed", "error");
                return;
            }
            input.value = "";
            showToast(`Subscribed to ${json.data.title}`, "success");
            loadFeeds();
        } catch (err) {
            showToast("Connection error", "error");
        } finally {
            btn.disabled = false;
        }
    });
}

/* --- Per-feed actions ---------------------------------------------------- */

async function checkFeed(id) {
    showToast("Checking feed…", "info");
    try {
        const res = await fetch(`/api/feeds/${id}/check`, { method: "POST" });
        const json = await res.json().catch(() => ({}));
        if (!res.ok) {
            showToast(json.detail || "Check failed", "error");
            return;
        }
        const d = json.data || {};
        showToast(`Checked — ${num(d.ingested)} new, ${num(d.skipped)} skipped`, "success");
        loadFeeds();
    } catch (err) {
        showToast("Connection error", "error");
    }
}

async function togglePause(id) {
    const feed = feedsData.find((f) => f.id === id);
    if (!feed) return;
    const next = feed.status === "paused" ? "active" : "paused";
    try {
        const res = await fetch(`/api/feeds/${id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status: next }),
        });
        const json = await res.json().catch(() => ({}));
        if (!res.ok) {
            showToast(json.detail || "Failed to update", "error");
            return;
        }
        showToast(next === "paused" ? "Feed paused" : "Feed resumed", "success");
        loadFeeds();
    } catch (err) {
        showToast("Connection error", "error");
    }
}

async function changeInterval(id, minutes) {
    try {
        const res = await fetch(`/api/feeds/${id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ fetch_interval_minutes: minutes }),
        });
        const json = await res.json().catch(() => ({}));
        if (!res.ok) {
            showToast(json.detail || "Failed to update interval", "error");
            loadFeeds();
            return;
        }
        const feed = feedsData.find((f) => f.id === id);
        if (feed) feed.fetch_interval_minutes = minutes;
        showToast("Interval updated", "success");
    } catch (err) {
        showToast("Connection error", "error");
    }
}

/* --- Rename / move modal ------------------------------------------------- */

function openRenameModal(id) {
    const feed = feedsData.find((f) => f.id === id);
    if (!feed) return;
    removeModal();

    const overlay = document.createElement("div");
    overlay.id = "feeds-modal-overlay";
    overlay.className = "settings-modal-overlay";
    overlay.innerHTML =
        '<div class="settings-modal">' +
            '<div class="settings-modal-header">' +
                "<h3>Rename feed</h3>" +
                '<button class="settings-modal-close" id="feeds-modal-close" title="Close">' + icon("close", { size: 15 }) + "</button>" +
            "</div>" +
            '<div class="settings-modal-body">' +
                '<div class="settings-field">' +
                    "<label>Title</label>" +
                    `<input type="text" id="feed-edit-title" value="${esc(feed.title || "")}">` +
                "</div>" +
                '<div class="settings-field">' +
                    "<label>Folder</label>" +
                    `<input type="text" id="feed-edit-folder" placeholder="(none)" value="${esc(feed.folder || "")}">` +
                "</div>" +
            "</div>" +
            '<div class="settings-modal-actions">' +
                '<button class="btn btn-ghost" id="feeds-modal-cancel">Cancel</button>' +
                '<button class="btn btn-primary" id="feeds-modal-save">Save</button>' +
            "</div>" +
        "</div>";
    document.body.appendChild(overlay);
    setupModalClose(overlay);

    document.getElementById("feeds-modal-save").addEventListener("click", async function () {
        const title = document.getElementById("feed-edit-title").value.trim();
        const folder = document.getElementById("feed-edit-folder").value.trim();
        const btn = this;
        btn.disabled = true;
        btn.textContent = "Saving…";
        try {
            const res = await fetch(`/api/feeds/${id}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ title, folder: folder || null }),
            });
            const json = await res.json().catch(() => ({}));
            if (!res.ok) {
                showToast(json.detail || "Failed to save", "error");
                btn.disabled = false;
                btn.textContent = "Save";
                return;
            }
            overlay.remove();
            showToast("Feed updated", "success");
            loadFeeds();
        } catch (err) {
            showToast("Connection error", "error");
            btn.disabled = false;
            btn.textContent = "Save";
        }
    });
}

/* --- Delete (keep-vs-delete-articles choice) ----------------------------- */

function confirmDeleteFeed(id) {
    const feed = feedsData.find((f) => f.id === id);
    if (!feed) return;
    removeModal();

    const count = feed.article_count || 0;
    const overlay = document.createElement("div");
    overlay.id = "feeds-delete-overlay";
    overlay.className = "export-overlay";
    overlay.innerHTML =
        '<div class="export-dialog">' +
            "<h3>Unsubscribe from feed</h3>" +
            `<p>Stop following <strong>${esc(feed.title || feed.url || "this feed")}</strong>. ` +
            `Its ${num(count)} saved article${count === 1 ? "" : "s"} stay in your library by default.</p>` +
            '<label class="feeds-delete-choice">' +
                '<input type="checkbox" id="feeds-delete-articles">' +
                ` Also delete ${num(count)} saved article${count === 1 ? "" : "s"} (a backup is made first)` +
            "</label>" +
            '<div class="export-dialog-actions">' +
                '<button class="export-cancel-btn" id="feeds-delete-cancel">Cancel</button>' +
                '<button class="btn btn-danger" id="feeds-delete-confirm">Unsubscribe</button>' +
            "</div>" +
        "</div>";
    document.body.appendChild(overlay);

    function onKeydown(e) {
        if (e.key === "Escape") close();
    }
    function close() {
        overlay.remove();
        document.removeEventListener("keydown", onKeydown);
    }
    document.getElementById("feeds-delete-cancel").addEventListener("click", close);
    document.getElementById("feeds-delete-confirm").addEventListener("click", () => {
        const withArticles = document.getElementById("feeds-delete-articles").checked;
        close();
        deleteFeed(id, withArticles);
    });
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) close();
    });
    document.addEventListener("keydown", onKeydown);
}

async function deleteFeed(id, deleteArticles) {
    const qs = deleteArticles ? "?delete_articles=true" : "";
    try {
        const res = await fetch(`/api/feeds/${id}${qs}`, { method: "DELETE" });
        const json = await res.json().catch(() => ({}));
        if (!res.ok) {
            showToast(json.detail || "Failed to unsubscribe", "error");
            return;
        }
        const removed = (json.data && json.data.deleted_articles) || 0;
        showToast(
            deleteArticles ? `Unsubscribed — ${num(removed)} article(s) deleted` : "Unsubscribed",
            "success",
        );
        loadFeeds();
    } catch (err) {
        showToast("Connection error", "error");
    }
}

/* --- OPML import / export ------------------------------------------------ */

function setupImportExport() {
    const importBtn = document.getElementById("feeds-import-btn");
    const fileInput = document.getElementById("feeds-import-input");
    importBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", async () => {
        const file = fileInput.files && fileInput.files[0];
        if (!file) return;
        const form = new FormData();
        form.append("file", file);
        try {
            const res = await fetch("/api/feeds/import", { method: "POST", body: form });
            const json = await res.json().catch(() => ({}));
            if (!res.ok) {
                showToast(json.detail || "Import failed", "error");
                return;
            }
            const d = json.data || {};
            let msg = `Imported ${num(d.added)} feed(s)`;
            if (d.skipped) msg += `, ${num(d.skipped)} already subscribed`;
            if (d.errors && d.errors.length) msg += `, ${num(d.errors.length)} error(s)`;
            showToast(msg, d.errors && d.errors.length ? "error" : "success");
            loadFeeds();
        } catch (err) {
            showToast("Connection error", "error");
        } finally {
            fileInput.value = "";
        }
    });
    // Export is a plain anchor navigation to GET /api/feeds/export.
}

/* --- Shared modal helpers ------------------------------------------------ */

function removeModal() {
    document.getElementById("feeds-modal-overlay")?.remove();
    document.getElementById("feeds-delete-overlay")?.remove();
}

function setupModalClose(overlay) {
    function onKeydown(e) {
        if (e.key === "Escape") close();
    }
    function close() {
        overlay.remove();
        document.removeEventListener("keydown", onKeydown);
    }
    document.getElementById("feeds-modal-close").addEventListener("click", close);
    document.getElementById("feeds-modal-cancel").addEventListener("click", close);
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) close();
    });
    document.addEventListener("keydown", onKeydown);
}

/* --- Keyboard (settings-adjacent-page map) ------------------------------- */

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
        if (document.getElementById("feeds-modal-overlay") || document.getElementById("feeds-delete-overlay")) {
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
                showFeedsShortcuts();
                break;
        }
    });

    document.getElementById("shortcuts-close")?.addEventListener("click", () => {
        document.getElementById("shortcuts-overlay").style.display = "none";
    });
    const overlayEl = document.getElementById("shortcuts-overlay");
    overlayEl?.addEventListener("click", (e) => {
        if (e.target === overlayEl) overlayEl.style.display = "none";
    });
}

function showFeedsShortcuts() {
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
            if (item.section) return `<div class="shortcut-section">${esc(item.section)}</div>`;
            const keys = item.keys.map((k) => `<kbd>${esc(k)}</kbd>`).join(" / ");
            return `<div class="shortcut-row"><span class="shortcut-keys">${keys}</span><span class="shortcut-desc">${esc(item.desc)}</span></div>`;
        })
        .join("");
    overlay.style.display = "flex";
}
