/* Tiro — /setup/remote wizard (Phase 3 M3.1 Task 4).
 *
 * LEAF entry module (nothing imports it), same pattern as wiki.js/sources.js:
 * loaded via a versioned <script type="module"> tag in remote_setup.html, so
 * no importmap entry is needed.
 *
 * Every server-derived string (magicdns_name, remote_url, error text) is run
 * through `esc()` before landing in innerHTML — a MagicDNS name/remote URL
 * are attacker-adjacent in principle (this is the wizard for exposing Tiro
 * off the local machine), so treat them the same as any other untrusted
 * server string per CLAUDE.md's sanitize/escape invariant.
 */

import { apiFetch, esc, showToast } from "./core.js";

document.addEventListener("DOMContentLoaded", () => {
    if (!document.getElementById("remote-tailscale-status")) return;
    loadRemoteStatus();
    setupRemoteForm();
});

async function loadRemoteStatus() {
    const statusEl = document.getElementById("remote-tailscale-status");
    const result = await apiFetch("/api/remote/status");
    if (!result.success) {
        statusEl.innerHTML = '<p class="settings-error">Failed to check Tailscale status.</p>';
        return;
    }
    renderTailscaleStatus(result.data);
    prefillRemoteUrl(result.data);
}

function renderTailscaleStatus(data) {
    const statusEl = document.getElementById("remote-tailscale-status");
    let html = '<div class="status-card">';

    if (data.tailscale_installed) {
        html += '<div class="status-card-header"><span class="status-indicator status-ok"></span><strong>Tailscale detected</strong></div>';
        if (data.magicdns_name) {
            html += '<div class="status-detail">MagicDNS name: <code>' + esc(data.magicdns_name) + '</code></div>';
        } else {
            html += '<div class="status-detail muted">Could not read your MagicDNS name &mdash; is <code>tailscale up</code> running?</div>';
        }
        if (data.serve_command) {
            html += '<div class="status-detail remote-serve-command">';
            html += '<code id="serve-command-text">' + esc(data.serve_command) + '</code>';
            html += '<button type="button" class="settings-configure-btn settings-configure-btn-secondary" id="btn-copy-serve-command">Copy</button>';
            html += '</div>';
            html += '<div class="status-detail muted">Run this once to expose Tiro over HTTPS via Tailscale Serve.</div>';
        }
    } else {
        html += '<div class="status-card-header"><span class="status-indicator status-off"></span><strong>Tailscale not detected</strong></div>';
        html += '<div class="status-detail"><a href="https://tailscale.com/download" target="_blank" rel="noopener noreferrer">Install Tailscale &rarr;</a></div>';
        html += '<div class="status-detail muted">Or configure a Remote URL below manually if you use a different reverse proxy.</div>';
    }

    html += '</div>';
    statusEl.innerHTML = html;

    const copyBtn = document.getElementById("btn-copy-serve-command");
    if (copyBtn) {
        copyBtn.addEventListener("click", () => {
            navigator.clipboard.writeText(data.serve_command).then(() => {
                copyBtn.textContent = "Copied!";
                setTimeout(() => { copyBtn.textContent = "Copy"; }, 1500);
            });
        });
    }
}

function prefillRemoteUrl(data) {
    const input = document.getElementById("remote-url-input");
    if (!input || input.value) return;
    if (data.remote_url) {
        input.value = data.remote_url;
    } else if (data.magicdns_name) {
        input.value = "https://" + data.magicdns_name;
    }
}

function setupRemoteForm() {
    const form = document.getElementById("remote-url-form");
    if (!form) return;

    form.addEventListener("submit", (e) => {
        e.preventDefault();
        saveRemoteUrl();
    });

    const testBtn = document.getElementById("btn-test-remote-url");
    testBtn.addEventListener("click", () => {
        testRemoteUrl();
    });
}

async function saveRemoteUrl() {
    const input = document.getElementById("remote-url-input");
    const allowCheckbox = document.getElementById("remote-url-allow-hostname");
    const saveBtn = document.getElementById("btn-save-remote-url");
    const url = input.value.trim();
    if (!url) return;

    saveBtn.disabled = true;
    const result = await apiFetch("/api/remote/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ remote_url: url, allow_hostname: allowCheckbox.checked }),
    });
    saveBtn.disabled = false;

    if (!result.success) {
        showToast(result.error || "Failed to save remote URL", "error");
        return;
    }
    showToast("Remote URL saved", "success");
}

async function testRemoteUrl() {
    const input = document.getElementById("remote-url-input");
    const resultEl = document.getElementById("remote-test-result");
    const testBtn = document.getElementById("btn-test-remote-url");
    const url = input.value.trim() || null;

    resultEl.style.display = "";
    resultEl.innerHTML = '<div class="settings-loading"><div class="digest-spinner"></div><p>Testing...</p></div>';
    testBtn.disabled = true;

    const result = await apiFetch("/api/remote/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
    });

    testBtn.disabled = false;

    if (!result.success) {
        resultEl.innerHTML = '<p class="settings-error">' + esc(result.error || "Test failed") + '</p>';
        return;
    }
    renderTestResult(result.data);
}

function renderTestResult(data) {
    const resultEl = document.getElementById("remote-test-result");
    if (data.ok) {
        resultEl.innerHTML =
            '<div class="status-card"><div class="status-card-header">' +
            '<span class="status-indicator status-ok"></span><strong>Reachable</strong></div>' +
            '<div class="status-detail">HTTP ' + esc(String(data.status_code)) + ' &middot; ' +
            esc(String(data.latency_ms)) + 'ms</div></div>';
    } else {
        resultEl.innerHTML =
            '<div class="status-card"><div class="status-card-header">' +
            '<span class="status-indicator status-off"></span><strong>Unreachable</strong></div>' +
            '<div class="status-detail">' + esc(data.error || "Unknown error") + '</div></div>';
    }
}
