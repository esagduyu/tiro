/* Tiro — digest module (M2.0 split of app.js, Task 2).
 *
 * Owns the /digest page only: ranked/by-topic/by-entity tabs, digest
 * generation + the staleness banner, digest history, and the digest
 * schedule modal. Loaded as `<script type="module">` from digest.html only.
 *
 * This was split out from inbox.js (rather than kept entangled, which the
 * plan allowed as a documented fallback) because the two turned out not to
 * be entangled at all in the current templates: digest and inbox moved to
 * separate routes (/digest vs /inbox) back in Checkpoint 22's UX redesign,
 * and app.js's shared top-of-file state/DOMContentLoaded block was really
 * just historical residue from when both lived on one page behind
 * `.view-tab`s. See .superpowers/sdd/task-2-report.md for the full
 * identifier audit, including two now-dead app.js branches this split
 * exposed (both left out of inbox.js, not carried into this file either,
 * since `.view-tab` / `.digest-tab` never coexist with `#article-list` in
 * any current template):
 *   - `setupViewTabs()` and the `#view-articles`/`#view-digest` toggle.
 *   - The inbox "r" key's `if (document.querySelector(".digest-tab"))
 *     loadDigest(...)` branch (dead on /inbox; the real "r"-regenerates-
 *     digest behavior lives here, wired directly to the digest-refresh
 *     button, and was never reachable via the shared inbox keydown handler
 *     in the first place — `setupKeyboard()` in the old app.js bailed before
 *     attaching any listener on /digest since it required `#article-list`).
 *
 * The one inline `onclick="loadDigest(true)"` handler that lived in
 * digest.html (the "Try again" button in the error state) is converted to
 * addEventListener below (`#digest-retry-btn`), along with the equivalent
 * handler that updateDigestBanner() used to inject as an onclick= string in
 * its innerHTML — both were the plan's "exactly one inline on*-handler
 * across templates" reference point.
 */

import { renderMarkdown, timeAgo } from "./core.js";

let digestData = null; // cached digest response
let digestLoaded = false;
let digestGenerating = false; // in-flight guard so rapid r-presses/clicks can't fire concurrent POSTs

/* ---- Digest sub-tabs (Ranked / By Topic / By Entity) ---- */

function setupDigestTabs() {
    document.querySelectorAll(".digest-tab").forEach((tab) => {
        tab.addEventListener("click", () => {
            document.querySelectorAll(".digest-tab").forEach((t) => t.classList.remove("active"));
            tab.classList.add("active");

            const type = tab.dataset.type;
            document.querySelectorAll(".digest-section").forEach((s) => (s.style.display = "none"));
            const section = document.getElementById(`digest-${type.replace("_", "-")}`);
            if (section) section.style.display = "block";
        });
    });

    // Refresh button
    const refreshBtn = document.getElementById("digest-refresh");
    if (refreshBtn) {
        refreshBtn.addEventListener("click", () => loadDigest(true));
    }

    // Retry button in the error state (was an inline onclick= in the template)
    const retryBtn = document.getElementById("digest-retry-btn");
    if (retryBtn) {
        retryBtn.addEventListener("click", () => loadDigest(true));
    }

    // History dropdown
    const historySelect = document.getElementById("digest-history");
    if (historySelect) {
        loadDigestHistory();
        historySelect.addEventListener("change", () => {
            const val = historySelect.value;
            if (val === "today") {
                loadDigest(false);
            } else {
                loadHistoricalDigest(val);
            }
        });
    }

    // Schedule button + modal
    const scheduleBtn = document.getElementById("digest-schedule-btn");
    if (scheduleBtn) {
        scheduleBtn.addEventListener("click", showDigestScheduleModal);
        loadDigestScheduleState();
    }
    const scheduleClose = document.getElementById("digest-schedule-close");
    if (scheduleClose) {
        scheduleClose.addEventListener("click", hideDigestScheduleModal);
    }
    const scheduleOverlay = document.getElementById("digest-schedule-overlay");
    if (scheduleOverlay) {
        scheduleOverlay.addEventListener("click", (e) => {
            if (e.target === scheduleOverlay) hideDigestScheduleModal();
        });
    }
    const scheduleSaveBtn = document.getElementById("schedule-save-btn");
    if (scheduleSaveBtn) {
        scheduleSaveBtn.addEventListener("click", saveDigestSchedule);
    }
}

/* ---- Load digest ---- */

async function loadDigest(refresh) {
    if (digestGenerating) return;
    digestGenerating = true;

    const loadingEl = document.getElementById("digest-loading");
    const errorEl = document.getElementById("digest-error");
    const contentEl = document.getElementById("digest-content");
    const emptyEl = document.getElementById("digest-empty");
    const refreshBtn = document.getElementById("digest-refresh");

    loadingEl.style.display = "block";
    errorEl.style.display = "none";
    contentEl.style.display = "none";
    emptyEl.style.display = "none";
    if (refreshBtn) refreshBtn.disabled = true;

    let phase = refresh ? "generate" : "load";
    try {
        // GET is a pure cache read; generation is POST (M4b). A 404 means
        // no digest exists yet — generate one, preserving first-visit UX.
        let res = refresh ? null : await fetch("/api/digest/today");
        if (!res || res.status === 404) {
            phase = "generate";
            res = await fetch("/api/digest/today", { method: "POST" });
        }

        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${res.status}`);
        }

        const json = await res.json();

        if (!json.success || !json.data) {
            throw new Error("Invalid response");
        }

        digestData = json.data;
        digestLoaded = true;

        // Render each section
        renderDigestSection("ranked", digestData.ranked);
        renderDigestSection("by_topic", digestData.by_topic);
        renderDigestSection("by_entity", digestData.by_entity);

        // Show time-ago banner
        updateDigestBanner(digestData);

        loadingEl.style.display = "none";
        contentEl.style.display = "block";

        // Show the active tab's section
        const activeTab = document.querySelector(".digest-tab.active");
        if (activeTab) {
            const type = activeTab.dataset.type;
            document.querySelectorAll(".digest-section").forEach((s) => (s.style.display = "none"));
            const section = document.getElementById(`digest-${type.replace("_", "-")}`);
            if (section) section.style.display = "block";
        }

        // Refresh history dropdown (new digest may have been generated)
        loadDigestHistory();

        // Reset history select to "Today"
        const historySelect = document.getElementById("digest-history");
        if (historySelect) historySelect.value = "today";
    } catch (err) {
        console.error("Digest load failed:", err);
        loadingEl.style.display = "none";
        document.getElementById("digest-error-msg").textContent =
            phase === "load"
                ? `Failed to load digest: ${err.message}`
                : `Failed to generate digest: ${err.message}`;
        errorEl.style.display = "block";
    } finally {
        digestGenerating = false;
        if (refreshBtn) refreshBtn.disabled = false;
    }
}

function renderDigestSection(type, data) {
    const elId = `digest-${type.replace("_", "-")}`;
    const el = document.getElementById(elId);
    if (!el || !data) return;

    const content = data.content || "";
    el.innerHTML = renderMarkdown(content);

    // Make article links work (they're /articles/ID)
    el.querySelectorAll("a").forEach((link) => {
        const href = link.getAttribute("href");
        // Internal article links — keep as-is, they already point to /articles/{id}
        if (href && href.startsWith("/articles/")) {
            link.addEventListener("click", (e) => {
                e.preventDefault();
                // Mark as read
                const id = href.split("/articles/")[1];
                fetch(`/api/articles/${id}/read`, { method: "PATCH" }).catch(() => {});
                window.location.href = href;
            });
        } else if (href && (href.startsWith("http://") || href.startsWith("https://"))) {
            link.target = "_blank";
            link.rel = "noopener noreferrer";
        }
    });
}

function updateDigestBanner(data) {
    const banner = document.getElementById("digest-banner");
    if (!banner) return;

    // Get created_at from any section (they're all generated together)
    const section = data.ranked || data.by_topic || data.by_entity;
    if (!section || !section.created_at) {
        banner.style.display = "none";
        return;
    }

    const then = new Date(section.created_at.replace(" ", "T"));
    const diffHr = (new Date() - then) / 3600000;
    const stale = diffHr >= 24;
    const ago = timeAgo(then);

    banner.className = stale ? "digest-banner digest-banner-stale" : "digest-banner";
    banner.innerHTML = stale
        ? `Digest is ${ago} old — new articles may not be included. <button class="digest-refresh-inline">Regenerate now</button>`
        : `Generated ${ago} <button class="digest-refresh-inline">Regenerate</button>`;
    banner.style.display = "flex";

    // Was an inline onclick="loadDigest(true)" in the pre-module version —
    // converted to addEventListener since this HTML is injected fresh every
    // call (the button element itself is always a new node here).
    banner.querySelector(".digest-refresh-inline")?.addEventListener("click", () => loadDigest(true));
}

/* ---- Digest history ---- */

async function loadDigestHistory() {
    const select = document.getElementById("digest-history");
    if (!select) return;
    try {
        const res = await fetch("/api/digest/history");
        const json = await res.json();
        if (!json.success) return;

        // Clear all but "Today"
        while (select.options.length > 1) select.remove(1);

        const today = new Date().toISOString().slice(0, 10);
        for (const entry of json.data) {
            if (entry.date === today) continue; // skip today (already shown)
            const opt = document.createElement("option");
            opt.value = entry.date;
            opt.textContent = formatDigestDate(entry.date);
            select.appendChild(opt);
        }
    } catch (e) {
        console.error("Failed to load digest history:", e);
    }
}

function formatDigestDate(isoDate) {
    const d = new Date(isoDate + "T12:00:00"); // noon to avoid timezone issues
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

async function loadHistoricalDigest(dateStr) {
    const loadingEl = document.getElementById("digest-loading");
    const errorEl = document.getElementById("digest-error");
    const contentEl = document.getElementById("digest-content");
    const emptyEl = document.getElementById("digest-empty");
    const refreshBtn = document.getElementById("digest-refresh");

    loadingEl.style.display = "block";
    errorEl.style.display = "none";
    contentEl.style.display = "none";
    emptyEl.style.display = "none";
    if (refreshBtn) refreshBtn.disabled = true;

    try {
        const res = await fetch(`/api/digest/date/${dateStr}`);
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${res.status}`);
        }

        const json = await res.json();
        if (!json.success || !json.data) throw new Error("Invalid response");

        digestData = json.data;

        renderDigestSection("ranked", digestData.ranked);
        renderDigestSection("by_topic", digestData.by_topic);
        renderDigestSection("by_entity", digestData.by_entity);
        updateDigestBanner(digestData);

        loadingEl.style.display = "none";
        contentEl.style.display = "block";

        const activeTab = document.querySelector(".digest-tab.active");
        if (activeTab) {
            const type = activeTab.dataset.type;
            document.querySelectorAll(".digest-section").forEach((s) => (s.style.display = "none"));
            const section = document.getElementById(`digest-${type.replace("_", "-")}`);
            if (section) section.style.display = "block";
        }
    } catch (err) {
        console.error("Historical digest load failed:", err);
        loadingEl.style.display = "none";
        document.getElementById("digest-error-msg").textContent =
            `Failed to load digest for ${formatDigestDate(dateStr)}: ${err.message}`;
        errorEl.style.display = "block";
    } finally {
        if (refreshBtn) refreshBtn.disabled = false;
    }
}

/* ---- Digest schedule ---- */

async function loadDigestScheduleState() {
    try {
        const res = await fetch("/api/settings/digest-schedule");
        const json = await res.json();
        if (!json.success) return;
        const btn = document.getElementById("digest-schedule-btn");
        if (btn && json.data.enabled) {
            btn.classList.add("active");
            btn.title = `Digest scheduled daily at ${json.data.time}`;
        }
    } catch (e) {
        // ignore
    }
}

async function showDigestScheduleModal() {
    const overlay = document.getElementById("digest-schedule-overlay");
    if (!overlay) return;

    // Load current settings
    try {
        const res = await fetch("/api/settings/digest-schedule");
        const json = await res.json();
        if (json.success) {
            document.getElementById("schedule-enabled").checked = json.data.enabled;
            document.getElementById("schedule-time").value = json.data.time;
            document.getElementById("schedule-unread-only").checked = json.data.unread_only;

            const statusEl = document.getElementById("schedule-email-status");
            if (statusEl) {
                statusEl.textContent = json.data.email_configured
                    ? "Email configured — digests will be emailed automatically"
                    : "No email configured — digests will be generated but not emailed";
            }
        }
    } catch (e) {
        // use defaults
    }

    overlay.style.display = "flex";
}

function hideDigestScheduleModal() {
    const overlay = document.getElementById("digest-schedule-overlay");
    if (overlay) overlay.style.display = "none";
}

async function saveDigestSchedule() {
    const enabled = document.getElementById("schedule-enabled").checked;
    const time = document.getElementById("schedule-time").value;
    const unreadOnly = document.getElementById("schedule-unread-only").checked;
    const tzOffset = new Date().getTimezoneOffset();

    const btn = document.getElementById("schedule-save-btn");
    if (btn) btn.disabled = true;

    try {
        const res = await fetch("/api/settings/digest-schedule", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                enabled,
                time,
                unread_only: unreadOnly,
                timezone_offset: tzOffset,
            }),
        });
        const json = await res.json();
        if (!res.ok) throw new Error(json.detail || "Save failed");

        hideDigestScheduleModal();

        // Update button state
        const scheduleBtn = document.getElementById("digest-schedule-btn");
        if (scheduleBtn) {
            if (enabled) {
                scheduleBtn.classList.add("active");
                scheduleBtn.title = `Digest scheduled daily at ${time}`;
            } else {
                scheduleBtn.classList.remove("active");
                scheduleBtn.title = "Schedule daily digest";
            }
        }
    } catch (e) {
        alert("Failed to save schedule: " + e.message);
    } finally {
        if (btn) btn.disabled = false;
    }
}

/* ---- Init ---- */

document.addEventListener("DOMContentLoaded", () => {
    if (!document.querySelector(".digest-tab")) return;
    setupDigestTabs();
    if (!digestLoaded) {
        loadDigest(false);
    }
});
