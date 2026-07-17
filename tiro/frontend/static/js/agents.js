/* Tiro — Agent runtime page (Phase 6 K2).
 *
 * Per-page LEAF entry module (M2.0 conventions): imports esc/num/showToast/
 * timeAgo/confirmDialog from core.js and icon from icons.js via the import
 * map (mirrors feeds.js); nothing imports agents.js, so it keeps the normal
 * `?v={{ static_v }}` cache-bust query in agents.html's <script type="module">
 * tag.
 *
 * esc() discipline: every server-derived string (agent names, tiers,
 * providers, errors, trace args/results) passes through esc() at its
 * innerHTML sink; numbers through num(). timeAgo() takes a Date, not a raw
 * string — server timestamps are SQL `%Y-%m-%d %H:%M:%S` (space-separated,
 * no "T"), so every timeAgo() call goes through the Safari-safe
 * fmtTimeAgo() helper below (same pattern as feeds.js's lastFetchedText()).
 */

import { esc, num, showToast, timeAgo, confirmDialog, renderMarkdown } from "./core.js";
import { icon } from "./icons.js";

const PAGE_SIZE = 50;
let runsOffset = 0;
let runsTotal = 0;

document.addEventListener("DOMContentLoaded", () => {
    setupFilters();
    setupKeyboard();
    loadAgents();
    loadRuns();
    loadSuggestions();
    loadPersonas();
});

function fmtTimeAgo(ts) {
    if (!ts) return "—";
    const then = new Date(String(ts).replace(" ", "T"));
    if (isNaN(then.getTime())) return "—";
    return esc(timeAgo(then));
}

function statusPill(status) {
    const cls = status === "ok" ? "agents-pill-ok"
        : status === "error" ? "agents-pill-error" : "agents-pill-running";
    return `<span class="pill ${cls}">${esc(status)}</span>`;
}

function fmtCost(c) {
    if (c === null || c === undefined) return "—";
    return `$${Number(c).toFixed(4)}`;
}

/* --- Agent cards ---------------------------------------------------------- */

async function loadAgents() {
    const el = document.getElementById("agents-list");
    try {
        const res = await fetch("/api/agents");
        const json = await res.json();
        const agents = json.data || [];
        el.innerHTML = agents.map((a) => `
            <div class="agents-card" data-agent="${esc(a.name)}">
                <div class="agents-card-head">
                    ${icon("zap", { size: 16 })}
                    <strong>${esc(a.name)}</strong>
                    <span class="pill">${esc(a.tier)}</span>
                    <span class="agents-version">v${esc(a.version)}</span>
                </div>
                <div class="agents-card-last">
                    ${a.last_run
                        ? `${statusPill(a.last_run.status)}
                           <span>${fmtTimeAgo(a.last_run.started_at)}</span>
                           <span>${fmtCost(a.last_run.cost_usd)}</span>`
                        : '<span class="agents-never">never run</span>'}
                </div>
            </div>`).join("");
        const sel = document.getElementById("runs-agent-filter");
        sel.innerHTML = '<option value="">All agents</option>' + agents.map(
            (a) => `<option value="${esc(a.name)}">${esc(a.name)}</option>`
        ).join("");
    } catch {
        el.innerHTML = '<p class="settings-error">Failed to load agents.</p>';
    }
}

/* --- Runs table ------------------------------------------------------------ */

function runFilters() {
    const agent = document.getElementById("runs-agent-filter").value;
    const status = document.getElementById("runs-status-filter").value;
    const p = new URLSearchParams({ limit: PAGE_SIZE, offset: runsOffset });
    if (agent) p.set("agent", agent);
    if (status) p.set("status", status);
    return p;
}

async function loadRuns() {
    const list = document.getElementById("runs-list");
    const empty = document.getElementById("runs-empty");
    try {
        const res = await fetch(`/api/agents/runs?${runFilters()}`);
        const { runs, total } = (await res.json()).data;
        runsTotal = total;
        if (total === 0) {
            list.innerHTML = "";
            empty.style.display = "block";
        } else {
            empty.style.display = "none";
            list.innerHTML = runs.map((r) => `
                <button type="button" class="agents-run-row" data-uid="${esc(r.run_uid)}">
                    ${statusPill(r.status)}
                    <span class="agents-run-name">${esc(r.agent_name)}</span>
                    <span class="agents-run-when">${fmtTimeAgo(r.started_at)}</span>
                    <span class="agents-run-tokens">${num(r.tokens_in ?? 0)}→${num(r.tokens_out ?? 0)} tok</span>
                    <span class="agents-run-cost">${fmtCost(r.cost_usd)}</span>
                </button>`).join("");
            list.querySelectorAll(".agents-run-row").forEach((btn) =>
                btn.addEventListener("click", () => openRun(btn.dataset.uid)));
        }
        renderPager();
    } catch {
        list.innerHTML = '<p class="settings-error">Failed to load runs.</p>';
    }
}

function renderPager() {
    const prev = document.getElementById("runs-prev");
    const next = document.getElementById("runs-next");
    const label = document.getElementById("runs-page-label");
    prev.disabled = runsOffset === 0;
    next.disabled = runsOffset + PAGE_SIZE >= runsTotal;
    label.textContent = runsTotal
        ? `${runsOffset + 1}–${Math.min(runsOffset + PAGE_SIZE, runsTotal)} of ${runsTotal}`
        : "";
}

function setupFilters() {
    document.getElementById("runs-agent-filter").addEventListener("change", () => {
        runsOffset = 0; loadRuns();
    });
    document.getElementById("runs-status-filter").addEventListener("change", () => {
        runsOffset = 0; loadRuns();
    });
    document.getElementById("runs-prev").addEventListener("click", () => {
        runsOffset = Math.max(0, runsOffset - PAGE_SIZE); loadRuns();
    });
    document.getElementById("runs-next").addEventListener("click", () => {
        runsOffset += PAGE_SIZE; loadRuns();
    });
}

/* --- Run detail + trace viewer (OPEN decision 9) --------------------------- */

async function openRun(uid) {
    const panel = document.getElementById("run-detail");
    panel.style.display = "block";
    panel.innerHTML = '<div class="settings-loading"><div class="digest-spinner"></div></div>';
    try {
        const res = await fetch(`/api/agents/runs/${encodeURIComponent(uid)}`);
        if (!res.ok) throw new Error("not found");
        const run = (await res.json()).data;
        panel.innerHTML = `
            <div class="agents-detail-head">
                <h3>${esc(run.agent_name)} ${statusPill(run.status)}</h3>
                <div class="agents-detail-actions">
                    <button type="button" class="btn btn-primary" id="run-replay-btn">
                        ${icon("refresh", { size: 15 })} Replay
                    </button>
                    <button type="button" class="btn btn-ghost" id="run-detail-close">Close</button>
                </div>
            </div>
            <dl class="agents-detail-meta">
                <dt>Run</dt><dd>${esc(run.run_uid)}</dd>
                <dt>Provider</dt><dd>${esc(run.provider || "—")} / ${esc(run.model || "—")}</dd>
                <dt>Started</dt><dd>${esc(run.started_at || "—")}</dd>
                <dt>Tokens</dt><dd>${num(run.tokens_in ?? 0)} in / ${num(run.tokens_out ?? 0)} out</dd>
                <dt>Cost</dt><dd>${fmtCost(run.cost_usd)}</dd>
                ${run.replay_of ? `<dt>Replay of</dt><dd>${esc(run.replay_of)}</dd>` : ""}
                ${run.error ? `<dt>Error</dt><dd class="agents-error">${esc(run.error)}</dd>` : ""}
                <dt>Citations</dt><dd>${num((run.citations || []).length)} article(s)</dd>
            </dl>
            <div id="run-trace" class="agents-trace">
                ${run.trace_available ? "" : '<p class="agents-never">Trace expired (pruned by retention).</p>'}
            </div>`;
        document.getElementById("run-detail-close").addEventListener("click", () => {
            panel.style.display = "none";
        });
        document.getElementById("run-replay-btn").addEventListener("click", () =>
            replayRun(run));
        if (run.trace_available) loadTrace(uid);
    } catch {
        panel.innerHTML = '<p class="settings-error">Failed to load run.</p>';
    }
}

async function loadTrace(uid) {
    const el = document.getElementById("run-trace");
    try {
        const res = await fetch(`/api/agents/runs/${encodeURIComponent(uid)}?trace=1`);
        if (!res.ok) throw new Error("expired");
        const events = (await res.text()).split("\n").filter(Boolean).map((l) => JSON.parse(l));
        el.innerHTML = events.map((ev) => {
            if (ev.kind === "run") {
                return `<details open class="agents-trace-event">
                    <summary><span class="pill">run</span> ${esc(ev.agent)} v${esc(ev.version)}
                        ${ev.replay_of ? `(replay of ${esc(ev.replay_of)})` : ""}</summary>
                    <pre>${esc(JSON.stringify(ev.inputs, null, 2))}</pre>
                </details>`;
            }
            const cost = ev.cost_usd !== undefined ? ` · ${fmtCost(ev.cost_usd)}` : "";
            const toks = ev.tokens_in !== undefined
                ? ` · ${num(ev.tokens_in)}→${num(ev.tokens_out ?? 0)} tok` : "";
            const body = ev.truncated
                ? `${esc(ev.result_preview)}\n… (truncated — ${esc(ev.result_digest)})`
                : esc(ev.result ?? "");
            return `<details class="agents-trace-event">
                <summary>#${num(ev.seq)} <span class="pill">${esc(ev.kind)}</span> ${esc(ev.name)}${toks}${cost}</summary>
                <h4>args</h4><pre>${esc(JSON.stringify(ev.args, null, 2))}</pre>
                <h4>result</h4><pre>${body}</pre>
            </details>`;
        }).join("");
    } catch {
        el.innerHTML = '<p class="agents-never">Trace unavailable.</p>';
    }
}

async function replayRun(run) {
    const costNote = run.cost_usd != null
        ? `Original run cost ${esc(fmtCost(run.cost_usd))} — actual cost may vary.`
        : "Cost estimate unavailable — actual cost may vary.";
    const ok = await confirmDialog(
        `Re-executes ${esc(run.agent_name)} live with fresh reads. ${costNote}`,
        { title: "Replay this run?", confirmText: "Replay", cancelText: "Cancel", danger: false }
    );
    if (!ok) return;
    try {
        const res = await fetch(
            `/api/agents/runs/${encodeURIComponent(run.run_uid)}/replay`,
            { method: "POST", headers: { "Content-Type": "application/json" },
              body: "{}" });
        const json = await res.json();
        if (!res.ok) throw new Error(json.detail || "replay failed");
        showToast(json.success ? "Replay finished." : `Replay failed: ${json.error}`,
                  json.success ? "success" : "error");
        runsOffset = 0;
        loadRuns();
        loadAgents();
        if (json.data && json.data.run_uid) openRun(json.data.run_uid);
    } catch {
        showToast("Replay failed.", "error");
    }
}

/* --- Keyboard (settings-adjacent map, feeds.js pattern) --------------------- */

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
        if (document.getElementById("core-confirm-overlay")) return;

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
            case "Escape": {
                e.preventDefault();
                const panel = document.getElementById("run-detail");
                if (panel && panel.style.display !== "none") {
                    panel.style.display = "none";
                    return;
                }
                window.location.href = "/inbox";
                break;
            }
            case "?":
                e.preventDefault();
                showAgentsShortcuts();
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

function showAgentsShortcuts() {
    const overlay = document.getElementById("shortcuts-overlay");
    const body = document.getElementById("shortcuts-body");
    if (!overlay || !body) return;

    const shortcuts = [
        { section: "Navigation" },
        { keys: ["b", "Esc"], desc: "Back to inbox (or close run detail)" },
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

/* --- Suggestions queue (K3) ------------------------------------------------ */

async function loadSuggestions() {
    const queue = document.getElementById("suggestions-queue");
    const empty = document.getElementById("suggestions-empty");
    try {
        const res = await fetch("/api/suggestions?status=pending");
        const rows = (await res.json()).data.suggestions || [];
        empty.style.display = rows.length ? "none" : "";
        queue.querySelectorAll(".suggestion-card").forEach((n) => n.remove());
        for (const s of rows) queue.appendChild(suggestionCard(s));
    } catch {
        showToast("Failed to load suggestions", "error");
    }
}

function suggestionCard(s) {
    const card = document.createElement("div");
    card.className = "suggestion-card callout";
    card.dataset.uid = s.uid;
    const body = s.payload.markdown
        ? renderMarkdown(s.payload.markdown)
        : `<code>${esc(JSON.stringify(s.payload))}</code>`;
    card.innerHTML = `
        <div class="suggestion-head">
            ${icon("message", { size: 16 })}
            <strong>${esc(s.persona)}</strong>
            <span class="pill">${esc(s.kind)}</span>
            <span class="suggestion-when">${fmtTimeAgo(s.created_at)}</span>
        </div>
        <div class="suggestion-body">${body}</div>
        <div class="suggestion-actions">
            <button type="button" class="btn btn-primary" data-sugg-accept>Apply</button>
            <button type="button" class="btn btn-ghost" data-sugg-dismiss>Dismiss</button>
        </div>`;
    card.querySelector("[data-sugg-accept]").addEventListener("click",
        () => resolveSuggestion(s.uid, "accept", card));
    card.querySelector("[data-sugg-dismiss]").addEventListener("click",
        () => resolveSuggestion(s.uid, "dismiss", card));
    return card;
}

async function resolveSuggestion(uid, action, card) {
    try {
        const res = await fetch(`/api/suggestions/${encodeURIComponent(uid)}/${action}`,
            { method: "POST" });
        if (!res.ok) {
            const detail = (await res.json()).detail || "failed";
            showToast(`Couldn't ${action}: ${detail}`, "error");
            return;
        }
        card.remove();
        showToast(action === "accept" ? "Suggestion applied" : "Dismissed");
        loadSuggestions();
    } catch {
        showToast("Network error", "error");
    }
}

/* --- Persona management (K3) ----------------------------------------------- */

const SCOPE_INPUT_HINTS = {
    article: { key: "article_id", label: "Article id", coerce: Number },
    query: { key: "query", label: "Query", coerce: String },
    library: { key: "wiki_slug", label: "Wiki slug", coerce: String },
};

async function loadPersonas() {
    const el = document.getElementById("personas-list");
    try {
        const res = await fetch("/api/personas");
        const personas = (await res.json()).data || [];
        el.innerHTML = "";
        for (const p of personas) el.appendChild(personaCard(p));
    } catch {
        el.innerHTML = '<p class="settings-error">Failed to load personas.</p>';
    }
}

function personaCard(p) {
    const card = document.createElement("div");
    card.className = "agents-card persona-card";
    if (p.error) {
        card.innerHTML = `
            <div class="agents-card-head">${icon("alert", { size: 16 })}
                <strong>${esc(p.slug)}</strong>
                <span class="pill agents-pill-error">broken</span></div>
            <p class="persona-error">${esc(p.error)}</p>
            <p class="persona-hint">Fix <code>${esc(p.path)}</code> — changes apply on next run.</p>`;
        return card;
    }
    const hint = SCOPE_INPUT_HINTS[p.scope];
    card.innerHTML = `
        <div class="agents-card-head">${icon("pencil", { size: 16 })}
            <strong>${esc(p.name)}</strong>
            <span class="pill">${esc(p.scope)}</span>
            <span class="pill">${esc(p.output)}</span>
            <span class="pill">${esc(p.tier)}</span>
            <span class="agents-version">v${esc(p.version)} · ${esc(p.schedule)}</span>
        </div>
        <div class="persona-actions">
            ${hint ? `<input class="persona-input" placeholder="${esc(hint.label)}">` : ""}
            <button type="button" class="btn btn-ghost" data-persona-run
                ${p.enabled ? "" : "disabled"}>Run</button>
            <button type="button" class="btn btn-ghost" data-persona-toggle>
                ${p.enabled ? "Disable" : "Enable"}</button>
        </div>
        <p class="persona-hint">Edit <code>${esc(p.path)}</code> to customize — changes apply on next run.</p>`;
    card.querySelector("[data-persona-toggle]").addEventListener("click", async () => {
        await fetch(`/api/personas/${encodeURIComponent(p.slug)}/${p.enabled ? "disable" : "enable"}`,
            { method: "POST" });
        loadPersonas();
    });
    card.querySelector("[data-persona-run]").addEventListener("click", async () => {
        const inputs = {};
        if (hint) {
            const raw = card.querySelector(".persona-input").value.trim();
            if (!raw) { showToast(`${hint.label} required`, "error"); return; }
            inputs[hint.key] = hint.coerce(raw);
        }
        showToast("Running persona…");
        const res = await fetch(`/api/agents/persona:${encodeURIComponent(p.slug)}/run`,
            { method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ inputs }) });
        const body = await res.json().catch(() => ({}));
        if (res.ok && body.success) {
            showToast("Persona finished — suggestion pending");
            loadSuggestions();
            loadRuns();
        } else {
            showToast(`Persona run failed: ${esc(body?.error || body?.detail || res.status)}`, "error");
        }
    });
    return card;
}
