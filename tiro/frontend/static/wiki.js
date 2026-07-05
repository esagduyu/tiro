/* Tiro — Wiki views (/wiki list, /wiki/{slug} page) */

let wikiPageData = null; // cached GET /api/wiki/{slug} response for the page view
let wikiRegenerating = false; // in-flight guard so rapid clicks can't fire concurrent POSTs

document.addEventListener("DOMContentLoaded", () => {
    // wiki.js is loaded (via base.html's {% block content %}) before the
    // marked/DOMPurify vendor scripts at the bottom of base.html, so `marked`
    // isn't defined until later in the document parse. DOMContentLoaded only
    // fires after the whole document (including those later scripts) has
    // parsed, so it's safe to configure marked here — same pattern as
    // reader.js.
    if (document.getElementById("wiki-page")) {
        marked.setOptions({ breaks: false, gfm: true });
    }

    if (document.getElementById("wiki-content")) {
        setupWikiListKeyboard();
        loadWikiList();
    }

    if (document.getElementById("wiki-page")) {
        setupWikiPageKeyboard();
        loadWikiPage();
    }
});

/* --- List view (/wiki) --- */

async function loadWikiList() {
    const statusEl = document.getElementById("wiki-status");
    const contentEl = document.getElementById("wiki-content");
    const emptyEl = document.getElementById("wiki-empty");

    try {
        const res = await fetch("/api/wiki");
        const json = await res.json();
        if (!json.success) throw new Error("Invalid response");

        const pages = (json.data && json.data.pages) || [];
        statusEl.style.display = "none";

        if (pages.length === 0) {
            contentEl.style.display = "none";
            emptyEl.style.display = "block";
            return;
        }

        emptyEl.style.display = "none";
        contentEl.style.display = "block";
        renderWikiList(pages);
    } catch (err) {
        statusEl.innerHTML = '<p class="settings-error">Failed to load wiki pages.</p>';
    }
}

function renderWikiList(pages) {
    const byKind = { entity: [], concept: [] };
    pages.forEach((p) => {
        if (byKind[p.kind]) byKind[p.kind].push(p);
    });

    renderWikiKindSection("entity", byKind.entity);
    renderWikiKindSection("concept", byKind.concept);
}

function renderWikiKindSection(kind, pages) {
    const section = document.getElementById(`wiki-section-${kind}`);
    const tbody = document.getElementById(`wiki-tbody-${kind}`);
    if (!section || !tbody) return;

    if (!pages || pages.length === 0) {
        section.style.display = "none";
        return;
    }

    section.style.display = "block";
    tbody.innerHTML = pages.map(wikiRowHtml).join("");

    tbody.querySelectorAll("tr[data-slug]").forEach((row) => {
        row.addEventListener("click", () => {
            window.location.href = `/wiki/${row.dataset.slug}`;
        });
    });
}

function wikiRowHtml(p) {
    const staleBadge = p.status === "stale" ? '<span class="wiki-stale-badge">Stale</span>' : "";
    const updatedRaw = typeof timeAgo === "function" && p.updated_at
        ? timeAgo(new Date(p.updated_at.replace(" ", "T")))
        : (p.updated_at || "");
    return `
        <tr data-slug="${esc(p.slug)}" class="wiki-row">
            <td>${esc(p.title)} ${staleBadge}</td>
            <td class="sources-col-count">${num(p.source_count)}</td>
            <td>${esc(p.status)}</td>
            <td>${esc(updatedRaw)}</td>
        </tr>
    `;
}

/* --- Page view (/wiki/{slug}) --- */

async function loadWikiPage() {
    const loadingEl = document.getElementById("wiki-page-loading");
    const errorEl = document.getElementById("wiki-page-error");
    const contentEl = document.getElementById("wiki-page-content");
    const slug = document.getElementById("wiki-page").dataset.slug;

    try {
        const res = await fetch(`/api/wiki/${slug}`);
        const json = await res.json();
        if (!res.ok || !json.success) throw new Error("Failed to load wiki page");

        wikiPageData = json.data;
        loadingEl.style.display = "none";
        contentEl.style.display = "block";
        renderWikiPage(wikiPageData);
        setupWikiRegenerate(slug);
    } catch (err) {
        loadingEl.style.display = "none";
        errorEl.style.display = "block";
    }
}

function renderWikiPage(data) {
    document.getElementById("wiki-page-title").textContent = data.title || "";
    document.getElementById("wiki-page-kind").textContent =
        data.kind === "entity" ? (data.entity_type || "Entity") : "Concept";

    const statusEl = document.getElementById("wiki-page-status");
    statusEl.textContent = data.status || "";
    statusEl.className = "wiki-status-badge wiki-status-" + (data.status || "fresh");

    document.getElementById("wiki-page-sources").textContent =
        `${num(data.source_count)} source${data.source_count === 1 ? "" : "s"}`;

    const updatedText = typeof timeAgo === "function" && data.updated_at
        ? "Updated " + timeAgo(new Date(data.updated_at.replace(" ", "T")))
        : "";
    document.getElementById("wiki-page-updated").textContent = updatedText;

    const bodyEl = document.getElementById("wiki-page-body");
    const resolved = resolveWikilinks(data.body || "", data.citations || {});
    bodyEl.innerHTML = renderMarkdown(resolved);
}

/* --- Wikilink resolution (security-sensitive: runs BEFORE markdown render) ---
 *
 * Wiki page bodies are LLM-generated and may cite articles via
 * `[[stem|label]]` / `[[stem]]` tokens (see tiro/wiki_gen.py CITATION_RE).
 * The API resolves each cited stem against `articles.markdown_path` and
 * returns a `{stem: article_id}` citations map (unresolvable stems are
 * simply absent). This function must run BEFORE renderMarkdown() because
 * it emits new markdown syntax (`[label](/articles/{id})`) that has to be
 * parsed by marked, not treated as literal text.
 *
 * The label text is untrusted (LLM output, ultimately influenced by
 * ingested web/email content) and gets spliced into markdown source, so it
 * is defensively escaped before insertion:
 *   - backslash-escape `\`, `[`, `]` and `)` so the label can never
 *     prematurely close the `[...]` link-text span or splice a `](url)`
 *     sequence that would hijack the link target to an attacker-chosen
 *     URL (e.g. label = "click](http://evil.com)" must not turn into a
 *     real link to evil.com).
 *   - applied uniformly to both the resolvable (wrapped in `[...]`) and
 *     unresolvable (emitted as plain text) branches, since unescaped
 *     brackets in the "plain text" branch could just as easily form an
 *     unintended markdown link.
 * DOMPurify (via renderMarkdown) still sanitizes the final HTML output
 * regardless — this escaping prevents a *content-integrity* / phishing
 * issue (a citation link silently pointing somewhere other than the
 * cited article), not a distinct DOMPurify bypass.
 */
const WIKILINK_RE = /\[\[([^\]|]+)(?:\|([^\]]*))?\]\]/g;

function resolveWikilinks(body, citations) {
    citations = citations || {};
    return String(body).replace(WIKILINK_RE, (match, stem, label) => {
        let displayLabel = (label !== undefined ? label : stem).trim();
        if (!displayLabel) displayLabel = stem;
        const safeLabel = escapeMarkdownLinkText(displayLabel);

        // citations is a plain object from JSON; guard against prototype
        // properties (e.g. stem === "__proto__") being read as "resolved"
        // by requiring the value to actually be a number (article ids are
        // always numeric from the API).
        const articleId = Object.prototype.hasOwnProperty.call(citations, stem)
            ? citations[stem]
            : undefined;

        if (typeof articleId !== "number") {
            // Unresolvable: plain label text, no link, no brackets.
            return safeLabel;
        }
        return `[${safeLabel}](/articles/${articleId})`;
    });
}

function escapeMarkdownLinkText(text) {
    return String(text)
        .replace(/\\/g, "\\\\")
        .replace(/\[/g, "\\[")
        .replace(/\]/g, "\\]")
        .replace(/\)/g, "\\)");
}

/* --- Regenerate --- */

function setupWikiRegenerate(slug) {
    const btn = document.getElementById("wiki-regenerate-btn");
    if (!btn) return;
    btn.addEventListener("click", () => {
        showWikiRegenerateConfirm(() => doWikiRegenerate(slug));
    });
}

function showWikiRegenerateConfirm(onConfirm) {
    const existing = document.getElementById("wiki-regenerate-overlay");
    if (existing) existing.remove();

    const overlay = document.createElement("div");
    overlay.id = "wiki-regenerate-overlay";
    overlay.className = "export-overlay";
    overlay.innerHTML =
        '<div class="export-dialog">' +
            "<h3>Regenerate wiki page</h3>" +
            "<p>Regenerate this page from scratch? The current body will be discarded " +
            "and rebuilt from the library's current articles. This cannot be undone.</p>" +
            '<div class="export-dialog-actions">' +
                '<button class="export-cancel-btn" id="wiki-regenerate-cancel">Cancel</button>' +
                '<button class="danger-confirm-btn" id="wiki-regenerate-confirm">Regenerate</button>' +
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
    document.getElementById("wiki-regenerate-cancel").addEventListener("click", close);
    document.getElementById("wiki-regenerate-confirm").addEventListener("click", () => {
        close();
        onConfirm();
    });
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) close();
    });
    document.addEventListener("keydown", onKeydown);
}

async function doWikiRegenerate(slug) {
    if (wikiRegenerating) return;
    wikiRegenerating = true;

    const btn = document.getElementById("wiki-regenerate-btn");
    const errEl = document.getElementById("wiki-regenerate-error");
    errEl.style.display = "none";
    errEl.textContent = "";
    btn.disabled = true;
    const originalTitle = btn.title;
    btn.title = "Regenerating…";

    try {
        const res = await fetch(`/api/wiki/${slug}/regenerate`, { method: "POST" });
        const json = await res.json().catch(() => ({}));

        if (!res.ok || !json.success) {
            // detail may come from a 409 (already in progress) or a 422
            // (WikiGenerationError, e.g. zero resolvable citations) — either
            // way it's server text, rendered via textContent (not
            // innerHTML), so no HTML from `detail` is ever interpreted.
            errEl.textContent = (json && json.detail) || "Regeneration failed.";
            errEl.style.display = "block";
            btn.disabled = false;
            btn.title = originalTitle;
            return;
        }

        window.location.reload();
    } catch (err) {
        errEl.textContent = "Connection error — regeneration failed.";
        errEl.style.display = "block";
        btn.disabled = false;
        btn.title = originalTitle;
    } finally {
        wikiRegenerating = false;
    }
}

/* --- Keyboard --- */

function setupWikiListKeyboard() {
    setupWikiKeyboard("/inbox", showWikiListShortcuts);
}

function setupWikiPageKeyboard() {
    setupWikiKeyboard("/wiki", showWikiPageShortcuts);
}

function setupWikiKeyboard(backHref, showShortcuts) {
    document.addEventListener("keydown", (e) => {
        const tag = document.activeElement.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
            if (e.key === "Escape") {
                document.activeElement.blur();
                e.preventDefault();
            }
            return;
        }

        // Don't capture while a confirm dialog is open (it handles its own Escape)
        if (document.getElementById("wiki-regenerate-overlay")) {
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
                window.location.href = backHref;
                break;
            case "?":
                e.preventDefault();
                showShortcuts();
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

function showWikiListShortcuts() {
    renderWikiShortcuts([
        { section: "Navigation" },
        { keys: ["b", "Esc"], desc: "Back to inbox" },
        { section: "General" },
        { keys: ["?"], desc: "Show this help" },
    ]);
}

function showWikiPageShortcuts() {
    renderWikiShortcuts([
        { section: "Navigation" },
        { keys: ["b", "Esc"], desc: "Back to wiki" },
        { section: "General" },
        { keys: ["?"], desc: "Show this help" },
    ]);
}

function renderWikiShortcuts(shortcuts) {
    const overlay = document.getElementById("shortcuts-overlay");
    const body = document.getElementById("shortcuts-body");
    if (!overlay || !body) return;

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

/* --- Escaping helpers --- */

function esc(str) {
    const el = document.createElement("span");
    el.textContent = str == null ? "" : String(str);
    return el.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function num(x) {
    const n = Number(x);
    return Number.isFinite(n) ? n : "?";
}

function renderMarkdown(md) {
    const raw = marked.parse(md || "");
    return DOMPurify.sanitize(raw, {
        FORBID_TAGS: ["script", "iframe", "object", "embed", "form", "style"],
        FORBID_ATTR: ["onerror", "onclick", "onload", "onmouseover"],
        ADD_ATTR: ["loading"],
    });
}
