/* Tiro — Reader view (M2.0 module split, Task 3).
 *
 * Imports esc/num/formatDate/renderMarkdown/showToast/timeAgo from core.js
 * (this file's local copies of esc/renderMarkdown/showReaderToast were
 * verified byte-identical to core.js's versions before deletion — see
 * .superpowers/sdd/task-3-report.md for the diff) and showShortcuts/
 * hideShortcuts from sidebar.js (loaded on every page via base.html, so the
 * import is always satisfied by the time this module runs).
 *
 * reader.js is a LEAF entry module — nothing else imports it — so unlike
 * sidebar.js it keeps the normal `?v={{ static_v }}` cache-bust query in
 * reader.html's <script type="module"> tag (see the T2 double-instantiation
 * note in sidebar.js/core.js for why sidebar.js is the one exception).
 *
 * `showReaderDeleteConfirm` (the `delete-overlay`/`delete-cancel`/
 * `delete-confirm` dialog) is intentionally NOT migrated to core.js's
 * `confirmDialog` — same judgment call T2 made for inbox.js's delete
 * confirm (see .superpowers/sdd/task-2-report.md): this file's keyboard
 * handler checks for `#delete-overlay` by id directly, while confirmDialog
 * uses different ids (`core-confirm-*`). Migrating would mean rewiring that
 * guard too, which isn't part of a behavior-identical dedup task.
 *
 * --- Annotation UI (M2.2 Task 2) ---
 *
 * Wires `./annotate.js`'s pure markdown<->plain-text projection core into a
 * real DOM selection -> highlight -> paint pipeline. Three text spaces are
 * in play (see annotate.js's module docstring): MARKDOWN (the article's
 * stored body, `a.content` from `GET /api/articles/{id}` — the SAME text
 * `tiro/api/routes_annotations.py` anchors against, by design), the PLAIN
 * PROJECTION (`projectMarkdown(a.content)`, computed once per article load
 * and cached in `annotationProjection`), and RENDERED DOM text (the actual
 * `#reader-body` textContent after `renderMarkdown` + DOMPurify).
 *
 * The DOM<->plain bridge is symmetric and reused in both directions:
 *   - CREATE (selection -> anchor): a DOM Range's boundaries are converted
 *     to flat-DOM-text offsets via `domOffsetFromBoundary` (the standard
 *     "pre-range .toString().length" trick — see that function's comment
 *     for why this beats a hand-rolled node-index walk for THIS direction),
 *     then `findQuoteInPlain` (falling back to `findQuoteInPlainFallback`)
 *     locates the selected text + ~32-char DOM context inside
 *     `annotationProjection.plain`, and `plainToMarkdownRange` converts that
 *     to markdown offsets for the POST body.
 *   - PAINT (anchor -> selection): the inverse. `markdownQuoteToPlain` turns
 *     a highlight's (possibly server-reconciled "shifted") markdown offsets
 *     into a plain-space range, then the SAME `findQuoteInPlain`/
 *     `findQuoteInPlainFallback` pair (symmetric use — quote/context sliced
 *     from `plain`, haystack is the flat DOM text this time) locates it in
 *     rendered DOM text, and `domRangeFromTextIndices` (using a
 *     `buildTextIndex` node map, since painting needs a REAL Range with
 *     real text-node boundaries, unlike the toString()-trick direction)
 *     builds the Range added to a `Highlight` object.
 *
 * Painting uses the CSS Custom Highlight API (`CSS.highlights`) exclusively
 * — no DOM mutation (no wrapping `<mark>` spans), so `buildTextIndex`'s node
 * map, built once right after `renderMarkdown` sets `#reader-body`'s
 * innerHTML, stays valid across every subsequent paint (including newly
 * created highlights) for the lifetime of the page: the body's DOM never
 * changes after initial render. Feature-detected: browsers without
 * `CSS.highlights` skip painting entirely (logged once) — every other
 * annotation feature (create/copy/note) still works.
 *
 * --- Highlights & Notes panel (M2.2 Task 3) ---
 *
 * `setupHighlightsPanel()` (called once from `loadArticle`, right after
 * `setupAnnotations`) wires the `#highlights-panel` margin panel: a list of
 * this article's highlights in document order (sorted by their LIVE
 * `anchor_status.position_start`; `hash_mismatch`/`missing` highlights sort
 * last and are additionally grouped under a separate "Couldn't re-anchor"
 * warning section), per-uid actions (color swap, delete, note edit — all
 * through `PATCH`/`DELETE /api/highlights/{uid}`), and an article-level note
 * drawer at the panel's top (`PUT`/`DELETE /api/articles/{id}/note`). Opens
 * with the SAME affordance as `#analysis-panel` (slide-in-from-right +
 * backdrop overlay + Esc-to-close) for consistency, and the two panels are
 * mutually exclusive (opening one closes the other) since both occupy the
 * same fixed right-hand slot.
 *
 * `paintHighlight`'s successfully-built `Range` is additionally cached per
 * uid in `annotationPaintedRanges` — the panel's delete/color-swap actions
 * mutate that SAME Range object's bucket membership directly (no repaint
 * pass needed, since editing color/note never changes a highlight's text
 * position), and a panel-row click reuses it to `scrollIntoView` + flash the
 * real painted range via a fifth, transient `tiro-hl-flash` Custom Highlight
 * bucket (see `flashHighlightRange`). Clicking a highlight IN the article
 * body (the reverse direction) is NOT implemented — see `flashHighlightRange`
 * and task-3-report.md's "click-to-open decision" for the judgment call.
 */

import {
    esc,
    num,
    formatDate,
    renderMarkdown,
    showToast,
    timeAgo,
    confirmDialog,
} from "./core.js";
import { showShortcuts, hideShortcuts } from "./sidebar.js";
import { icon } from "./icons.js";
import {
    projectMarkdown,
    plainToMarkdownRange,
    markdownQuoteToPlain,
    findQuoteInPlain,
    findQuoteInPlainFallback,
} from "./annotate.js";
import { computeReadingProgress } from "./reading-progress.js";

document.addEventListener("DOMContentLoaded", () => {
    const reader = document.getElementById("reader");
    const articleId = reader.dataset.articleId;
    loadArticle(articleId);
    setupReaderKeyboard(articleId);
    setupReaderKebab();
    setupReaderActionBar();
});

async function loadArticle(id) {
    const loadingEl = document.getElementById("reader-loading");
    const errorEl = document.getElementById("reader-error");
    const contentEl = document.getElementById("reader-content");

    try {
        // Mark as read
        fetch(`/api/articles/${id}/read`, { method: "PATCH" }).catch(() => {});

        const res = await fetch(`/api/articles/${id}`);
        const json = await res.json();

        if (!json.success) {
            throw new Error("Failed to load article");
        }

        const a = json.data;

        // Title
        document.getElementById("reader-title").textContent = a.title;
        document.title = `${a.title} — Tiro`;

        // Source
        document.getElementById("reader-source").textContent =
            a.source_name || a.domain || "Unknown source";

        // VIP indicator (always show, make clickable). The meta-line star is the
        // single source of VIP state; the desktop header cluster's `star` icon-btn
        // (M3.2 T6 review) just mirrors it and delegates its click through
        // readerToggleVip → the meta star's click — no duplicated fetch/state logic.
        const vip = document.getElementById("reader-vip");
        const vipBtn = document.getElementById("reader-vip-btn");
        const syncVipBtn = () => {
            if (vipBtn) vipBtn.classList.toggle("active", vip.classList.contains("active"));
        };
        if (a.source_id) {
            vip.style.display = "inline";
            vip.dataset.sourceId = a.source_id;
            if (a.is_vip) vip.classList.add("active");
            syncVipBtn();
            vip.addEventListener("click", async () => {
                try {
                    const res = await fetch(`/api/sources/${a.source_id}/vip`, { method: "PATCH" });
                    const json = await res.json();
                    if (json.success) {
                        vip.classList.toggle("active");
                        syncVipBtn();
                    }
                } catch (err) {
                    console.error("VIP toggle failed:", err);
                }
            });
            if (vipBtn) vipBtn.addEventListener("click", readerToggleVip);
        }

        // Author
        const authorEl = document.getElementById("reader-author");
        const authorSep = document.getElementById("author-sep");
        if (a.author) {
            authorEl.textContent = a.author;
        } else {
            authorEl.style.display = "none";
            authorSep.style.display = "none";
        }

        // Date
        document.getElementById("reader-date").textContent = formatDate(
            a.published_at || a.ingested_at
        );

        // Reading time
        document.getElementById("reader-time").textContent =
            `${a.reading_time_min || "?"} min read`;

        // Original URL. The accent "Read the original" link in the meta area,
        // plus the desktop kebab item and the phone overflow-sheet item, all
        // point at the same URL (hidden when the article has none).
        const linkEl = document.getElementById("reader-original-link");
        const kebabOriginal = document.getElementById("reader-kebab-original");
        const sheetOriginal = document.getElementById("reader-sheet-original");
        if (a.url) {
            linkEl.href = a.url;
            linkEl.innerHTML = `${icon("external", { size: 13 })}${esc(new URL(a.url).hostname)}`;
            if (kebabOriginal) {
                kebabOriginal.href = a.url;
                kebabOriginal.style.display = "";
            }
            if (sheetOriginal) {
                sheetOriginal.href = a.url;
                sheetOriginal.style.display = "";
            }
        } else {
            linkEl.parentElement.style.display = "none";
        }

        // Tags
        const tagsEl = document.getElementById("reader-tags");
        if (a.tags && a.tags.length) {
            tagsEl.innerHTML = a.tags
                .map((t) => `<span class="tag clickable-tag" data-tag="${esc(t)}">${esc(t)}</span>`)
                .join("");
            tagsEl.querySelectorAll(".clickable-tag").forEach((tag) => {
                tag.addEventListener("click", () => {
                    window.location.href = `/?q=${encodeURIComponent(tag.dataset.tag)}`;
                });
            });
        }

        // Summary
        const summaryEl = document.getElementById("reader-summary");
        if (a.summary) {
            summaryEl.innerHTML = `<strong>TL;DR</strong> &ndash; <em>${esc(a.summary)}</em>`;
        } else {
            summaryEl.style.display = "none";
        }

        // Markdown body
        const bodyEl = document.getElementById("reader-body");
        if (a.content) {
            bodyEl.innerHTML = renderMarkdown(a.content);
            // Open external links in new tab
            bodyEl.querySelectorAll("a").forEach((link) => {
                if (link.hostname && link.hostname !== location.hostname) {
                    link.target = "_blank";
                    link.rel = "noopener noreferrer";
                }
            });
        }

        // Reading-session telemetry (M2.3 Task 2) — must run AFTER the body's
        // innerHTML is set (dwell tracking walks #reader-body's H2/H3s). No-ops
        // entirely when the server rendered data-telemetry="off".
        setupTelemetry(a.id);

        // Reading progress bar (owner UX wave 1) — must run AFTER the body's
        // innerHTML is set so the first paint measures the real body height.
        setupReadingProgress();

        // Annotations (highlights + selection toolbar) — must run AFTER the
        // body's innerHTML is set (buildTextIndex walks the rendered DOM).
        setupAnnotations(a.id, a.content || "");
        setupHighlightsPanel(a.id);

        // Rating buttons
        setupRating(a.id, a.rating);

        // Delete button
        setupDelete(a.id, a.title);

        // Related articles
        loadRelatedArticles(a.id);

        // Analysis panel
        setupAnalysis(a.id);

        // Audio player
        setupAudioPlayer(a.id, a.content || "");

        loadingEl.style.display = "none";
        contentEl.style.display = "block";
    } catch (err) {
        console.error("Failed to load article:", err);
        loadingEl.style.display = "none";
        errorEl.style.display = "block";
    }
}

function setupRating(articleId, currentRating) {
    const ratingMap = { "-1": "dislike", "1": "like", "2": "love" };
    const active = ratingMap[String(currentRating)] || "";

    document.querySelectorAll(".reader-actions .rate-btn").forEach((btn) => {
        const ratingClass = btn.classList.contains("love")
            ? "love"
            : btn.classList.contains("like")
            ? "like"
            : "dislike";
        if (ratingClass === active) btn.classList.add("active");

        btn.addEventListener("click", async () => {
            const rating = parseInt(btn.dataset.rating, 10);
            try {
                const res = await fetch(`/api/articles/${articleId}/rate`, {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ rating }),
                });
                const json = await res.json();
                if (json.success) {
                    document
                        .querySelectorAll(".reader-actions .rate-btn")
                        .forEach((b) => b.classList.remove("active"));
                    btn.classList.add("active");
                }
            } catch (err) {
                console.error("Rating failed:", err);
            }
        });
    });
}

/* --- Delete --- */

function setupDelete(articleId, title) {
    const btn = document.getElementById("delete-btn");
    if (!btn) return;
    btn.addEventListener("click", () => {
        readerDeleteFlow(articleId, title);
    });
}

function readerDeleteFlow(articleId, title) {
    showReaderDeleteConfirm(title, async () => {
        await deleteReaderArticle(articleId);
    });
}

function showReaderDeleteConfirm(title, onConfirm) {
    const existing = document.getElementById("delete-overlay");
    if (existing) existing.remove();

    const overlay = document.createElement("div");
    overlay.id = "delete-overlay";
    overlay.className = "export-overlay";
    overlay.innerHTML =
        '<div class="export-dialog">' +
            "<h3>Delete article</h3>" +
            `<p>Permanently delete <strong>${esc(title || "this article")}</strong> from your library? This cannot be undone.</p>` +
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

async function deleteReaderArticle(articleId) {
    try {
        const res = await fetch(`/api/articles/${articleId}`, { method: "DELETE" });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            showToast(err.detail || "Failed to delete article", "error");
            return;
        }
        showToast("Article deleted", "success");
        setTimeout(() => {
            window.location.href = "/inbox";
        }, 500);
    } catch (err) {
        console.error("Delete failed:", err);
        showToast("Failed to delete article", "error");
    }
}

/* --- Ingenuity Analysis Panel --- */

let analysisResult = null;
let analysisInFlight = false; // in-flight guard so rapid r-presses/clicks can't fire concurrent POSTs

function setupAnalysis(articleId) {
    const btn = document.getElementById("analysis-btn");
    const panel = document.getElementById("analysis-panel");
    const overlay = document.getElementById("analysis-overlay");
    const closeBtn = document.getElementById("analysis-close");
    const retryBtn = document.getElementById("analysis-retry");
    const runBtn = document.getElementById("analysis-run-btn");

    function openPanel() {
        // Mutually exclusive with the highlights panel (M2.2 Task 3) — both
        // occupy the same fixed right-hand slot, so opening one closes the
        // other rather than stacking two panels in the same spot.
        document.getElementById("highlights-panel")?.classList.remove("open");
        document.getElementById("highlights-overlay")?.classList.remove("open");
        panel.classList.add("open");
        overlay.classList.add("open");
    }
    function closePanel() {
        panel.classList.remove("open");
        overlay.classList.remove("open");
    }

    closeBtn.addEventListener("click", closePanel);
    overlay.addEventListener("click", closePanel);

    btn.addEventListener("click", async () => {
        openPanel();
        if (analysisResult) {
            showAnalysisBody();
        } else {
            // Check for cached analysis without triggering a new one
            try {
                const res = await fetch(`/api/articles/${articleId}/analysis`);
                const json = await res.json();
                if (json.success && json.data) {
                    analysisResult = json.data;
                    renderAnalysis(json.data);
                    showAnalysisBody();
                    return;
                }
            } catch (err) {
                // Ignore — fall through to intro
            }
            showAnalysisIntro();
        }
    });

    runBtn.addEventListener("click", () => {
        fetchAnalysis(articleId);
    });

    retryBtn.addEventListener("click", () => {
        fetchAnalysis(articleId);
    });
}

function showAnalysisIntro() {
    document.getElementById("analysis-intro").style.display = "block";
    document.getElementById("analysis-loading").style.display = "none";
    document.getElementById("analysis-error").style.display = "none";
    document.getElementById("analysis-body").style.display = "none";
}

function showAnalysisBody() {
    document.getElementById("analysis-intro").style.display = "none";
    document.getElementById("analysis-loading").style.display = "none";
    document.getElementById("analysis-error").style.display = "none";
    document.getElementById("analysis-body").style.display = "block";
}

async function fetchAnalysis(articleId) {
    if (analysisInFlight) return;
    analysisInFlight = true;

    const introEl = document.getElementById("analysis-intro");
    const loadingEl = document.getElementById("analysis-loading");
    const errorEl = document.getElementById("analysis-error");
    const bodyEl = document.getElementById("analysis-body");

    introEl.style.display = "none";
    loadingEl.style.display = "block";
    errorEl.style.display = "none";
    bodyEl.style.display = "none";

    try {
        const res = await fetch(`/api/articles/${articleId}/analysis`, {
            method: "POST",
        });
        const json = await res.json();

        if (!res.ok || !json.success) {
            throw new Error(json.detail || "Analysis failed");
        }

        analysisResult = json.data;
        renderAnalysis(json.data);
        loadingEl.style.display = "none";
        bodyEl.style.display = "block";
    } catch (err) {
        console.error("Analysis failed:", err);
        loadingEl.style.display = "none";
        errorEl.style.display = "block";
    } finally {
        analysisInFlight = false;
    }
}

function scoreColor(score) {
    if (score >= 7) return "score-good";
    if (score >= 4) return "score-caution";
    return "score-concern";
}

function aggregateScoreColor(avg) {
    if (avg >= 7) return "analysis-summary-good";
    if (avg >= 5) return "analysis-summary-caution";
    return "analysis-summary-concern";
}

/**
 * Reader-local wrapper over core.js's timeAgo(): analysis-panel timestamps
 * are handed an ISO string (or empty), while core's timeAgo takes a Date
 * and has no falsy short-circuit. Not a semantic divergence in the shared
 * logic (the diffMin/diffHr/diffDay bucketing below is core's timeAgo
 * itself) — just a different call-site shape, so this stays a thin local
 * function rather than a core.js export change.
 */
function analysisTimeAgo(isoStr) {
    if (!isoStr) return "";
    return timeAgo(new Date(isoStr));
}

function renderAnalysis(data) {
    const bodyEl = document.getElementById("analysis-body");

    const biasScore = data.bias?.score ?? "?";
    const factScore = data.factual_confidence?.score ?? "?";
    const novelScore = data.novelty?.score ?? "?";

    // Compute aggregate score for summary color
    const scores = [biasScore, factScore, novelScore].filter(
        (s) => typeof s === "number"
    );
    const avgScore =
        scores.length > 0
            ? scores.reduce((a, b) => a + b, 0) / scores.length
            : null;
    const summaryColorClass =
        avgScore !== null ? aggregateScoreColor(avgScore) : "";

    // Timestamp
    let timestampHtml = "";
    if (data.analyzed_at) {
        const ago = analysisTimeAgo(data.analyzed_at);
        timestampHtml = `<div class="analysis-timestamp">Analyzed ${ago}</div>`;
    }

    bodyEl.innerHTML = `
        ${timestampHtml}
        <div class="analysis-summary ${summaryColorClass}">${esc(data.overall_summary || "")}</div>

        <details class="analysis-dimension">
            <summary class="dimension-header">
                <span class="dimension-title">Bias</span>
                <span class="dimension-score ${scoreColor(biasScore)}">${num(biasScore)}/10</span>
            </summary>
            <div class="dimension-content">
                <div class="dimension-detail">
                    <span class="dimension-lean">${esc(data.bias?.lean || "")}</span>
                </div>
                ${renderList("Indicators", data.bias?.indicators)}
                ${renderList("Missing perspectives", data.bias?.missing_perspectives)}
            </div>
        </details>

        <details class="analysis-dimension">
            <summary class="dimension-header">
                <span class="dimension-title">Factual Confidence</span>
                <span class="dimension-score ${scoreColor(factScore)}">${num(factScore)}/10</span>
            </summary>
            <div class="dimension-content">
                ${renderList("Well-sourced claims", data.factual_confidence?.well_sourced_claims)}
                ${renderList("Unsourced assertions", data.factual_confidence?.unsourced_assertions)}
                ${renderList("Flags", data.factual_confidence?.flags)}
            </div>
        </details>

        <details class="analysis-dimension">
            <summary class="dimension-header">
                <span class="dimension-title">Novelty</span>
                <span class="dimension-score ${scoreColor(novelScore)}">${num(novelScore)}/10</span>
            </summary>
            <div class="dimension-content">
                <div class="dimension-detail">${esc(data.novelty?.assessment || "")}</div>
                ${renderList("Novel claims", data.novelty?.novel_claims)}
            </div>
        </details>

        <div class="analysis-actions">
            <button class="analysis-refresh-btn btn btn-ghost">${icon("refresh", { size: 14 })}Re-analyze</button>
        </div>
    `;

    bodyEl.querySelector(".analysis-refresh-btn").addEventListener("click", () => {
        fetchAnalysis(document.getElementById("reader").dataset.articleId);
    });
}

/* --- Related articles --- */

async function loadRelatedArticles(articleId) {
    const section = document.getElementById("related-articles");
    const listEl = document.getElementById("related-list");

    try {
        const res = await fetch(`/api/articles/${articleId}/related`);
        const json = await res.json();

        if (!json.success || !json.data || !json.data.length) {
            return;
        }

        listEl.innerHTML = json.data.map((r) => {
            const date = formatDate(r.published_at || r.ingested_at);
            const note = r.connection_note
                ? `<div class="related-card-note">${esc(r.connection_note)}</div>`
                : "";
            const score = Math.round(r.similarity_score * 100);
            return `
            <div class="related-card">
                <a href="/articles/${r.related_article_id}">
                    <div class="related-card-title">${esc(r.title)}</div>
                </a>
                <div class="related-card-meta">
                    <span>${esc(r.source_name || "")}</span>
                    <span class="meta-sep">&middot;</span>
                    <span>${date}</span>
                    <span class="meta-sep">&middot;</span>
                    <span class="similarity-badge">${score}% similar</span>
                </div>
                ${note}
            </div>`;
        }).join("");

        section.style.display = "block";
    } catch (err) {
        console.error("Failed to load related articles:", err);
    }
}

function renderList(label, items) {
    if (!items || !items.length) return "";
    const lis = items.map((item) => `<li>${esc(item)}</li>`).join("");
    return `<div class="dimension-list">
        <span class="dimension-list-label">${esc(label)}</span>
        <ul>${lis}</ul>
    </div>`;
}

/* --- Reader keyboard navigation --- */

function setupReaderKeyboard(articleId) {
    // Shortcuts overlay close
    const closeBtn = document.getElementById("shortcuts-close");
    if (closeBtn) {
        closeBtn.addEventListener("click", hideShortcuts);
    }
    const shortcutsOverlay = document.getElementById("shortcuts-overlay");
    if (shortcutsOverlay) {
        shortcutsOverlay.addEventListener("click", (e) => {
            if (e.target === shortcutsOverlay) hideShortcuts();
        });
    }

    document.addEventListener("keydown", (e) => {
        // Don't capture when typing in inputs
        const tag = document.activeElement.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
            if (e.key === "Escape") {
                document.activeElement.blur();
                e.preventDefault();
            }
            return;
        }

        // If shortcuts overlay is open, only ? and Escape close it
        if (shortcutsOverlay && shortcutsOverlay.style.display !== "none") {
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

        // If analysis panel is open, Escape closes it
        const analysisPanel = document.getElementById("analysis-panel");
        if (analysisPanel && analysisPanel.classList.contains("open") && e.key === "Escape") {
            document.getElementById("analysis-close")?.click();
            e.preventDefault();
            return;
        }

        // If the highlights panel is open, Escape closes it too (M2.2 Task 3
        // — same pattern as the analysis panel above; no new global keys are
        // bound to open this panel this task, only mouse/click affordances).
        const highlightsPanel = document.getElementById("highlights-panel");
        if (highlightsPanel && highlightsPanel.classList.contains("open") && e.key === "Escape") {
            document.getElementById("highlights-close")?.click();
            e.preventDefault();
            return;
        }

        // If the selection toolbar is open, Escape dismisses it instead of
        // falling through to the "b"/"Escape" case below (which would
        // navigate away and silently drop the user's reading position) —
        // same guard pattern as the panels above. setupAnnotationToolbar
        // registers its own keydown listener with the identical Escape
        // check, but since both listeners are plain (non-capturing, no
        // stopPropagation) document-level handlers, this one still runs
        // even after that one fires; hideAnnotationToolbar is idempotent
        // (safe to call twice), so no double-handling bug results.
        const annotateToolbar = document.getElementById("annotate-toolbar");
        if (annotateToolbar && annotateToolbar.classList.contains("open") && e.key === "Escape") {
            hideAnnotationToolbar(annotateToolbar);
            e.preventDefault();
            return;
        }

        // If the desktop kebab dropdown is open, Escape closes it instead of
        // navigating back to /inbox (M3.2 T6 review) — same guard pattern as the
        // panels above, and matching the inbox card-menu convention where an open
        // menu swallows keys except Escape. Toggling the button closes it;
        // setupReaderKebab's own Escape listener then no-ops (dropdown already
        // hidden), so no double-handling results.
        const kebabBtn = document.getElementById("reader-kebab-btn");
        const kebabDropdown = kebabBtn?.nextElementSibling;
        if (kebabDropdown && !kebabDropdown.hidden && e.key === "Escape") {
            kebabBtn.click();
            e.preventDefault();
            return;
        }

        // If the phone overflow sheet is showing, let sidebar.js's generic
        // Escape handler close it and swallow this event here — otherwise the
        // "Escape" case below would ALSO fire and navigate back to /inbox in
        // the same keydown (same guard pattern as the kebab dropdown above).
        // sidebar.js registers its keydown listener first, so by the time this
        // handler runs it has already stripped the sheet's `.open` class; the
        // sheet stays non-`[hidden]` through its 220ms slide-out, so match on
        // that instead of `.open` to stay correct regardless of listener order.
        if (document.querySelector(".sheet:not([hidden])") && e.key === "Escape") {
            return;
        }

        switch (e.key) {
            case "b":
            case "Escape":
                e.preventDefault();
                window.location.href = "/inbox";
                break;
            case "s":
                e.preventDefault();
                readerToggleVip();
                break;
            case "1":
                e.preventDefault();
                readerRate(-1); // dislike
                break;
            case "2":
                e.preventDefault();
                readerRate(1); // like
                break;
            case "3":
                e.preventDefault();
                readerRate(2); // love
                break;
            case "p":
                e.preventDefault();
                toggleAudioPlayback();
                break;
            case "i":
                e.preventDefault();
                readerToggleAnalysis();
                break;
            case "r":
                e.preventDefault();
                readerRunAnalysis(articleId);
                break;
            case "x":
                e.preventDefault();
                document.getElementById("delete-btn")?.click();
                break;
            case "d":
                e.preventDefault();
                window.location.href = "/digest";
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
            case "F":
                // Shift+F -> feeds (matches the inbox keymap; `n` is taken by
                // "save new item" there, so both pages use Shift+F for feeds).
                e.preventDefault();
                window.location.href = "/feeds";
                break;
            case "?":
                e.preventDefault();
                showShortcuts("reader");
                break;
        }
    });
}

function readerToggleVip() {
    const vipEl = document.getElementById("reader-vip");
    if (!vipEl || !vipEl.dataset.sourceId) return;
    vipEl.click();
}

function readerRate(rating) {
    const ratingMap = { "-1": "dislike", "1": "like", "2": "love" };
    const className = ratingMap[String(rating)];
    const btn = document.querySelector(`.reader-actions .rate-btn.${className}`);
    if (btn) btn.click();
}

function readerToggleAnalysis() {
    const panel = document.getElementById("analysis-panel");
    if (!panel) return;
    if (panel.classList.contains("open")) {
        document.getElementById("analysis-close")?.click();
    } else {
        document.getElementById("analysis-btn")?.click();
    }
}

function readerRunAnalysis(articleId) {
    const panel = document.getElementById("analysis-panel");
    if (!panel || !panel.classList.contains("open")) return;
    // If showing intro, click run; if showing results, re-analyze
    const runBtn = document.getElementById("analysis-run-btn");
    if (runBtn && runBtn.offsetParent !== null) {
        runBtn.click();
    } else {
        fetchAnalysis(articleId);
    }
}

/* Desktop reader header overflow menu (kebab → Read original / Delete). Delete
   inside keeps its `#delete-btn` id (so the `x` key and confirm dialog are
   unchanged); this just toggles the dropdown's `hidden` attribute. Mirrors the
   inbox card-menu pattern's markup/classes for free styling. */
function setupReaderKebab() {
    const btn = document.getElementById("reader-kebab-btn");
    if (!btn) return;
    const dropdown = btn.nextElementSibling;
    if (!dropdown) return;

    const close = () => {
        dropdown.hidden = true;
        btn.setAttribute("aria-expanded", "false");
    };

    btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const wasOpen = !dropdown.hidden;
        if (wasOpen) {
            close();
        } else {
            dropdown.hidden = false;
            btn.setAttribute("aria-expanded", "true");
        }
    });
    // Close on outside click / Escape.
    document.addEventListener("click", (e) => {
        if (!dropdown.hidden && !btn.parentElement.contains(e.target)) close();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !dropdown.hidden) close();
    });
}

/* Phone reader action bar (spec §6). Every button delegates to an existing
   reader.js handler/control — no new logic paths. The overflow-sheet open/close
   is handled generically by sidebar.js ([data-sheet]/[data-sheet-close]); this
   only wires each item to its existing action. */
function setupReaderActionBar() {
    document.querySelectorAll("#reader-action-bar .reader-bar-rate").forEach((b) => {
        b.addEventListener("click", () => readerRate(parseInt(b.dataset.rating, 10)));
    });
    document
        .getElementById("reader-bar-audio")
        ?.addEventListener("click", toggleAudioPlayback);
    document
        .getElementById("reader-bar-note")
        ?.addEventListener("click", () =>
            document.getElementById("article-note-btn")?.click()
        );
    document
        .getElementById("reader-sheet-analysis")
        ?.addEventListener("click", readerToggleAnalysis);
    document
        .getElementById("reader-sheet-vip")
        ?.addEventListener("click", readerToggleVip);
    document
        .getElementById("reader-sheet-delete")
        ?.addEventListener("click", () =>
            document.getElementById("delete-btn")?.click()
        );
}

/* --- Audio Player --- */

let audioState = { fallback: false, playing: false };

async function setupAudioPlayer(articleId, articleContent) {
    const player = document.getElementById("audio-player");
    if (!player) return;

    try {
        const res = await fetch(`/api/articles/${articleId}/audio/status`);
        const json = await res.json();
        if (!json.success) return;

        const data = json.data;
        player.style.display = "";

        // Reveal the phone action-bar play/pause button now that audio exists.
        const barAudio = document.getElementById("reader-bar-audio");
        if (barAudio) barAudio.style.display = "";

        if (data.fallback) {
            audioState.fallback = true;
            setupFallbackPlayer(articleContent);
        } else if (data.cached) {
            showAudioControls(articleId, data.duration_seconds);
        } else {
            setupGenerateButton(articleId);
        }
    } catch (err) {
        console.error("Audio status check failed:", err);
    }
}

function setupGenerateButton(articleId) {
    const genDiv = document.getElementById("audio-generate");
    const genBtn = document.getElementById("audio-generate-btn");
    genDiv.style.display = "";

    genBtn.addEventListener("click", () => {
        // Skip the generate step — just point audio at the streaming endpoint
        // and start playing. The GET /audio endpoint streams from OpenAI if not
        // cached, so the browser starts playing within ~1-2 seconds.
        genDiv.style.display = "none";
        showAudioControls(articleId, null);

        // Auto-play
        const audio = document.getElementById("audio-el");
        audio.play().catch(() => {});
    });
}

function showAudioControls(articleId, durationSeconds) {
    document.getElementById("audio-generate").style.display = "none";
    document.getElementById("audio-generating").style.display = "none";
    const controls = document.getElementById("audio-controls");
    controls.style.display = "flex";

    const audio = document.getElementById("audio-el");
    const playBtn = document.getElementById("audio-play-btn");
    const progressWrap = document.querySelector(".audio-progress-wrap");
    const progressFill = document.getElementById("audio-progress-fill");
    const timeEl = document.getElementById("audio-time");
    const speedBtn = document.getElementById("audio-speed-btn");

    audio.src = `/api/articles/${articleId}/audio`;

    // Show duration if known (cached playback), otherwise just elapsed time
    var knownDuration = durationSeconds || null;

    if (knownDuration) {
        timeEl.textContent = `0:00 / ${formatAudioTime(knownDuration)}`;
    } else {
        timeEl.textContent = "0:00";
    }

    audio.addEventListener("loadedmetadata", () => {
        if (audio.duration && isFinite(audio.duration)) {
            knownDuration = audio.duration;
            timeEl.textContent = `0:00 / ${formatAudioTime(audio.duration)}`;
        }
    });

    playBtn.addEventListener("click", toggleAudioPlayback);

    // Show spinner while buffering, pause icon only when audio actually plays
    audio.addEventListener("play", () => {
        playBtn.innerHTML = '<div class="audio-btn-spinner"></div>';
        playBtn.disabled = true;
    });
    audio.addEventListener("playing", () => {
        playBtn.innerHTML = icon("pause", { size: 15 });
        playBtn.disabled = false;
        audioState.playing = true;
    });
    audio.addEventListener("pause", () => {
        playBtn.innerHTML = icon("play", { size: 15 });
        playBtn.disabled = false;
        audioState.playing = false;
    });
    audio.addEventListener("ended", () => {
        playBtn.innerHTML = icon("play", { size: 15 });
        playBtn.disabled = false;
        audioState.playing = false;
        progressFill.style.width = "0%";
    });

    audio.addEventListener("timeupdate", () => {
        if (knownDuration) {
            const pct = (audio.currentTime / knownDuration) * 100;
            progressFill.style.width = Math.min(pct, 100) + "%";
            timeEl.textContent =
                formatAudioTime(audio.currentTime) + " / " + formatAudioTime(knownDuration);
        } else {
            // Streaming — no total duration yet, show elapsed only
            timeEl.textContent = formatAudioTime(audio.currentTime);
        }
    });

    progressWrap.addEventListener("click", (e) => {
        if (!audio.duration) return;
        const rect = progressWrap.getBoundingClientRect();
        const pct = (e.clientX - rect.left) / rect.width;
        audio.currentTime = pct * audio.duration;
    });

    const speeds = [1, 1.25, 1.5, 2];
    let speedIndex = 0;
    speedBtn.addEventListener("click", () => {
        speedIndex = (speedIndex + 1) % speeds.length;
        audio.playbackRate = speeds[speedIndex];
        speedBtn.textContent = speeds[speedIndex] + "x";
    });
}

function toggleAudioPlayback() {
    if (audioState.fallback) {
        toggleFallbackPlayback();
        return;
    }

    const audio = document.getElementById("audio-el");
    if (!audio || !audio.src) return;

    if (audio.paused) {
        audio.play();
    } else {
        audio.pause();
    }
}

/* --- speechSynthesis fallback --- */

let fallbackState = { cleanText: "", charIndex: 0, rate: 1, startTime: 0 };

function setupFallbackPlayer(articleContent) {
    if (!window.speechSynthesis) return;

    const genDiv = document.getElementById("audio-generate");
    const genBtn = document.getElementById("audio-generate-btn");

    genDiv.style.display = "";
    genBtn.textContent = "Listen (browser voice)";
    genBtn.classList.add("audio-fallback-btn");

    genBtn.addEventListener("click", () => {
        genDiv.style.display = "none";
        const controls = document.getElementById("audio-controls");
        controls.style.display = "flex";

        // Wire up play/pause button for fallback
        document.getElementById("audio-play-btn").addEventListener("click", toggleAudioPlayback);

        // Wire up speed button
        const speedBtn = document.getElementById("audio-speed-btn");
        const speeds = [1, 1.25, 1.5, 2];
        let speedIndex = 0;
        speedBtn.addEventListener("click", () => {
            speedIndex = (speedIndex + 1) % speeds.length;
            fallbackState.rate = speeds[speedIndex];
            speedBtn.textContent = speeds[speedIndex] + "x";
            // Restart speech at current position with new rate
            if (speechSynthesis.speaking || speechSynthesis.paused) {
                speechSynthesis.cancel();
                startFallbackSpeechFrom(fallbackState.charIndex);
            }
        });

        // Estimate duration (~150 words per minute)
        const clean = stripMarkdownForSpeech(articleContent);
        fallbackState.cleanText = clean;
        const wordCount = clean.split(/\s+/).length;
        const estSeconds = (wordCount / 150) * 60;
        document.getElementById("audio-time").textContent = "0:00 / " + formatAudioTime(estSeconds);

        startFallbackSpeechFrom(0);
    });
}

function stripMarkdownForSpeech(text) {
    return text
        .replace(/!\[[^\]]*\]\([^)]+\)/g, "")
        .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
        .replace(/\*{1,3}([^*]+)\*{1,3}/g, "$1")
        .replace(/#{1,6}\s+/g, "")
        .replace(/`[^`]+`/g, "")
        .replace(/<[^>]+>/g, "")
        .trim();
}

function startFallbackSpeechFrom(charIndex) {
    const text = fallbackState.cleanText.substring(charIndex);
    if (!text) return;

    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = fallbackState.rate;
    audioState.playing = true;
    fallbackState.startTime = Date.now();

    const playBtn = document.getElementById("audio-play-btn");
    const progressFill = document.getElementById("audio-progress-fill");
    const timeEl = document.getElementById("audio-time");
    const totalChars = fallbackState.cleanText.length;

    // Estimate total duration at current rate
    const wordCount = fallbackState.cleanText.split(/\s+/).length;
    const estTotalSeconds = (wordCount / 150) * 60 / fallbackState.rate;

    playBtn.innerHTML = icon("pause", { size: 15 });

    // Track progress via boundary events
    utterance.onboundary = (e) => {
        const currentChar = charIndex + e.charIndex;
        fallbackState.charIndex = currentChar;
        const pct = (currentChar / totalChars) * 100;
        progressFill.style.width = pct + "%";

        // Estimate elapsed time from progress percentage
        const elapsed = (pct / 100) * estTotalSeconds;
        timeEl.textContent = formatAudioTime(elapsed) + " / " + formatAudioTime(estTotalSeconds);
    };

    utterance.onend = () => {
        playBtn.innerHTML = icon("play", { size: 15 });
        audioState.playing = false;
        progressFill.style.width = "100%";
        timeEl.textContent = formatAudioTime(estTotalSeconds) + " / " + formatAudioTime(estTotalSeconds);
        fallbackState.charIndex = 0;
    };

    speechSynthesis.speak(utterance);
}

function toggleFallbackPlayback() {
    if (!window.speechSynthesis) return;

    const playBtn = document.getElementById("audio-play-btn");

    if (speechSynthesis.speaking && !speechSynthesis.paused) {
        speechSynthesis.pause();
        playBtn.innerHTML = icon("play", { size: 15 });
        audioState.playing = false;
    } else if (speechSynthesis.paused) {
        speechSynthesis.resume();
        playBtn.innerHTML = icon("pause", { size: 15 });
        audioState.playing = true;
    }
}

function formatAudioTime(seconds) {
    if (!seconds || !isFinite(seconds)) return "0:00";
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return m + ":" + (s < 10 ? "0" : "") + s;
}

/* --- Reading progress bar (owner UX wave 1) ---
 *
 * A thin fixed bar whose fill width tracks scroll through #reader-body via
 * the pure computeReadingProgress() (see reading-progress.js). rAF-throttled
 * passive scroll + resize listeners; a single ticking guard coalesces bursts
 * of scroll events into at most one measurement per frame. Deliberately its
 * own listener set — NOT piggybacked on setupTelemetry's scroll handler,
 * which (a) only runs when telemetry is opted in and (b) measures against the
 * full-document scrollHeight, a different denominator (see reading-progress.js
 * for the rationale). prefers-reduced-motion needs no special handling: the
 * fill is a direct width write with no CSS transition, so there's nothing to
 * suppress. */
function setupReadingProgress() {
    const bar = document.getElementById("reading-progress-bar");
    const bodyEl = document.getElementById("reader-body");
    if (!bar || !bodyEl) return;

    let ticking = false;

    function measure() {
        ticking = false;
        const rect = bodyEl.getBoundingClientRect();
        const bodyTop = rect.top + window.scrollY;
        const frac = computeReadingProgress(
            window.scrollY,
            window.innerHeight || document.documentElement.clientHeight || 0,
            bodyTop,
            rect.height,
        );
        bar.style.width = (frac * 100).toFixed(2) + "%";
    }

    function onScrollOrResize() {
        if (ticking) return;
        ticking = true;
        window.requestAnimationFrame(measure);
    }

    window.addEventListener("scroll", onScrollOrResize, { passive: true });
    window.addEventListener("resize", onScrollOrResize, { passive: true });
    // Re-measure when #reader-body's own height changes after first paint —
    // late-loading images reflow the body and shift the true denominator, which
    // a window resize/scroll alone wouldn't catch.
    if (typeof ResizeObserver !== "undefined") {
        new ResizeObserver(onScrollOrResize).observe(bodyEl);
    }
    measure(); // initial fill (short articles start at 100%)
}

/* --- Reading-session telemetry (M2.3 Task 2) ---
 *
 * Opt-in, strictly local-only (see `tiro/api/routes_sessions.py` for the
 * server side, which double-checks `reading_telemetry_enabled` itself — this
 * client gate is belt-and-suspenders, not the only enforcement point).
 * `reader.html` renders `data-telemetry="on"|"off"` on `#reader` from
 * `config.reading_telemetry_enabled` (threaded through the `/articles/{id}`
 * route in `tiro/app.py`, the same way `_theme_context` threads theme
 * hrefs); when it's "off", `setupTelemetry` returns immediately and NOTHING
 * below ever registers a listener or a timer.
 *
 * Three signals are tracked, matching `SessionPayload` in
 * routes_sessions.py:
 *   - max_scroll_pct: high-watermark of (scrollTop+viewportH)/scrollHeight,
 *     clamped 0-100. Short articles (the whole thing fits without scrolling)
 *     are scored 100 immediately on render — there's no meaningful "depth"
 *     concept when there's nothing to scroll, and treating it as 0 would
 *     make short-but-fully-read articles look unread to the future ranking
 *     signal this feeds (Decision #8).
 *   - active_seconds: a 1s interval accumulator gated on the tab being
 *     visible AND a qualifying interaction (scroll/keydown/pointermove/
 *     pointerdown) within the last 30s — an open-but-idle background tab
 *     (or an open-but-ignored foreground tab) doesn't count as reading time.
 *   - dwell: active seconds attributed to "the current section" — the last
 *     H2/H3 in #reader-body whose top has scrolled above the viewport
 *     (plain scroll-position math via getBoundingClientRect, re-evaluated
 *     every tick — simpler and more directly testable than an
 *     IntersectionObserver for a single "which heading are we past" query).
 *     Time before the first heading (or when there are no headings at all)
 *     attributes to a synthetic "(intro)" bucket. Keyed by heading
 *     textContent (truncated 200 chars, mirroring the server's clamp) —
 *     articles with duplicate heading text merge into one dwell bucket, an
 *     accepted simplification (the payload only round-trips heading TEXT,
 *     not DOM identity, since that's what routes_sessions.py stores).
 *
 * Sent exactly once per page load, on the first of visibilitychange->hidden
 * or pagehide, via `navigator.sendBeacon` (a Blob with
 * `type: "application/json"` — routes_sessions.py reads it with
 * `request.json()`, which works fine off a Blob-POSTed body since the
 * Content-Type header is what FastAPI/Starlette keys off, not the sender
 * being fetch vs sendBeacon) falling back to `fetch(..., {method: "POST",
 * keepalive: true})` when sendBeacon is unavailable or returns false (queue
 * full). A `sent` flag on the per-load state object makes both trigger paths
 * (visibilitychange and pagehide can both fire on some browsers/OSes) safe —
 * whichever runs first wins, the second is a no-op.
 *
 * Empty-session guard: if the tab is hidden/closed with active_seconds === 0
 * AND max_scroll_pct === 0 (opened and instantly backgrounded, or a
 * prefetch/bot hit that never rendered to a human), nothing is sent at all —
 * that row would be pure noise for the ranking signal this feeds, not a
 * " 0-effort read" worth recording.
 */

let telemetryState = null; // reset per page load; stays null for the life of the page when disabled

function setupTelemetry(articleId) {
    const reader = document.getElementById("reader");
    if (!reader || reader.dataset.telemetry !== "on") return; // disabled: zero listeners, zero timers

    const bodyEl = document.getElementById("reader-body");
    const headings = bodyEl ? Array.from(bodyEl.querySelectorAll("h2, h3")) : [];

    telemetryState = {
        articleId,
        startedAt: new Date().toISOString(),
        maxScrollPct: 0,
        activeSeconds: 0,
        dwell: new Map(), // heading text (or "(intro)") -> accumulated seconds
        lastInteraction: Date.now(),
        sent: false,
        headings,
        intervalId: null,
    };

    updateTelemetryScrollDepth(); // short articles: score 100 immediately, before any scroll event
    window.addEventListener("scroll", updateTelemetryScrollDepth, { passive: true });
    window.addEventListener("resize", updateTelemetryScrollDepth);

    ["scroll", "keydown", "pointermove", "pointerdown"].forEach((evt) => {
        document.addEventListener(evt, markTelemetryInteraction, { passive: true });
    });

    telemetryState.intervalId = setInterval(tickTelemetry, 1000);

    document.addEventListener("visibilitychange", handleTelemetryVisibilityChange);
    window.addEventListener("pagehide", sendTelemetry);
}

function updateTelemetryScrollDepth() {
    if (!telemetryState) return;
    const doc = document.documentElement;
    const viewportH = window.innerHeight || doc.clientHeight || 0;
    const scrollHeight = doc.scrollHeight || 0;
    const scrollTop = window.scrollY || doc.scrollTop || 0;

    let pct;
    if (scrollHeight <= viewportH) {
        pct = 100; // nothing to scroll: the whole article is already on screen
    } else {
        pct = ((scrollTop + viewportH) / scrollHeight) * 100;
    }
    pct = Math.max(0, Math.min(100, Math.round(pct)));
    if (pct > telemetryState.maxScrollPct) telemetryState.maxScrollPct = pct;
}

function markTelemetryInteraction() {
    if (telemetryState) telemetryState.lastInteraction = Date.now();
}

function tickTelemetry() {
    if (!telemetryState) return;
    const idleMs = Date.now() - telemetryState.lastInteraction;
    const active = document.visibilityState === "visible" && idleMs <= 30000;
    if (!active) return;

    telemetryState.activeSeconds += 1;

    const heading = currentTelemetrySection();
    telemetryState.dwell.set(heading, (telemetryState.dwell.get(heading) || 0) + 1);
}

function currentTelemetrySection() {
    if (!telemetryState || telemetryState.headings.length === 0) return "(intro)";

    let current = null;
    for (const h of telemetryState.headings) {
        if (h.getBoundingClientRect().top <= 0) {
            current = h;
        } else {
            break; // headings are in document order; the first one still below viewport top ends the scan
        }
    }
    if (!current) return "(intro)";
    return (current.textContent || "").trim().slice(0, 200) || "(intro)";
}

function handleTelemetryVisibilityChange() {
    if (document.visibilityState === "hidden") sendTelemetry();
}

function sendTelemetry() {
    if (!telemetryState || telemetryState.sent) return;

    if (telemetryState.activeSeconds === 0 && telemetryState.maxScrollPct === 0) {
        // Empty-session guard — see module comment above. Checked BEFORE the
        // sent flag/teardown below: an instant hide that trips this guard
        // must not burn the page load's once-per-load send budget, so a
        // returning-visible user later in the same load can still send.
        return;
    }
    telemetryState.sent = true; // set BEFORE any early return past this point: never send twice, never retry

    teardownTelemetryListeners();

    const dwell = Array.from(telemetryState.dwell.entries())
        .slice(0, 100)
        .map(([heading, seconds]) => ({ heading, seconds }));

    const payload = {
        started_at: telemetryState.startedAt,
        max_scroll_pct: telemetryState.maxScrollPct,
        active_seconds: telemetryState.activeSeconds,
        dwell,
    };
    const body = JSON.stringify(payload);
    const url = `/api/articles/${telemetryState.articleId}/session`;

    let beaconSent = false;
    if (navigator.sendBeacon) {
        try {
            beaconSent = navigator.sendBeacon(url, new Blob([body], { type: "application/json" }));
        } catch (err) {
            beaconSent = false;
        }
    }
    if (!beaconSent) {
        fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body,
            keepalive: true,
        }).catch(() => {});
    }
}

function teardownTelemetryListeners() {
    if (!telemetryState) return;
    if (telemetryState.intervalId) clearInterval(telemetryState.intervalId);
    window.removeEventListener("scroll", updateTelemetryScrollDepth);
    window.removeEventListener("resize", updateTelemetryScrollDepth);
    ["scroll", "keydown", "pointermove", "pointerdown"].forEach((evt) => {
        document.removeEventListener(evt, markTelemetryInteraction);
    });
    document.removeEventListener("visibilitychange", handleTelemetryVisibilityChange);
    window.removeEventListener("pagehide", sendTelemetry);
}

/* --- Annotations: selection -> highlight -> paint (M2.2 Task 2) --- */

const ANNOTATE_COLORS = ["yellow", "green", "blue", "pink"];
const ANNOTATE_DEFAULT_COLOR = "yellow";
const ANNOTATE_CONTEXT_CHARS = 32;
const ANNOTATE_SELECTIONCHANGE_DEBOUNCE_MS = 150;

// Per-article state, reset by setupAnnotations() on every loadArticle() run.
let annotationProjection = null; // {plain, map} from projectMarkdown(a.content)
let annotationTextIndex = null; // {text, nodes} — flat DOM text + node offsets
let annotationHighlights = []; // local cache of highlight dicts from the GET
let annotationHighlightObjects = null; // {color: Highlight} once CSS.highlights registered
let annotationCssSupported = false;
let annotationCssWarned = false;
let annotationSelectionRange = null; // cloned Range backing the open toolbar
let annotationSelectionText = "";
let annotationSelectionDebounceTimer = null;
let annotationArticleId = null;
let annotateCreateInFlight = false; // guard: one POST per color/Note click, no concurrent creates
let annotationPaintedRanges = new Map(); // uid -> {color, range} for the currently-painted Range per highlight (Task 3: delete/color-swap/flash reuse these directly)

function setupAnnotations(articleId, articleContent) {
    annotationArticleId = articleId;
    const bodyEl = document.getElementById("reader-body");
    annotationProjection = projectMarkdown(articleContent);
    annotationTextIndex = buildTextIndex(bodyEl);
    annotationHighlights = [];
    annotationPaintedRanges = new Map();

    annotationCssSupported = typeof CSS !== "undefined" && !!CSS.highlights;
    if (annotationCssSupported) {
        annotationHighlightObjects = {};
        ANNOTATE_COLORS.forEach((color) => {
            const h = new Highlight();
            annotationHighlightObjects[color] = h;
            CSS.highlights.set(`tiro-hl-${color}`, h);
        });
    } else if (!annotationCssWarned) {
        // eslint-disable-next-line no-console
        console.warn(
            "CSS Custom Highlight API not supported in this browser — " +
            "highlights will be created and stored normally but not painted."
        );
        annotationCssWarned = true;
    }

    setupAnnotationToolbar(bodyEl);
    loadAnnotations(articleId);
}

async function loadAnnotations(articleId) {
    // M2.2 Task 4 review fix: consume the /highlights click-through handoff
    // key UNCONDITIONALLY, before the fetch, regardless of whether it
    // succeeds. Previously this was only consumed after a successful
    // `loadAnnotations()` (inside the try, after render) — a failed fetch
    // (network blip, 500, etc.) left the sessionStorage key in place, so it
    // would incorrectly flash on a LATER, unrelated article's load once the
    // network recovered. Reading+removing first means the handoff is used at
    // most once no matter what happens to this fetch; the captured uid is
    // only acted on (via `flashHighlightRange`) after annotations have
    // actually painted, further below.
    const flashUid = consumeFlashHandoffKey();

    try {
        const res = await fetch(`/api/articles/${articleId}/annotations`);
        const json = await res.json();
        if (!json.success) return;
        annotationHighlights = json.data.highlights || [];
        annotationHighlights.forEach(paintHighlight);
        // Task 3: the same GET payload carries the article-level note — cache
        // it and render the (by-then-already-wired) highlights panel rather
        // than issuing a second fetch.
        articleNoteState = json.data.note || null;
        renderHighlightsPanel();
        // M2.2 Task 4: reuses T3's `flashHighlightRange` verbatim — no new
        // scroll/flash mechanism — so if the uid isn't a currently-painted
        // Range (unanchored highlight, or a stale/foreign uid), it already
        // no-ops gracefully.
        if (flashUid) flashHighlightRange(flashUid);
    } catch (err) {
        console.error("Failed to load annotations:", err);
    }
}

/**
 * M2.2 Task 4: /highlights review view hands off a click-through via
 * `sessionStorage['tiro:flash-highlight']` (set to the highlight's uid).
 * Read-and-remove happens here, UNCONDITIONALLY and before the annotations
 * fetch — see the review-fix comment in `loadAnnotations` for why the old
 * "consume only on success" ordering was wrong.
 */
function consumeFlashHandoffKey() {
    try {
        const uid = sessionStorage.getItem("tiro:flash-highlight");
        if (uid) sessionStorage.removeItem("tiro:flash-highlight");
        return uid;
    } catch (err) {
        return null; // sessionStorage unavailable — nothing to consume
    }
}

/**
 * Paint one highlight dict (as returned by `GET /api/articles/{id}/
 * annotations`, or synthesized client-side right after a successful create —
 * see `createHighlightFromSelection`) via the CSS Custom Highlight API.
 * Skipped entirely (no-op, not an error) when `CSS.highlights` isn't
 * supported, when the anchor's live status is `hash_mismatch`/`missing`
 * (never painted per the task brief — T3's panel is where those surface),
 * or when either half of the DOM<->plain bridge can't locate the text
 * (stale/edited content, formatting-crossing edge cases) — painting is
 * best-effort, never a crash.
 */
function paintHighlight(hl) {
    if (!annotationCssSupported) return;

    const anchorStatus = hl.anchor_status;
    const status = anchorStatus && anchorStatus.status;
    if (status !== "exact" && status !== "shifted") return;

    // Use the LIVE (possibly reconciled/"shifted") positions from
    // anchor_status, not the stored text_position_start/end — for a
    // "shifted" highlight these differ, and anchor_status's are current.
    const mdStart = anchorStatus.position_start;
    const mdEnd = anchorStatus.position_end;
    if (typeof mdStart !== "number" || typeof mdEnd !== "number") return;

    const plainRange = markdownQuoteToPlain(annotationProjection, mdStart, mdEnd);
    if (!plainRange) return;

    const domRange = locatePlainRangeInDomText(plainRange);
    if (!domRange) return;

    const range = domRangeFromTextIndices(annotationTextIndex, domRange.start, domRange.end);
    if (!range) return;

    const paintColor = ANNOTATE_COLORS.includes(hl.color) ? hl.color : ANNOTATE_DEFAULT_COLOR;
    const bucket = annotationHighlightObjects[paintColor];
    try {
        bucket.add(range);
        // Cache the real, successfully-painted Range per uid (Task 3): the
        // panel's delete/color-swap actions mutate this SAME Range object's
        // bucket membership directly instead of re-running the DOM<->plain
        // bridge, and a panel-row click reuses it to scrollIntoView + flash.
        if (hl.uid) annotationPaintedRanges.set(hl.uid, { color: paintColor, range });
    } catch (err) {
        // Range construction succeeded but Highlight.add() can still throw
        // on a degenerate/collapsed range in some engines — never let a
        // single bad highlight break the rest of the paint pass.
        console.error("Failed to paint highlight:", err);
    }
}

/** PLAIN-space range -> DOM-text-space range, using the same symmetric
 * findQuoteInPlain/findQuoteInPlainFallback pair CREATE uses in the other
 * direction (haystack/needle roles swapped: here the flat DOM text is the
 * haystack and the plain projection supplies the quote/context). */
function locatePlainRangeInDomText(plainRange) {
    const { plain } = annotationProjection;
    const domText = annotationTextIndex.text;

    const quote = plain.slice(plainRange.start, plainRange.end);
    const prefix = plain.slice(Math.max(0, plainRange.start - ANNOTATE_CONTEXT_CHARS), plainRange.start);
    const suffix = plain.slice(plainRange.end, plainRange.end + ANNOTATE_CONTEXT_CHARS);
    const approxDomPos = plain.length
        ? Math.round((plainRange.start / plain.length) * domText.length)
        : 0;

    let found = findQuoteInPlain(domText, quote, prefix, suffix, approxDomPos);
    if (!found) {
        found = findQuoteInPlainFallback(domText, quote, prefix, suffix, approxDomPos);
    }
    return found;
}

/** Walk `rootEl`'s text nodes (TreeWalker, document order) into a flat
 * string plus a per-node {start, end} offset table. Built ONCE per article
 * load (see setupAnnotations) and reused for every paint — the reader body's
 * DOM never mutates after initial render (painting uses CSS Custom
 * Highlights, not wrapper spans), so the index never goes stale mid-page. */
function buildTextIndex(rootEl) {
    const walker = document.createTreeWalker(rootEl, NodeFilter.SHOW_TEXT, null);
    let text = "";
    const nodes = [];
    let node = walker.nextNode();
    while (node) {
        const value = node.nodeValue || "";
        nodes.push({ node, start: text.length, end: text.length + value.length });
        text += value;
        node = walker.nextNode();
    }
    return { text, nodes };
}

/** Binary search `textIndex.nodes` (sorted by `start`, document order) for
 * the text node covering flat-text `index`, returning a {node, offset}
 * boundary point suitable for `Range.setStart`/`setEnd`. */
function domPointFromTextIndex(textIndex, index) {
    const nodes = textIndex.nodes;
    if (nodes.length === 0) return null;
    let lo = 0;
    let hi = nodes.length - 1;
    while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (nodes[mid].end < index) lo = mid + 1;
        else hi = mid;
    }
    const n = nodes[lo];
    const offset = Math.min(Math.max(index - n.start, 0), n.node.nodeValue.length);
    return { node: n.node, offset };
}

/** Build a real DOM Range spanning flat-text offsets [start, end) using the
 * node index — needed for CSS.highlights (unlike the reverse direction,
 * which only needs a character COUNT, not real boundaries; see
 * domOffsetFromBoundary). */
function domRangeFromTextIndices(textIndex, start, end) {
    const startPoint = domPointFromTextIndex(textIndex, start);
    const endPoint = domPointFromTextIndex(textIndex, end);
    if (!startPoint || !endPoint) return null;
    try {
        const range = document.createRange();
        range.setStart(startPoint.node, startPoint.offset);
        range.setEnd(endPoint.node, endPoint.offset);
        return range;
    } catch (err) {
        return null;
    }
}

/**
 * Flat-DOM-text character offset of a Range boundary (container, offset)
 * relative to `rootEl` — the standard "measure a pre-range's stringified
 * length" technique. Chosen over a hand-rolled node-index lookup for THIS
 * direction because a real user/programmatic Selection's boundary container
 * is not always a Text node (`Range.selectNodeContents(p)` — used by this
 * task's Playwright spec — sets `startContainer`/`endContainer` to the
 * ELEMENT `p` with an offset counted in child nodes, not characters); walking
 * `container`+`offset` back to a text-node-relative index would need the
 * same "first/last text descendant" logic `Range.toString()` already
 * implements internally, so reusing it here (rather than re-deriving it) is
 * simpler and matches the browser's own definition of "flattened text
 * length" exactly, including for element-node boundaries.
 */
function domOffsetFromBoundary(rootEl, container, offset) {
    try {
        const preRange = document.createRange();
        preRange.selectNodeContents(rootEl);
        preRange.setEnd(container, offset);
        return preRange.toString().length;
    } catch (err) {
        // Swallowed-exception risk is BOUNDED, not silent data corruption:
        // a thrown `setEnd`/`toString` here only means this ONE boundary
        // falls back to offset 0, which feeds `approxPlainPos` — a tiebreak
        // hint for `findQuoteInPlain`'s proximity scoring, not the quote
        // match itself. The quote text still has to be found verbatim in
        // `annotationProjection.plain` (via prefix/suffix/exact-text
        // scoring) for a highlight to be created at all; a bad approxPos at
        // worst picks the wrong occurrence among several equally-scored
        // duplicates, it never fabricates a match that isn't really there.
        return 0;
    }
}

/** Build (once, lazily) the floating selection toolbar's DOM inside the
 * static `#annotate-toolbar` container reader.html provides, and wire its
 * mouseup/selectionchange show/hide + color/Note/Copy click handlers. Safe
 * to call once per article load (setupAnnotations always runs after a fresh
 * loadArticle()); re-populating innerHTML each time is harmless since no
 * article-specific data is baked into the toolbar's static markup. */
function setupAnnotationToolbar(bodyEl) {
    const toolbar = document.getElementById("annotate-toolbar");
    if (!toolbar) return; // template didn't ship the container — degrade silently

    toolbar.innerHTML = `
        ${ANNOTATE_COLORS.map(
            (color) =>
                `<button type="button" class="annotate-color-btn" data-color="${color}" title="Highlight ${color}"></button>`
        ).join("")}
        <span class="annotate-toolbar-sep"></span>
        <button type="button" class="annotate-toolbar-btn" id="annotate-note-btn">Note</button>
        <button type="button" class="annotate-toolbar-btn" id="annotate-copy-btn">Copy</button>
    `;

    const preventFocusLoss = (e) => e.preventDefault();

    toolbar.querySelectorAll(".annotate-color-btn").forEach((btn) => {
        btn.addEventListener("mousedown", preventFocusLoss);
        btn.addEventListener("click", () => {
            createHighlightFromSelection(btn.dataset.color);
        });
    });

    const noteBtn = document.getElementById("annotate-note-btn");
    noteBtn.addEventListener("mousedown", preventFocusLoss);
    noteBtn.addEventListener("click", async () => {
        const hl = await createHighlightFromSelection(ANNOTATE_DEFAULT_COLOR);
        if (hl) {
            // Seam for T3 (notes panel): the real note editor doesn't exist
            // yet, so this is judged/documented scope for THIS task — create
            // the highlight, announce it, and hand off via a CustomEvent
            // carrying the new highlight's uid. T3 listens for this to open
            // its editor pre-focused on the right highlight.
            document.dispatchEvent(
                new CustomEvent("tiro:highlight-created", { detail: { uid: hl.uid } })
            );
            showToast("Highlight added — notes editor arrives in a later task", "info");
        }
    });

    const copyBtn = document.getElementById("annotate-copy-btn");
    copyBtn.addEventListener("mousedown", preventFocusLoss);
    copyBtn.addEventListener("click", () => {
        copySelectionText();
    });

    // mouseup responds immediately (the Playwright spec dispatches this
    // directly after building a programmatic selection); selectionchange is
    // a debounced backstop for keyboard-driven selection changes (e.g.
    // shift+arrow) that don't necessarily fire a mouseup on the body.
    bodyEl.addEventListener("mouseup", () => {
        updateToolbarFromSelection(toolbar, bodyEl);
    });
    document.addEventListener("selectionchange", () => {
        clearTimeout(annotationSelectionDebounceTimer);
        annotationSelectionDebounceTimer = setTimeout(() => {
            updateToolbarFromSelection(toolbar, bodyEl);
        }, ANNOTATE_SELECTIONCHANGE_DEBOUNCE_MS);
    });

    document.addEventListener("mousedown", (e) => {
        if (toolbar.classList.contains("open") && !toolbar.contains(e.target)) {
            hideAnnotationToolbar(toolbar);
        }
    });
    window.addEventListener(
        "scroll",
        () => hideAnnotationToolbar(toolbar),
        { capture: true, passive: true }
    );
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && toolbar.classList.contains("open")) {
            hideAnnotationToolbar(toolbar);
        }
    });
}

function updateToolbarFromSelection(toolbar, bodyEl) {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
        hideAnnotationToolbar(toolbar);
        return;
    }
    const range = sel.getRangeAt(0);
    if (!bodyEl.contains(range.commonAncestorContainer)) {
        hideAnnotationToolbar(toolbar);
        return;
    }
    const text = sel.toString();
    if (!text || !text.trim()) {
        hideAnnotationToolbar(toolbar);
        return;
    }
    annotationSelectionRange = range.cloneRange();
    annotationSelectionText = text;
    showAnnotationToolbar(toolbar, range);
}

function showAnnotationToolbar(toolbar, range) {
    toolbar.classList.add("open");
    // Clickable content (color dots, Note, Copy) must not sit inside an
    // aria-hidden subtree — toggle the attribute in lockstep with .open
    // (the template ships aria-hidden="true" as the closed/default state).
    toolbar.removeAttribute("aria-hidden");
    const rects = range.getClientRects();
    const rect = rects.length ? rects[rects.length - 1] : range.getBoundingClientRect();

    // Measure after making it visible so offsetWidth/Height are accurate.
    const toolbarRect = toolbar.getBoundingClientRect();
    let left = rect.right - toolbarRect.width;
    let top = rect.bottom + 8;

    left = Math.max(8, Math.min(left, window.innerWidth - toolbarRect.width - 8));
    if (top + toolbarRect.height > window.innerHeight) {
        top = rect.top - toolbarRect.height - 8;
    }
    top = Math.max(8, top);

    toolbar.style.left = `${left}px`;
    toolbar.style.top = `${top}px`;
}

function hideAnnotationToolbar(toolbar) {
    toolbar.classList.remove("open");
    toolbar.setAttribute("aria-hidden", "true");
    annotationSelectionRange = null;
    annotationSelectionText = "";
}

/**
 * Map the current selection through buildTextIndex + projectMarkdown/
 * plainToMarkdownRange and POST it. Returns the created highlight dict on
 * success (with a synthesized `anchor_status` — see below), or `undefined`
 * on failure/unanchorable (toast already shown; caller doesn't need to).
 */
async function createHighlightFromSelection(color) {
    // In-flight guard (digestGenerating/analysisInFlight style, per
    // CLAUDE.md's convention): a rapid double-click on a color dot or the
    // Note button must fire exactly one POST, not two concurrent creates.
    if (annotateCreateInFlight) return undefined;
    annotateCreateInFlight = true;
    try {
        return await createHighlightFromSelectionInner(color);
    } finally {
        annotateCreateInFlight = false;
    }
}

async function createHighlightFromSelectionInner(color) {
    const toolbar = document.getElementById("annotate-toolbar");
    const range = annotationSelectionRange;
    const selText = annotationSelectionText;
    if (!range || !selText) return undefined;

    const bodyEl = document.getElementById("reader-body");
    const domText = annotationTextIndex.text;
    const domStart = domOffsetFromBoundary(bodyEl, range.startContainer, range.startOffset);
    const domEnd = domOffsetFromBoundary(bodyEl, range.endContainer, range.endOffset);
    const domPrefix = domText.slice(Math.max(0, domStart - ANNOTATE_CONTEXT_CHARS), domStart);
    const domSuffix = domText.slice(domEnd, domEnd + ANNOTATE_CONTEXT_CHARS);
    const approxPlainPos = domText.length
        ? Math.round((domStart / domText.length) * annotationProjection.plain.length)
        : 0;

    let found = findQuoteInPlain(annotationProjection.plain, selText, domPrefix, domSuffix, approxPlainPos);
    if (!found) {
        found = findQuoteInPlainFallback(annotationProjection.plain, selText, domPrefix, domSuffix, approxPlainPos);
    }
    if (!found) {
        showToast("Couldn't anchor this selection", "error");
        if (toolbar) hideAnnotationToolbar(toolbar);
        return undefined;
    }

    const mdRange = plainToMarkdownRange(annotationProjection, found.start, found.end);

    try {
        const res = await fetch(`/api/articles/${annotationArticleId}/highlights`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                position_start: mdRange.start,
                position_end: mdRange.end,
                color,
            }),
        });
        const json = await res.json().catch(() => null);
        if (!res.ok || !json || !json.success) {
            showToast((json && json.detail) || "Failed to save highlight", "error");
            return undefined;
        }

        const hl = json.data;
        // The create endpoint's response is a bare highlight row (no
        // anchor_status field — that's only computed by the GET/annotations
        // reconciler against the CURRENT body). A highlight is trivially
        // "exact" against the body it was JUST created from, so synthesize
        // the same shape paintHighlight() expects rather than refetching.
        hl.anchor_status = {
            status: "exact",
            position_start: hl.text_position_start,
            position_end: hl.text_position_end,
        };
        annotationHighlights.push(hl);
        paintHighlight(hl);
        // Task 3: keep the highlights panel's list in sync immediately, even
        // when the panel is closed or wasn't opened via the "Note" button
        // seam below (a plain color-dot click never dispatches
        // tiro:highlight-created) — otherwise the panel would show a stale
        // list (missing this highlight) until the next full page reload.
        renderHighlightsPanel();

        window.getSelection().removeAllRanges();
        if (toolbar) hideAnnotationToolbar(toolbar);
        return hl;
    } catch (err) {
        console.error("Highlight creation failed:", err);
        showToast("Failed to save highlight", "error");
        return undefined;
    }
}

async function copySelectionText() {
    const toolbar = document.getElementById("annotate-toolbar");
    const text = annotationSelectionText;
    if (!text) return;

    try {
        await navigator.clipboard.writeText(text);
        showToast("Copied to clipboard", "success");
    } catch (err) {
        // Fallback for browsers/contexts without the async Clipboard API
        // (e.g. non-secure context).
        try {
            const ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.focus();
            ta.select();
            document.execCommand("copy");
            document.body.removeChild(ta);
            showToast("Copied to clipboard", "success");
        } catch (fallbackErr) {
            console.error("Copy failed:", fallbackErr);
            showToast("Copy failed", "error");
        }
    }
    if (toolbar) hideAnnotationToolbar(toolbar);
}

/* --- Highlights & Notes panel (M2.2 Task 3) --- */

const HIGHLIGHT_QUOTE_TRUNCATE = 140;

let articleNoteState = null; // {uid, body_markdown, updated_at} | null, from the annotations GET
let noteSaveInFlight = false; // guard: article-note Save button
let noteClearInFlight = false; // guard: article-note Clear button
let highlightActionInFlight = new Set(); // guard: per-uid PATCH/DELETE (color/note/delete)

function setupHighlightsPanel(articleId) {
    const btn = document.getElementById("highlights-btn");
    const noteBtn = document.getElementById("article-note-btn");
    const panel = document.getElementById("highlights-panel");
    const overlay = document.getElementById("highlights-overlay");
    const closeBtn = document.getElementById("highlights-close");
    const drawer = document.getElementById("note-drawer");
    const listEl = document.getElementById("highlights-list");
    const warningListEl = document.getElementById("highlights-warning-list");
    if (!btn || !panel || !overlay || !closeBtn) return; // template didn't ship the panel — degrade silently

    function openPanel() {
        // Mutually exclusive with the analysis panel — see that panel's
        // openPanel() for the symmetric close-the-other-one call.
        document.getElementById("analysis-panel")?.classList.remove("open");
        document.getElementById("analysis-overlay")?.classList.remove("open");
        panel.classList.add("open");
        overlay.classList.add("open");
    }
    function closePanel() {
        panel.classList.remove("open");
        overlay.classList.remove("open");
    }

    btn.addEventListener("click", openPanel);
    closeBtn.addEventListener("click", closePanel);
    overlay.addEventListener("click", closePanel);

    if (noteBtn && drawer) {
        // Opens the panel (idempotently) with the note drawer expanded and
        // focused. NOT a strict open/closed toggle on repeat clicks: manual
        // testing showed the full-viewport backdrop overlay (same mechanism
        // `#analysis-overlay` already uses, `position: fixed; inset: 0`)
        // sits ABOVE the header buttons in stacking order once a panel is
        // open, so a "second click on this same header button" while the
        // panel is already open is never actually reachable through the UI
        // in the first place (the overlay intercepts the click and closes
        // the panel instead) — the same constraint the pre-existing analysis
        // panel already lives with for ITS header button. An idempotent
        // "always open + expand" is simpler and matches what's actually
        // clickable; collapsing the drawer without closing the whole panel
        // isn't reachable via this button and isn't offered elsewhere either
        // (documented judgment call — see task-3-report.md).
        noteBtn.addEventListener("click", () => {
            openPanel();
            drawer.classList.add("open");
            document.getElementById("article-note-textarea")?.focus();
        });
    }

    setupArticleNoteControls(articleId);
    if (listEl) attachHighlightsListEvents(listEl);
    if (warningListEl) attachHighlightsListEvents(warningListEl);

    // T2's seam (js/reader.js's annotate-toolbar "Note" button): a highlight
    // created from the floating selection toolbar dispatches this with the
    // new highlight's uid. Open the panel and jump straight to that
    // highlight's note editor, pre-focused.
    document.addEventListener("tiro:highlight-created", (e) => {
        openPanel();
        renderHighlightsPanel();
        focusHighlightNote(e.detail.uid);
    });
}

/** Re-render both the article-note drawer and the highlight list/warning
 * section from current in-memory state (`annotationHighlights` +
 * `articleNoteState`). Safe to call liberally after any mutation — a full
 * re-render is simpler and robust enough at this document's scale (one
 * article's highlights, not a corpus); the one known cost is that any OTHER
 * highlight's note editor that happened to be open gets collapsed back to
 * closed, since editor open/closed state isn't tracked separately from the
 * DOM (documented judgment call, not a bug — see task-3-report.md). */
function renderHighlightsPanel() {
    renderArticleNoteDrawer();

    const listEl = document.getElementById("highlights-list");
    const emptyEl = document.getElementById("highlights-empty");
    const warningSection = document.getElementById("highlights-warning-section");
    const warningListEl = document.getElementById("highlights-warning-list");
    if (!listEl || !emptyEl || !warningSection || !warningListEl) return;

    const sorted = sortedHighlightsForPanel();
    const normal = sorted.filter((hl) => !isWarningHighlight(hl));
    const warnings = sorted.filter(isWarningHighlight);

    emptyEl.style.display = sorted.length === 0 ? "block" : "none";
    listEl.innerHTML = normal.map((hl) => renderHighlightRow(hl, { full: false })).join("");

    if (warnings.length) {
        warningSection.style.display = "block";
        warningListEl.innerHTML = warnings.map((hl) => renderHighlightRow(hl, { full: true })).join("");
    } else {
        warningSection.style.display = "none";
        warningListEl.innerHTML = "";
    }
}

/** Highlights in DOCUMENT ORDER: sorted by their LIVE `anchor_status.
 * position_start` (exact/shifted only — that field is the reconciled,
 * possibly-relocated position, same field paintHighlight() itself reads),
 * with unanchored (hash_mismatch/missing) highlights sorted last. Ties
 * (including "no position" ties among unanchored highlights) keep the
 * server's own ordering (`ORDER BY text_position_start IS NULL,
 * text_position_start, created_at` — see routes_annotations.py's
 * get_annotations) via a stable index tiebreak, since Array.prototype.sort
 * is stable per spec but this makes that reliance explicit. */
function sortedHighlightsForPanel() {
    return annotationHighlights
        .map((hl, i) => ({ hl, i }))
        .sort((a, b) => {
            const diff = anchorSortKey(a.hl) - anchorSortKey(b.hl);
            if (diff !== 0) return diff;
            return a.i - b.i;
        })
        .map((w) => w.hl);
}

function anchorSortKey(hl) {
    const status = hl.anchor_status && hl.anchor_status.status;
    if (status === "exact" || status === "shifted") {
        const pos = hl.anchor_status.position_start;
        if (typeof pos === "number") return pos;
    }
    return Number.MAX_SAFE_INTEGER;
}

function isWarningHighlight(hl) {
    const status = hl.anchor_status && hl.anchor_status.status;
    return status === "hash_mismatch" || status === "missing";
}

function truncateQuote(text, max) {
    if (!text) return "";
    if (text.length <= max) return text;
    return text.slice(0, max - 1).trimEnd() + "…";
}

function warningBadgeLabel(hl) {
    const status = hl.anchor_status && hl.anchor_status.status;
    if (status === "missing") return "Text not found";
    if (status === "hash_mismatch") return "Content changed";
    return "Couldn't re-anchor";
}

/** One highlight row's HTML. `full: true` (the warning-section variant) shows
 * the FULL quote text (not truncated) plus a status badge, per the brief —
 * "with the quote text so the user can find it manually." Every server
 * string goes through esc(); the note body itself is never interpolated
 * here as raw HTML (only later, through renderMarkdown, in the preview
 * toggle). "find similar text" affordance is explicitly out of scope (noted
 * in task-3-report.md), matching the brief's own carve-out. */
function renderHighlightRow(hl, { full }) {
    const rawQuote = hl.quote_text || "";
    const quoteText = full ? rawQuote : truncateQuote(rawQuote, HIGHLIGHT_QUOTE_TRUNCATE);
    const quoteClass = full ? "highlight-quote-full" : "highlight-quote";
    const hasNote = !!(hl.note_markdown && hl.note_markdown.trim());
    const uid = esc(hl.uid);
    const badge = full
        ? `<span class="highlight-anchor-badge">${esc(warningBadgeLabel(hl))}</span>`
        : "";
    const noteIndicator = hasNote
        ? `<span class="highlight-note-indicator" title="Has a note">${icon("note", { size: 13 })}</span>`
        : "";
    const colorButtons = ANNOTATE_COLORS
        .map(
            (c) =>
                `<button type="button" class="highlight-color-btn" data-color="${c}" data-uid="${uid}" title="${c}"></button>`
        )
        .join("");

    return `
        <div class="highlight-row" data-uid="${uid}">
            ${badge}
            <div class="highlight-row-main">
                <span class="highlight-color-dot" data-color="${esc(hl.color)}"></span>
                <span class="${quoteClass}">${esc(quoteText)}</span>
                ${noteIndicator}
            </div>
            <div class="highlight-row-actions">
                <div class="highlight-color-picker">${colorButtons}</div>
                <button type="button" class="highlight-note-btn" data-uid="${uid}">${hasNote ? "Edit note" : "Add note"}</button>
                <button type="button" class="highlight-delete-btn" data-uid="${uid}" title="Delete highlight" aria-label="Delete highlight">${icon("trash", { size: 14 })}</button>
            </div>
            <div class="highlight-note-editor" id="highlight-note-editor-${uid}" style="display: none;">
                <textarea class="highlight-note-textarea" data-uid="${uid}">${esc(hl.note_markdown || "")}</textarea>
                <div class="highlight-note-preview" style="display: none;"></div>
                <div class="highlight-note-actions">
                    <button type="button" class="highlight-note-save-btn" data-uid="${uid}">Save</button>
                    <button type="button" class="highlight-note-preview-toggle-btn" data-uid="${uid}">Preview</button>
                    <button type="button" class="highlight-note-cancel-btn" data-uid="${uid}">Cancel</button>
                </div>
            </div>
        </div>
    `;
}

/** Single delegated click handler per list container (`#highlights-list` /
 * `#highlights-warning-list`), attached ONCE in setupHighlightsPanel — safe
 * across every renderHighlightsPanel() innerHTML replacement since the
 * listener lives on the (never-replaced) container element, not its
 * children. Order matters: specific action buttons are matched before the
 * generic "row click -> flash" fallback, and clicks anywhere inside an open
 * note editor (textarea/preview, not just its buttons) are excluded from
 * that fallback so placing a cursor to type a note doesn't also scroll/flash
 * the article body. */
function attachHighlightsListEvents(container) {
    container.addEventListener("click", (e) => {
        const colorBtn = e.target.closest(".highlight-color-btn");
        if (colorBtn) {
            updateHighlightColor(colorBtn.dataset.uid, colorBtn.dataset.color);
            return;
        }
        const noteBtn = e.target.closest(".highlight-note-btn");
        if (noteBtn) {
            toggleHighlightNoteEditor(noteBtn.dataset.uid);
            return;
        }
        const deleteBtn = e.target.closest(".highlight-delete-btn");
        if (deleteBtn) {
            deleteHighlightRow(deleteBtn.dataset.uid);
            return;
        }
        const saveBtn = e.target.closest(".highlight-note-save-btn");
        if (saveBtn) {
            saveHighlightNoteFromEditor(saveBtn.dataset.uid);
            return;
        }
        const previewBtn = e.target.closest(".highlight-note-preview-toggle-btn");
        if (previewBtn) {
            toggleHighlightNotePreview(previewBtn.dataset.uid);
            return;
        }
        const cancelBtn = e.target.closest(".highlight-note-cancel-btn");
        if (cancelBtn) {
            closeHighlightNoteEditor(cancelBtn.dataset.uid);
            return;
        }
        if (e.target.closest(".highlight-note-editor")) return;

        const row = e.target.closest(".highlight-row");
        if (row && row.dataset.uid) {
            flashHighlightRange(row.dataset.uid);
        }
    });
}

function toggleHighlightNoteEditor(uid) {
    const editor = document.getElementById(`highlight-note-editor-${uid}`);
    if (!editor) return;
    const isOpen = editor.style.display !== "none";
    // Only one note editor open at a time, panel-wide — collapse any other.
    document.querySelectorAll(".highlight-note-editor").forEach((el) => {
        if (el !== editor) el.style.display = "none";
    });
    editor.style.display = isOpen ? "none" : "block";
    if (!isOpen) {
        editor.querySelector(".highlight-note-textarea")?.focus();
    }
}

function closeHighlightNoteEditor(uid) {
    const editor = document.getElementById(`highlight-note-editor-${uid}`);
    if (editor) editor.style.display = "none";
}

/** Preview toggle: rendering goes ONLY through renderMarkdown (marked ->
 * DOMPurify) per the brief's XSS rule — never a raw innerHTML of the
 * textarea's value. */
function toggleHighlightNotePreview(uid) {
    const editor = document.getElementById(`highlight-note-editor-${uid}`);
    if (!editor) return;
    const textarea = editor.querySelector(".highlight-note-textarea");
    const preview = editor.querySelector(".highlight-note-preview");
    const btn = editor.querySelector(".highlight-note-preview-toggle-btn");
    if (!textarea || !preview || !btn) return;

    const showingPreview = preview.style.display !== "none";
    if (showingPreview) {
        preview.style.display = "none";
        textarea.style.display = "";
        btn.textContent = "Preview";
    } else {
        preview.innerHTML = renderMarkdown(textarea.value);
        preview.style.display = "block";
        textarea.style.display = "none";
        btn.textContent = "Edit";
    }
}

function saveHighlightNoteFromEditor(uid) {
    const editor = document.getElementById(`highlight-note-editor-${uid}`);
    const textarea = editor && editor.querySelector(".highlight-note-textarea");
    if (!textarea) return;
    saveHighlightNote(uid, textarea.value);
}

/** Merge a PATCH/create response into the local `annotationHighlights` cache
 * by uid. `anchor_status` is deliberately never touched here — PATCH's
 * response shape (`_highlight_row_to_dict`) has no such field (only the GET/
 * annotations reconciler computes it), so a spread merge naturally leaves
 * the previously-known anchor_status in place, which is correct: neither a
 * color nor a note edit changes a highlight's text position. */
function mergeHighlightUpdate(data) {
    const idx = annotationHighlights.findIndex((h) => h.uid === data.uid);
    if (idx !== -1) {
        annotationHighlights[idx] = { ...annotationHighlights[idx], ...data };
    }
}

async function updateHighlightColor(uid, color) {
    if (highlightActionInFlight.has(uid)) return;
    highlightActionInFlight.add(uid);
    try {
        const res = await fetch(`/api/highlights/${uid}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ color }),
        });
        const json = await res.json().catch(() => null);
        if (!res.ok || !json || !json.success) {
            showToast((json && json.detail) || "Failed to change highlight color", "error");
            return;
        }
        mergeHighlightUpdate(json.data);
        repaintHighlightColor(uid, json.data.color);
        renderHighlightsPanel();
    } catch (err) {
        console.error("Failed to change highlight color:", err);
        showToast("Failed to change highlight color", "error");
    } finally {
        highlightActionInFlight.delete(uid);
    }
}

/** Move the ALREADY-PAINTED Range for `uid` between color buckets directly —
 * no repaint pass (no re-running the DOM<->plain bridge), since a color
 * change never changes a highlight's text position. No-op if the highlight
 * isn't currently painted (CSS.highlights unsupported, or its anchor status
 * was hash_mismatch/missing and so was never painted in the first place). */
function repaintHighlightColor(uid, newColor) {
    const rec = annotationPaintedRanges.get(uid);
    if (!rec || !annotationCssSupported) return;
    const oldBucket = annotationHighlightObjects[rec.color];
    const resolvedColor = ANNOTATE_COLORS.includes(newColor) ? newColor : ANNOTATE_DEFAULT_COLOR;
    const newBucket = annotationHighlightObjects[resolvedColor];
    if (oldBucket) oldBucket.delete(rec.range);
    if (newBucket) newBucket.add(rec.range);
    rec.color = resolvedColor;
}

function unpaintHighlight(uid) {
    const rec = annotationPaintedRanges.get(uid);
    if (rec && annotationCssSupported) {
        annotationHighlightObjects[rec.color]?.delete(rec.range);
    }
    annotationPaintedRanges.delete(uid);
}

async function deleteHighlightRow(uid) {
    // Guard must be acquired BEFORE the confirm dialog, not after — two rapid
    // clicks on the delete button both got past a post-confirm guard check
    // (each opened its own confirmDialog before either could set the flag),
    // and confirming both stacked dialogs fired two DELETEs (the second
    // 404s, a spurious error toast). Acquiring here means the second click's
    // dialog never even opens; releasing in `finally` covers cancel and
    // error paths the same as the confirmed-success path.
    if (highlightActionInFlight.has(uid)) return;
    highlightActionInFlight.add(uid);
    try {
        const confirmed = await confirmDialog(
            "Delete this highlight? This cannot be undone.",
            { title: "Delete highlight", confirmText: "Delete", cancelText: "Cancel", danger: true }
        );
        if (!confirmed) return;
        const res = await fetch(`/api/highlights/${uid}`, { method: "DELETE" });
        const json = await res.json().catch(() => null);
        if (!res.ok || !json || !json.success) {
            showToast((json && json.detail) || "Failed to delete highlight", "error");
            return;
        }
        unpaintHighlight(uid);
        annotationHighlights = annotationHighlights.filter((h) => h.uid !== uid);
        renderHighlightsPanel();
        showToast("Highlight deleted", "success");
    } catch (err) {
        console.error("Failed to delete highlight:", err);
        showToast("Failed to delete highlight", "error");
    } finally {
        highlightActionInFlight.delete(uid);
    }
}

async function saveHighlightNote(uid, noteMarkdown) {
    if (highlightActionInFlight.has(uid)) return;
    highlightActionInFlight.add(uid);
    try {
        const res = await fetch(`/api/highlights/${uid}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ note_markdown: noteMarkdown }),
        });
        const json = await res.json().catch(() => null);
        if (!res.ok || !json || !json.success) {
            showToast((json && json.detail) || "Failed to save note", "error");
            return;
        }
        mergeHighlightUpdate(json.data);
        renderHighlightsPanel();
        showToast("Note saved", "success");
    } catch (err) {
        console.error("Failed to save highlight note:", err);
        showToast("Failed to save note", "error");
    } finally {
        highlightActionInFlight.delete(uid);
    }
}

/** Seam consumer for T2's `tiro:highlight-created` CustomEvent: opens (via
 * setupHighlightsPanel's listener, already called before this) the new
 * highlight's note editor and scrolls the panel row into view. Called after
 * renderHighlightsPanel() has already run for the freshly-created highlight,
 * so the row/editor DOM is guaranteed to exist. */
function focusHighlightNote(uid) {
    toggleHighlightNoteEditor(uid);
    document.querySelector(`.highlight-row[data-uid="${uid}"]`)?.scrollIntoView({ block: "center" });
}

/**
 * Click-to-open judgment call (documented per the brief's "judge and
 * document" instruction): only the PANEL-ROW -> BODY direction is
 * implemented. A panel row click scrolls the article body to the
 * ALREADY-COMPUTED painted Range (`annotationPaintedRanges`, populated by
 * paintHighlight — no re-running the DOM<->plain bridge) and flashes it via
 * a fifth, transient `tiro-hl-flash` Custom Highlight bucket cleared after
 * ~1.2s (a plain setTimeout, not a CSS transition/animation — ::highlight()
 * pseudo-element animation support is inconsistent across engines).
 *
 * The REVERSE direction (clicking a painted highlight IN the article body ->
 * open its panel row) is NOT implemented. CSS Custom Highlights carry no
 * click/pointer-event semantics of their own (unlike a wrapper `<mark>`
 * span, there's no element to attach a listener to) — resolving a body click
 * to "which highlight is under the cursor" would need
 * `document.caretPositionFromPoint`/`caretRangeFromPoint` (non-standard/
 * inconsistent browser support) to get a text offset, THEN a linear scan
 * over every painted Range's boundaries to find a containing one, on every
 * click anywhere in the article body. That's not "cheap" by the brief's own
 * bar, and the panel-row-driven direction already gets the user from any
 * highlight to its note/actions in one click from the panel — reserved as a
 * candidate for a future task if real usage shows the reverse direction is
 * needed.
 */
function flashHighlightRange(uid) {
    const rec = annotationPaintedRanges.get(uid);
    if (!rec) {
        // Unanchored (warning-section) highlights have no live Range — the
        // full quote text shown in that row is the user's manual-search aid
        // instead (the brief's "find similar text" affordance is out of
        // scope, per its own note).
        return;
    }
    const { range } = rec;
    const container = range.startContainer;
    const el = container.nodeType === Node.TEXT_NODE ? container.parentElement : container;
    el?.scrollIntoView({ behavior: "smooth", block: "center" });

    if (!annotationCssSupported) return;
    let flashBucket = CSS.highlights.get("tiro-hl-flash");
    if (!flashBucket) {
        flashBucket = new Highlight();
        CSS.highlights.set("tiro-hl-flash", flashBucket);
    }
    flashBucket.clear();
    flashBucket.add(range);
    setTimeout(() => flashBucket.clear(), 1200);
}

/* --- Article-level note drawer (M2.2 Task 3) --- */

function renderArticleNoteDrawer() {
    const textarea = document.getElementById("article-note-textarea");
    const preview = document.getElementById("article-note-preview");
    if (!textarea || !preview) return;
    // Don't clobber an in-progress edit on an unrelated re-render (e.g. a
    // highlight created elsewhere) — only sync from server state when the
    // textarea isn't the currently focused element.
    if (document.activeElement !== textarea) {
        textarea.value = articleNoteState ? articleNoteState.body_markdown || "" : "";
    }
    preview.innerHTML = textarea.value ? renderMarkdown(textarea.value) : "";
}

function setupArticleNoteControls(articleId) {
    const textarea = document.getElementById("article-note-textarea");
    const preview = document.getElementById("article-note-preview");
    const saveBtn = document.getElementById("article-note-save");
    const clearBtn = document.getElementById("article-note-clear");
    if (!textarea || !preview || !saveBtn || !clearBtn) return;

    // Live preview — renderMarkdown only, per the XSS rule.
    textarea.addEventListener("input", () => {
        preview.innerHTML = textarea.value ? renderMarkdown(textarea.value) : "";
    });

    saveBtn.addEventListener("click", async () => {
        if (noteSaveInFlight) return;
        noteSaveInFlight = true;
        try {
            const res = await fetch(`/api/articles/${articleId}/note`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ body_markdown: textarea.value }),
            });
            const json = await res.json().catch(() => null);
            if (!res.ok || !json || !json.success) {
                // 400 (whitespace-only body_markdown) surfaces here as a toast.
                showToast((json && json.detail) || "Failed to save note", "error");
                return;
            }
            articleNoteState = json.data;
            showToast("Note saved", "success");
        } catch (err) {
            console.error("Failed to save article note:", err);
            showToast("Failed to save note", "error");
        } finally {
            noteSaveInFlight = false;
        }
    });

    clearBtn.addEventListener("click", async () => {
        const confirmed = await confirmDialog(
            "Clear the article-level note? This cannot be undone.",
            { title: "Clear note", confirmText: "Clear", cancelText: "Cancel", danger: true }
        );
        if (!confirmed) return;
        if (noteClearInFlight) return;
        noteClearInFlight = true;
        try {
            const res = await fetch(`/api/articles/${articleId}/note`, { method: "DELETE" });
            const json = await res.json().catch(() => null);
            if (!res.ok || !json || !json.success) {
                showToast((json && json.detail) || "Failed to clear note", "error");
                return;
            }
            articleNoteState = null;
            textarea.value = "";
            preview.innerHTML = "";
            showToast("Note cleared", "success");
        } catch (err) {
            console.error("Failed to clear article note:", err);
            showToast("Failed to clear note", "error");
        } finally {
            noteClearInFlight = false;
        }
    });
}
