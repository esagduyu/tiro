/* Tiro — reader annotation pure core (M2.2 Task 1).
 *
 * Pure text-space mapping functions for the reader annotation UI. NO DOM, no
 * fetch, no imports — this module only ever sees strings/numbers in and
 * out, so it is trivially unit-testable under node:test and safely reusable
 * by whichever layer eventually turns a DOM Selection into a highlight (T2)
 * and posts it to the M2.1 backend (T3).
 *
 * Three text spaces are in play for a reader highlight:
 *   1. MARKDOWN space — the article's stored markdown file. Anchors
 *      (`tiro/anchors.py`) are offsets into THIS space: `{quote, prefix,
 *      suffix, position_start, position_end}`.
 *   2. RENDERED DOM space — what the user actually sees and selects
 *      (marked.js output, sanitized). NOT this module's problem.
 *   3. PLAIN-TEXT PROJECTION — an intermediate space this module invents:
 *      markdown with inline syntax stripped and block structure collapsed
 *      to newlines. It approximates what the reader visually sees (close
 *      to the DOM's `textContent`) while staying cheap to compute from the
 *      markdown alone, with an exact index map back to markdown offsets.
 *      T2 uses this projection to convert DOM selections into markdown
 *      anchors without needing a full DOM-to-markdown reverse renderer.
 *
 * Map representation (`projectMarkdown`'s returned `map`): a plain array,
 * parallel to `plain` (`map.length === plain.length`), where `map[i]` is the
 * markdown string index such that `markdown[map[i]] === plain[i]` — i.e. a
 * per-character origin map. Every plain character is a LITERAL copy of some
 * markdown character (stripping only ever deletes syntax characters, never
 * transforms/decodes/reorders content characters), so this invariant always
 * holds, `map` is always strictly increasing, and both directions
 * (`plainToMarkdownRange` / `markdownQuoteToPlain`) reduce to searches over
 * it. A packed/run-length representation would be more compact but a flat
 * array keeps both directions a one-liner; article sizes here are small
 * enough (single documents, not corpora) that this isn't a perf concern.
 *
 * Decisions worth flagging for T2/T3 (see task-1-report.md for the full
 * writeup):
 *   - Image `![alt](url)` projects to `alt`, taken LITERALLY (no nested
 *     inline processing) — alt text is already conceptually plain.
 *   - Fenced code block DELIMITER lines (``` / ~~~) are dropped from the
 *     projection entirely (no plain chars emitted for the fence markers
 *     themselves), but the line's own newline is still emitted, so the code
 *     content is bordered by block-separator newlines rather than fused
 *     into an adjacent paragraph. Content lines INSIDE the fence are kept
 *     verbatim (no inline stripping — code is literal).
 *   - Emphasis matching (`*`/`_`/`**`/`__`/`***`/`___`) requires an EXACT
 *     equal-length closing run rather than implementing full CommonMark
 *     delimiter-run flanking rules. Sufficient for the supported test
 *     table (bold/italic/bold-italic/nested); pathological runs (4+ of the
 *     same delimiter, mismatched split runs) fall back to literal text.
 *   - `plainToMarkdownRange`'s returned range can and does include syntax
 *     characters that sit INSIDE the selected span (e.g. selecting across
 *     a `**` boundary pulls the asterisks into the markdown range) — this
 *     is correct per the task brief, not a bug.
 */

/**
 * Project `markdown` into a plain-text approximation with an exact
 * character-origin map back to markdown offsets. See module docstring for
 * the map representation and the fence/image/emphasis decisions.
 *
 * @param {string} markdown
 * @returns {{plain: string, map: number[]}}
 */
export function projectMarkdown(markdown) {
    const plainArr = [];
    const mapArr = [];
    const lines = markdown.split("\n");
    let mdOffset = 0;
    let inFence = false;

    for (let li = 0; li < lines.length; li++) {
        const line = lines[li];
        const lineMdStart = mdOffset;
        const fenceMatch = /^ {0,3}(`{3,}|~{3,})/.exec(line);

        if (fenceMatch) {
            // Fence delimiter line (open or close): dropped from the
            // projection — see module docstring's fence decision.
            inFence = !inFence;
        } else if (inFence) {
            // Inside a fenced code block: verbatim, no inline stripping.
            emitLiteral(line, lineMdStart, plainArr, mapArr);
        } else {
            const prefixLen = blockPrefixLength(line);
            const content = line.slice(prefixLen);
            processInline(content, lineMdStart + prefixLen, plainArr, mapArr);
        }

        // Block structure survives as newlines: every source line boundary
        // (including blank lines and dropped fence-delimiter lines, whose
        // content was empty but whose newline still counts) becomes one
        // plain '\n'. This is what makes blank-line paragraph breaks in
        // markdown show up as blank plain lines too.
        if (li < lines.length - 1) {
            plainArr.push("\n");
            mapArr.push(lineMdStart + line.length);
        }

        mdOffset += line.length + 1; // +1 for the '\n' consumed by split()
    }

    return { plain: plainArr.join(""), map: mapArr };
}

/**
 * Markdown offsets whose slice covers exactly the plain range
 * `[plainStart, plainEnd)`. Syntax characters sitting INSIDE the span
 * (e.g. a `**` boundary crossed by the selection) are included in the
 * returned range — that's correct per the projection's design, not a bug.
 *
 * @param {{plain: string, map: number[]}} projection
 * @param {number} plainStart
 * @param {number} plainEnd
 * @returns {{start: number, end: number}}
 */
export function plainToMarkdownRange(projection, plainStart, plainEnd) {
    const { plain, map } = projection;
    const len = plain.length;
    if (len === 0) return { start: 0, end: 0 };

    const s = Math.max(0, Math.min(plainStart, len));
    const e = Math.max(0, Math.min(plainEnd, len));

    // Start boundary: the source index of the first included char (or, for
    // an empty/at-end range, one past the last char in the projection).
    const start = s < len ? map[s] : map[len - 1] + 1;
    if (e <= s) return { start, end: start };

    // End boundary (exclusive): one past the source index of the LAST
    // included char (map[e - 1]) — not map[e] itself, which would instead
    // reach forward to the start of the next plain char and could swallow
    // trailing syntax that sits between the selection and that next char
    // (e.g. a `*` closing an emphasis run immediately after the selection).
    const end = map[e - 1] + 1;
    return { start, end };
}

/**
 * Inverse of `plainToMarkdownRange`: the plain-text range whose origin
 * chars fall inside markdown `[mdStart, mdEnd)`. Used to paint a stored
 * anchor (markdown offsets) back onto the rendered projection. Returns
 * `null` when the markdown range contains no projected characters at all
 * (i.e. it falls entirely inside stripped syntax — a heading marker, an
 * emphasis delimiter, a dropped fence line, etc).
 *
 * `map` is strictly increasing (see module docstring), so both bounds are a
 * binary search.
 *
 * @param {{plain: string, map: number[]}} projection
 * @param {number} mdStart
 * @param {number} mdEnd
 * @returns {{start: number, end: number} | null}
 */
export function markdownQuoteToPlain(projection, mdStart, mdEnd) {
    const { map } = projection;
    if (map.length === 0 || mdStart >= mdEnd) return null;

    const lo = lowerBound(map, mdStart);
    if (lo === map.length || map[lo] >= mdEnd) return null;

    const hi = lowerBound(map, mdEnd) - 1;
    if (hi < lo) return null;

    return { start: lo, end: hi + 1 };
}

/**
 * Locate `quote` (with optional `prefix`/`suffix` context) inside `plain`.
 *
 * MIRRORS `tiro/anchors.py`'s `reconcile_anchor` candidate-selection logic
 * (see that module's docstring + `_find_all`/`_context_score`/
 * `reconcile_anchor`) one-to-one, applied to the plain projection instead
 * of markdown, and without the hash-fallback branches (this is a UI
 * best-effort locator, not the authoritative server-side reconciler):
 *   1. Find every occurrence of `quote` in `plain` via `indexOf` scanning
 *      (never a RegExp — `quote` is untrusted/arbitrary text that may
 *      contain regex metacharacters), including overlapping occurrences
 *      (mirrors `_find_all`'s `idx = text.find(needle, idx + 1)` stepping).
 *   2. Score each occurrence by context match quality — full prefix+suffix
 *      match = 2, partial (prefix-only or suffix-only) = 1, bare quote
 *      match with neither = 0 (mirrors `_context_score` exactly, including
 *      its property that empty prefix/suffix trivially "match" everywhere,
 *      pushing disambiguation onto the proximity tiebreak).
 *   3. Keep only the max-scoring occurrences; if more than one remains and
 *      `approxPos` is given, break the tie by proximity to `approxPos`
 *      (mirrors `reconcile_anchor`'s `min(..., key=lambda s: abs(s -
 *      stored_start))`); otherwise take the first candidate found.
 *
 * Returns `null` when `quote` is falsy or not found anywhere in `plain`.
 *
 * @param {string} plain
 * @param {string} quote
 * @param {string} [prefix]
 * @param {string} [suffix]
 * @param {number} [approxPos]
 * @returns {{start: number, end: number} | null}
 */
export function findQuoteInPlain(plain, quote, prefix, suffix, approxPos) {
    if (!quote) return null;

    const occurrences = findAllOccurrences(plain, quote);
    if (occurrences.length === 0) return null;

    const p = prefix || "";
    const s = suffix || "";

    let bestScore = -1;
    const scored = [];
    for (const start of occurrences) {
        const end = start + quote.length;
        const actualPrefix = plain.slice(Math.max(0, start - p.length), start);
        const actualSuffix = plain.slice(end, end + s.length);
        const prefixMatch = actualPrefix === p;
        const suffixMatch = actualSuffix === s;
        const score = prefixMatch && suffixMatch ? 2 : prefixMatch || suffixMatch ? 1 : 0;
        scored.push({ start, end, score });
        if (score > bestScore) bestScore = score;
    }

    const top = scored.filter((c) => c.score === bestScore);
    let winner;
    if (top.length === 1 || approxPos === null || approxPos === undefined) {
        winner = top[0];
    } else {
        winner = top.reduce((best, c) =>
            Math.abs(c.start - approxPos) < Math.abs(best.start - approxPos) ? c : best
        );
    }
    return { start: winner.start, end: winner.end };
}

// ---------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------

/** All (possibly overlapping) start indices of `needle` in `text`. Direct
 * JS port of anchors.py's `_find_all` (indexOf instead of str.find). */
function findAllOccurrences(text, needle) {
    if (!needle) return [];
    const positions = [];
    let idx = text.indexOf(needle);
    while (idx !== -1) {
        positions.push(idx);
        idx = text.indexOf(needle, idx + 1);
    }
    return positions;
}

/** First index in strictly-increasing `arr` with `arr[i] >= value`. */
function lowerBound(arr, value) {
    let lo = 0;
    let hi = arr.length;
    while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (arr[mid] < value) lo = mid + 1;
        else hi = mid;
    }
    return lo;
}

/** Push each char of `str` (a literal, untransformed slice of the source
 * markdown starting at `mdStart`) onto plainArr/mapArr, preserving the
 * markdown[map[i]] === plain[i] invariant character-by-character. */
function emitLiteral(str, mdStart, plainArr, mapArr) {
    for (let k = 0; k < str.length; k++) {
        plainArr.push(str[k]);
        mapArr.push(mdStart + k);
    }
}

/** Length of a leading ATX heading marker (`#` through `######` + required
 * whitespace or end-of-line) or a leading blockquote marker run (one or
 * more `>` each with an optional following space, e.g. nested `>>`), else
 * 0 for a line with no block-level prefix to strip. */
function blockPrefixLength(line) {
    let m = /^ {0,3}#{1,6}(?:\s+|$)/.exec(line);
    if (m) return m[0].length;

    m = /^(?: {0,3}>[ \t]?)+/.exec(line);
    if (m) return m[0].length;

    return 0;
}

/** Scan `text` (a slice of markdown starting at absolute offset `base`) for
 * inline syntax, emitting the stripped-down plain content into
 * plainArr/mapArr. Images/links/code-spans/emphasis are recognized by
 * direct character scanning (never RegExp substitution) so origin offsets
 * stay exact and content that happens to look like markdown (inside a
 * matched span) isn't double-processed incorrectly. */
function processInline(text, base, plainArr, mapArr) {
    let i = 0;
    const n = text.length;

    while (i < n) {
        const ch = text[i];

        if (ch === "!" && text[i + 1] === "[") {
            const link = parseLinkOrImage(text, i + 1);
            if (link) {
                // Image alt text is kept literally (no nested inline
                // processing) — see module docstring's image decision.
                emitLiteral(link.inner, base + i + 2, plainArr, mapArr);
                i = link.next;
                continue;
            }
        }

        if (ch === "[") {
            const link = parseLinkOrImage(text, i);
            if (link) {
                processInline(link.inner, base + i + 1, plainArr, mapArr);
                i = link.next;
                continue;
            }
        }

        if (ch === "`") {
            let runLen = 1;
            while (text[i + runLen] === "`") runLen++;
            const marker = "`".repeat(runLen);
            const contentStart = i + runLen;
            const closeIdx = text.indexOf(marker, contentStart);
            if (closeIdx !== -1) {
                // Code span content is literal — no nested emphasis
                // processing, matching CommonMark code-span semantics.
                emitLiteral(text.slice(contentStart, closeIdx), base + contentStart, plainArr, mapArr);
                i = closeIdx + runLen;
                continue;
            }
        }

        if (ch === "*" || ch === "_") {
            let runLen = 1;
            while (text[i + runLen] === ch) runLen++;
            const contentStart = i + runLen;
            const closeIdx = findClosingRun(text, contentStart, ch, runLen);
            if (closeIdx !== -1 && closeIdx > contentStart) {
                processInline(text.slice(contentStart, closeIdx), base + contentStart, plainArr, mapArr);
                i = closeIdx + runLen;
                continue;
            }
        }

        plainArr.push(ch);
        mapArr.push(base + i);
        i++;
    }
}

/** `text[bracketIdx]` must be `[`. Matches `[inner](url)` with no nested
 * bracket/paren support (out of scope for the reader's inline content). */
function parseLinkOrImage(text, bracketIdx) {
    const closeBracket = text.indexOf("]", bracketIdx + 1);
    if (closeBracket === -1 || text[closeBracket + 1] !== "(") return null;

    const closeParen = text.indexOf(")", closeBracket + 2);
    if (closeParen === -1) return null;

    return {
        inner: text.slice(bracketIdx + 1, closeBracket),
        next: closeParen + 1,
    };
}

/** First index at/after `from` where `ch` repeats EXACTLY `exactLen`
 * times (not part of a longer or shorter run) — an "equal-length closing
 * run" emphasis matcher, deliberately simpler than full CommonMark
 * delimiter-run flanking rules (see module docstring). */
function findClosingRun(text, from, ch, exactLen) {
    let idx = from;
    while (idx < text.length) {
        if (text[idx] === ch) {
            let runLen = 1;
            while (text[idx + runLen] === ch) runLen++;
            if (runLen === exactLen) return idx;
            idx += runLen;
        } else {
            idx++;
        }
    }
    return -1;
}
