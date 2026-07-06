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
 */

import {
    esc,
    num,
    formatDate,
    renderMarkdown,
    showToast,
    timeAgo,
} from "./core.js";
import { showShortcuts, hideShortcuts } from "./sidebar.js";
import {
    projectMarkdown,
    plainToMarkdownRange,
    markdownQuoteToPlain,
    findQuoteInPlain,
    findQuoteInPlainFallback,
} from "./annotate.js";

document.addEventListener("DOMContentLoaded", () => {
    const reader = document.getElementById("reader");
    const articleId = reader.dataset.articleId;
    loadArticle(articleId);
    setupReaderKeyboard(articleId);
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

        // VIP indicator (always show, make clickable)
        const vip = document.getElementById("reader-vip");
        if (a.source_id) {
            vip.style.display = "inline";
            vip.dataset.sourceId = a.source_id;
            if (a.is_vip) vip.classList.add("active");
            vip.addEventListener("click", async () => {
                try {
                    const res = await fetch(`/api/sources/${a.source_id}/vip`, { method: "PATCH" });
                    const json = await res.json();
                    if (json.success) {
                        vip.classList.toggle("active");
                    }
                } catch (err) {
                    console.error("VIP toggle failed:", err);
                }
            });
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

        // Original URL
        const linkEl = document.getElementById("reader-original-link");
        if (a.url) {
            linkEl.href = a.url;
            linkEl.textContent = new URL(a.url).hostname;
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

        // Annotations (highlights + selection toolbar) — must run AFTER the
        // body's innerHTML is set (buildTextIndex walks the rendered DOM).
        setupAnnotations(a.id, a.content || "");

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
            <button class="analysis-refresh-btn">Re-analyze</button>
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
        playBtn.innerHTML = "&#9646;&#9646;";
        playBtn.disabled = false;
        audioState.playing = true;
    });
    audio.addEventListener("pause", () => {
        playBtn.innerHTML = "&#9654;";
        playBtn.disabled = false;
        audioState.playing = false;
    });
    audio.addEventListener("ended", () => {
        playBtn.innerHTML = "&#9654;";
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

    playBtn.innerHTML = "&#9646;&#9646;";

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
        playBtn.innerHTML = "&#9654;";
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
        playBtn.innerHTML = "&#9654;";
        audioState.playing = false;
    } else if (speechSynthesis.paused) {
        speechSynthesis.resume();
        playBtn.innerHTML = "&#9646;&#9646;";
        audioState.playing = true;
    }
}

function formatAudioTime(seconds) {
    if (!seconds || !isFinite(seconds)) return "0:00";
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return m + ":" + (s < 10 ? "0" : "") + s;
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

function setupAnnotations(articleId, articleContent) {
    annotationArticleId = articleId;
    const bodyEl = document.getElementById("reader-body");
    annotationProjection = projectMarkdown(articleContent);
    annotationTextIndex = buildTextIndex(bodyEl);
    annotationHighlights = [];

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
    try {
        const res = await fetch(`/api/articles/${articleId}/annotations`);
        const json = await res.json();
        if (!json.success) return;
        annotationHighlights = json.data.highlights || [];
        annotationHighlights.forEach(paintHighlight);
    } catch (err) {
        console.error("Failed to load annotations:", err);
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

    const bucket = annotationHighlightObjects[hl.color] || annotationHighlightObjects[ANNOTATE_DEFAULT_COLOR];
    try {
        bucket.add(range);
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
