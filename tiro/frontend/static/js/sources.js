/* Tiro — Sources & Authors management (M2.0 module split, Task 4).
 *
 * Imports esc/num/showToast from core.js (this file's local copies were
 * verified byte-identical to core.js's versions before deletion — same
 * DOM-trick esc()/finite-number num()/two-stage-setTimeout showToast() as
 * documented in core.js's docstrings, which cite this exact file as the
 * origin of showToast() — see .superpowers/sdd/task-4-report.md for the
 * confirmation).
 *
 * sources.js is a LEAF entry module — nothing else imports it — so it keeps
 * the normal `?v={{ static_v }}` cache-bust query in sources.html's
 * <script type="module"> tag (same reasoning as reader.js in Task 3).
 *
 * `confirmDeleteSource` (the `sources-delete-overlay`/`sources-delete-cancel`/
 * `sources-delete-confirm` dialog) is intentionally NOT migrated to core.js's
 * `confirmDialog`: `setupKeyboard()` below directly checks for
 * `#sources-delete-overlay`/`#sources-modal-overlay` by id to gate other
 * keys while a dialog is open, a different id contract than confirmDialog's
 * `core-confirm-*` ids. Same judgment call as Task 2 (inbox.js) and Task 3
 * (reader.js) — not mandated by the brief, left local and documented rather
 * than silently skipped.
 */

import { esc, num, showToast } from "./core.js";
import { icon } from "./icons.js";

let sourcesData = [];
let authorsData = [];
let authorsLoaded = false;

document.addEventListener("DOMContentLoaded", () => {
    setupTabs();
    setupKeyboard();
    loadSources();
});

/* --- Tabs --- */

function setupTabs() {
    document.querySelectorAll(".sources-tab").forEach((tab) => {
        tab.addEventListener("click", () => {
            document.querySelectorAll(".sources-tab").forEach((t) => t.classList.remove("active"));
            tab.classList.add("active");

            const which = tab.dataset.tab;
            document.getElementById("sources-panel").style.display = which === "sources" ? "block" : "none";
            document.getElementById("authors-panel").style.display = which === "authors" ? "block" : "none";

            if (which === "authors" && !authorsLoaded) {
                loadAuthors();
            }
        });
    });
}

/* --- Sources tab --- */

async function loadSources() {
    const statusEl = document.getElementById("sources-status");
    const tableEl = document.getElementById("sources-table");
    const emptyEl = document.getElementById("sources-empty");

    try {
        const res = await fetch("/api/sources");
        const json = await res.json();
        if (!json.success) throw new Error("Invalid response");

        sourcesData = json.data || [];
        statusEl.style.display = "none";

        if (sourcesData.length === 0) {
            tableEl.style.display = "none";
            emptyEl.style.display = "block";
            return;
        }

        emptyEl.style.display = "none";
        tableEl.style.display = "table";
        renderSources();
    } catch (err) {
        statusEl.innerHTML = '<p class="settings-error">Failed to load sources.</p>';
    }
}

function renderSources() {
    const tbody = document.getElementById("sources-tbody");
    tbody.innerHTML = sourcesData.map(sourceRowHtml).join("");

    tbody.querySelectorAll("[data-action]").forEach((el) => {
        const id = parseInt(el.dataset.id, 10);
        const action = el.dataset.action;
        el.addEventListener("click", () => {
            if (action === "vip") toggleSourceVip(id);
            else if (action === "edit") openEditSourceModal(id);
            else if (action === "merge") openMergeSourceModal(id);
            else if (action === "delete") confirmDeleteSource(id);
        });
    });
}

function sourceRowHtml(s) {
    const typeLabel = s.source_type || "web";
    const domainOrSender = s.email_sender || s.domain || "";
    const vipClass = s.is_vip ? "icon-btn sources-vip-btn active" : "icon-btn sources-vip-btn";
    return `
        <tr>
            <td data-label="Name">${esc(s.name || "Unnamed")}</td>
            <td data-label="Type"><span class="source-type-pill source-type-${esc(typeLabel)}">${esc(typeLabel)}</span></td>
            <td data-label="Domain / Sender">${esc(domainOrSender)}</td>
            <td class="sources-col-count" data-label="Articles">${num(s.article_count)}</td>
            <td class="sources-col-vip" data-label="VIP">
                <button type="button" class="${vipClass}" data-action="vip" data-id="${s.id}" title="Toggle VIP">${icon("star", { size: 15 })}</button>
            </td>
            <td class="sources-col-actions" data-label="Actions">
                <div class="sources-row-actions">
                    <button type="button" class="icon-btn" data-action="edit" data-id="${s.id}" title="Edit source">${icon("pencil", { size: 15 })}</button>
                    <button type="button" class="btn btn-ghost" data-action="merge" data-id="${s.id}">Merge</button>
                    <button type="button" class="btn btn-danger" data-action="delete" data-id="${s.id}">${icon("trash", { size: 14 })}Delete</button>
                </div>
            </td>
        </tr>
    `;
}

async function toggleSourceVip(id) {
    try {
        const res = await fetch(`/api/sources/${id}/vip`, { method: "PATCH" });
        const json = await res.json();
        if (!res.ok || !json.success) throw new Error("Failed");

        const source = sourcesData.find((s) => s.id === id);
        if (source) source.is_vip = json.data.is_vip;
        renderSources();
    } catch (err) {
        showToast("Failed to update VIP status", "error");
    }
}

/* --- Edit source modal --- */

function openEditSourceModal(id) {
    const source = sourcesData.find((s) => s.id === id);
    if (!source) return;

    removeSourcesModal();

    const overlay = document.createElement("div");
    overlay.id = "sources-modal-overlay";
    overlay.className = "settings-modal-overlay";
    overlay.innerHTML =
        '<div class="settings-modal">' +
            '<div class="settings-modal-header">' +
                "<h3>Edit source</h3>" +
                '<button class="settings-modal-close" id="sources-modal-close" title="Close">' + icon("close", { size: 15 }) + "</button>" +
            "</div>" +
            '<div class="settings-modal-body">' +
                '<div class="settings-field">' +
                    "<label>Name</label>" +
                    `<input type="text" id="edit-source-name" value="${esc(source.name || "")}">` +
                "</div>" +
                '<div class="settings-field">' +
                    "<label>Domain</label>" +
                    `<input type="text" id="edit-source-domain" value="${esc(source.domain || "")}">` +
                "</div>" +
                '<div class="settings-field">' +
                    "<label>Email sender</label>" +
                    `<input type="text" id="edit-source-email" value="${esc(source.email_sender || "")}">` +
                "</div>" +
            "</div>" +
            '<div class="settings-modal-actions">' +
                '<button class="btn btn-ghost" id="sources-modal-cancel">Cancel</button>' +
                '<button class="btn btn-primary" id="sources-modal-save">Save</button>' +
            "</div>" +
        "</div>";
    document.body.appendChild(overlay);

    setupModalCloseHandlers(overlay);

    document.getElementById("sources-modal-save").addEventListener("click", async function () {
        const name = document.getElementById("edit-source-name").value.trim();
        const domain = document.getElementById("edit-source-domain").value.trim();
        const emailSender = document.getElementById("edit-source-email").value.trim();

        const saveBtn = this;
        saveBtn.disabled = true;
        saveBtn.textContent = "Saving...";

        try {
            const res = await fetch(`/api/sources/${id}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: name, domain: domain, email_sender: emailSender }),
            });
            const json = await res.json();
            if (!res.ok) {
                showToast(json.detail || "Failed to save", "error");
                saveBtn.disabled = false;
                saveBtn.textContent = "Save";
                return;
            }

            const idx = sourcesData.findIndex((s) => s.id === id);
            if (idx !== -1) sourcesData[idx] = { ...sourcesData[idx], ...json.data };
            overlay.remove();
            showToast("Source updated", "success");
            renderSources();
        } catch (err) {
            showToast("Connection error", "error");
            saveBtn.disabled = false;
            saveBtn.textContent = "Save";
        }
    });
}

/* --- Merge source modal --- */

function openMergeSourceModal(id) {
    const source = sourcesData.find((s) => s.id === id);
    if (!source) return;

    const targets = sourcesData.filter((s) => s.id !== id);
    if (targets.length === 0) {
        showToast("No other source to merge into", "error");
        return;
    }

    removeSourcesModal();

    const options = targets
        .map((t) => `<option value="${t.id}">${esc(t.name)} (${esc(t.source_type)})</option>`)
        .join("");

    const overlay = document.createElement("div");
    overlay.id = "sources-modal-overlay";
    overlay.className = "settings-modal-overlay";
    overlay.innerHTML =
        '<div class="settings-modal">' +
            '<div class="settings-modal-header">' +
                `<h3>Merge "${esc(source.name)}" into...</h3>` +
                '<button class="settings-modal-close" id="sources-modal-close" title="Close">' + icon("close", { size: 15 }) + "</button>" +
            "</div>" +
            '<div class="settings-modal-body">' +
                '<div class="settings-field">' +
                    "<label>Target source</label>" +
                    `<select id="merge-target-select">${options}</select>` +
                "</div>" +
                `<p class="settings-modal-hint">All ${num(source.article_count)} article(s) from "${esc(source.name)}" will move to the target, and "${esc(source.name)}" will be deleted.</p>` +
                '<div id="merge-force-warning" class="sources-force-warning" style="display: none;"></div>' +
            "</div>" +
            '<div class="settings-modal-actions">' +
                '<button class="btn btn-ghost" id="sources-modal-cancel">Cancel</button>' +
                '<button class="btn btn-primary" id="sources-modal-save">Merge</button>' +
            "</div>" +
        "</div>";
    document.body.appendChild(overlay);

    setupModalCloseHandlers(overlay);

    // Assigned via .onclick (not addEventListener) so the 409 retry path
    // below can fully replace the handler with a force:true retry instead
    // of stacking a second listener alongside the original force:false one.
    document.getElementById("sources-modal-save").onclick = () => doMergeSource(id, false);
}

async function doMergeSource(fromId, force) {
    const targetSelect = document.getElementById("merge-target-select");
    const warningEl = document.getElementById("merge-force-warning");
    const saveBtn = document.getElementById("sources-modal-save");
    if (!targetSelect || !saveBtn) return;

    const intoId = parseInt(targetSelect.value, 10);
    saveBtn.disabled = true;
    saveBtn.textContent = "Merging...";

    try {
        const res = await fetch("/api/sources/merge", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ from_id: fromId, into_id: intoId, force: force }),
        });
        const json = await res.json();

        if (res.status === 409 && json.error === "type_mismatch") {
            const fromSource = sourcesData.find((s) => s.id === fromId);
            const intoSource = sourcesData.find((s) => s.id === intoId);
            warningEl.style.display = "block";
            warningEl.innerHTML =
                `<strong>${esc(fromSource ? fromSource.source_type : "")}</strong> and ` +
                `<strong>${esc(intoSource ? intoSource.source_type : "")}</strong> are different source types. ` +
                "Merging across types is unusual — merge anyway?";
            saveBtn.disabled = false;
            saveBtn.textContent = "Force merge";
            saveBtn.onclick = () => doMergeSource(fromId, true);
            return;
        }

        if (!res.ok) {
            showToast(json.detail || "Failed to merge", "error");
            saveBtn.disabled = false;
            saveBtn.textContent = "Merge";
            return;
        }

        removeSourcesModal();
        showToast(`Merged — ${num(json.data.moved_articles)} article(s) moved`, "success");
        loadSources();
    } catch (err) {
        showToast("Connection error", "error");
        saveBtn.disabled = false;
        saveBtn.textContent = "Merge";
    }
}

/* --- Delete source --- */

function confirmDeleteSource(id) {
    const source = sourcesData.find((s) => s.id === id);
    if (!source) return;

    const existing = document.getElementById("sources-delete-overlay");
    if (existing) existing.remove();

    const overlay = document.createElement("div");
    overlay.id = "sources-delete-overlay";
    overlay.className = "export-overlay";
    overlay.innerHTML =
        '<div class="export-dialog">' +
            "<h3>Delete source</h3>" +
            `<p>Permanently delete <strong>${esc(source.name)}</strong> and its ${num(source.article_count)} article(s) ` +
            "from your library? This cannot be undone. A backup snapshot is created automatically before deleting.</p>" +
            '<div class="export-dialog-actions">' +
                '<button class="export-cancel-btn" id="sources-delete-cancel">Cancel</button>' +
                '<button class="danger-confirm-btn" id="sources-delete-confirm">Delete</button>' +
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
    document.getElementById("sources-delete-cancel").addEventListener("click", close);
    document.getElementById("sources-delete-confirm").addEventListener("click", () => {
        close();
        deleteSource(id);
    });
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) close();
    });
    document.addEventListener("keydown", onKeydown);
}

async function deleteSource(id) {
    try {
        const res = await fetch(`/api/sources/${id}`, { method: "DELETE" });
        const json = await res.json().catch(() => ({}));
        if (!res.ok) {
            showToast(json.detail || "Failed to delete source", "error");
            return;
        }
        showToast("Source deleted", "success");
        loadSources();
    } catch (err) {
        showToast("Connection error", "error");
    }
}

/* --- Authors tab --- */

async function loadAuthors() {
    const statusEl = document.getElementById("authors-status");
    const tableEl = document.getElementById("authors-table");
    const emptyEl = document.getElementById("authors-empty");

    try {
        const res = await fetch("/api/authors");
        const json = await res.json();
        if (!json.success) throw new Error("Invalid response");

        authorsData = json.data || [];
        authorsLoaded = true;
        statusEl.style.display = "none";

        if (authorsData.length === 0) {
            tableEl.style.display = "none";
            emptyEl.style.display = "block";
            return;
        }

        emptyEl.style.display = "none";
        tableEl.style.display = "table";
        renderAuthors();
    } catch (err) {
        statusEl.innerHTML = '<p class="settings-error">Failed to load authors.</p>';
    }
}

function renderAuthors() {
    const tbody = document.getElementById("authors-tbody");
    tbody.innerHTML = authorsData.map(authorRowHtml).join("");

    tbody.querySelectorAll("[data-action]").forEach((el) => {
        const id = parseInt(el.dataset.id, 10);
        const action = el.dataset.action;
        el.addEventListener("click", () => {
            if (action === "vip") toggleAuthorVip(id);
            else if (action === "merge") openMergeAuthorModal(id);
        });
    });
}

function authorRowHtml(a) {
    const vipClass = a.is_vip ? "icon-btn sources-vip-btn active" : "icon-btn sources-vip-btn";
    return `
        <tr>
            <td data-label="Name">${esc(a.name || "Unknown")}</td>
            <td class="sources-col-count" data-label="Articles">${num(a.article_count)}</td>
            <td class="sources-col-vip" data-label="VIP">
                <button type="button" class="${vipClass}" data-action="vip" data-id="${a.id}" title="Toggle VIP">${icon("star", { size: 15 })}</button>
            </td>
            <td class="sources-col-actions" data-label="Actions">
                <div class="sources-row-actions">
                    <button type="button" class="btn btn-ghost" data-action="merge" data-id="${a.id}">Merge</button>
                </div>
            </td>
        </tr>
    `;
}

async function toggleAuthorVip(id) {
    try {
        const res = await fetch(`/api/authors/${id}/vip`, { method: "PATCH" });
        const json = await res.json();
        if (!res.ok || !json.success) throw new Error("Failed");

        const author = authorsData.find((a) => a.id === id);
        if (author) author.is_vip = json.data.is_vip;
        renderAuthors();
    } catch (err) {
        showToast("Failed to update VIP status", "error");
    }
}

function openMergeAuthorModal(id) {
    const author = authorsData.find((a) => a.id === id);
    if (!author) return;

    const targets = authorsData.filter((a) => a.id !== id);
    if (targets.length === 0) {
        showToast("No other author to merge into", "error");
        return;
    }

    removeSourcesModal();

    const options = targets.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");

    const overlay = document.createElement("div");
    overlay.id = "sources-modal-overlay";
    overlay.className = "settings-modal-overlay";
    overlay.innerHTML =
        '<div class="settings-modal">' +
            '<div class="settings-modal-header">' +
                `<h3>Merge "${esc(author.name)}" into...</h3>` +
                '<button class="settings-modal-close" id="sources-modal-close" title="Close">' + icon("close", { size: 15 }) + "</button>" +
            "</div>" +
            '<div class="settings-modal-body">' +
                '<div class="settings-field">' +
                    "<label>Target author</label>" +
                    `<select id="merge-author-target-select">${options}</select>` +
                "</div>" +
                `<p class="settings-modal-hint">All ${num(author.article_count)} article(s) credited to "${esc(author.name)}" will be re-credited to the target, and "${esc(author.name)}" will be removed.</p>` +
            "</div>" +
            '<div class="settings-modal-actions">' +
                '<button class="btn btn-ghost" id="sources-modal-cancel">Cancel</button>' +
                '<button class="btn btn-primary" id="sources-modal-save">Merge</button>' +
            "</div>" +
        "</div>";
    document.body.appendChild(overlay);

    setupModalCloseHandlers(overlay);

    document.getElementById("sources-modal-save").addEventListener("click", async function () {
        const targetSelect = document.getElementById("merge-author-target-select");
        const keepId = parseInt(targetSelect.value, 10);

        const saveBtn = this;
        saveBtn.disabled = true;
        saveBtn.textContent = "Merging...";

        try {
            const res = await fetch("/api/authors/merge", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ keep_id: keepId, merge_id: id }),
            });
            const json = await res.json();
            if (!res.ok) {
                showToast(json.detail || "Failed to merge", "error");
                saveBtn.disabled = false;
                saveBtn.textContent = "Merge";
                return;
            }

            removeSourcesModal();
            showToast("Authors merged", "success");
            loadAuthors();
        } catch (err) {
            showToast("Connection error", "error");
            saveBtn.disabled = false;
            saveBtn.textContent = "Merge";
        }
    });
}

/* --- Shared modal helpers --- */

function removeSourcesModal() {
    const existing = document.getElementById("sources-modal-overlay");
    if (existing) existing.remove();
}

function setupModalCloseHandlers(overlay) {
    function onKeydown(e) {
        if (e.key === "Escape") close();
    }
    function close() {
        overlay.remove();
        document.removeEventListener("keydown", onKeydown);
    }
    document.getElementById("sources-modal-close").addEventListener("click", close);
    document.getElementById("sources-modal-cancel").addEventListener("click", close);
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) close();
    });
    document.addEventListener("keydown", onKeydown);
}

/* --- Keyboard --- */

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

        // Don't capture while any modal/dialog is open (they handle their own Escape)
        if (document.getElementById("sources-modal-overlay") || document.getElementById("sources-delete-overlay")) {
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
                showSourcesShortcuts();
                break;
        }
    });

    const closeBtn = document.getElementById("shortcuts-close");
    if (closeBtn) {
        closeBtn.addEventListener("click", () => {
            document.getElementById("shortcuts-overlay").style.display = "none";
        });
    }
    const overlayEl = document.getElementById("shortcuts-overlay");
    if (overlayEl) {
        overlayEl.addEventListener("click", (e) => {
            if (e.target === overlayEl) overlayEl.style.display = "none";
        });
    }
}

function showSourcesShortcuts() {
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
