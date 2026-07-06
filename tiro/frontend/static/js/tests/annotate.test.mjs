// node:test coverage for js/annotate.js — PURE FUNCTIONS ONLY (M2.2 Task 1).
//
// annotate.js maps between markdown (anchor space, see tiro/anchors.py) and
// a plain-text projection approximating the rendered DOM. These tests cover
// the projection tables, offset-map exactness, the findQuoteInPlain
// candidate-scoring port of anchors.py's reconcile_anchor, and the inverse
// markdownQuoteToPlain lookup. No DOM, no jsdom — same harness policy as
// core.test.mjs.

import { test } from "node:test";
import assert from "node:assert/strict";
import {
    projectMarkdown,
    plainToMarkdownRange,
    findQuoteInPlain,
    markdownQuoteToPlain,
    findQuoteInPlainFallback,
    safeNFC,
    collapseWhitespaceWithMap,
    buildNormalizedProjection,
} from "../annotate.js";

// ---------------------------------------------------------------------
// projectMarkdown: inline/block syntax stripping table
// ---------------------------------------------------------------------

test("projectMarkdown: bold **text**", () => {
    assert.equal(projectMarkdown("**bold**").plain, "bold");
});

test("projectMarkdown: bold __text__", () => {
    assert.equal(projectMarkdown("__bold__").plain, "bold");
});

test("projectMarkdown: italic *text*", () => {
    assert.equal(projectMarkdown("*italic*").plain, "italic");
});

test("projectMarkdown: italic _text_", () => {
    assert.equal(projectMarkdown("_italic_").plain, "italic");
});

test("projectMarkdown: bold-italic ***text***", () => {
    assert.equal(projectMarkdown("***both***").plain, "both");
});

test("projectMarkdown: bold-italic ___text___", () => {
    assert.equal(projectMarkdown("___both___").plain, "both");
});

test("projectMarkdown: inline code keeps content, drops backticks", () => {
    assert.equal(projectMarkdown("`code`").plain, "code");
});

test("projectMarkdown: inline code content is literal (no nested emphasis stripping)", () => {
    assert.equal(projectMarkdown("`*not italic*`").plain, "*not italic*");
});

test("projectMarkdown: link [text](url) projects to text", () => {
    assert.equal(projectMarkdown("[a link](https://example.com)").plain, "a link");
});

test("projectMarkdown: image ![alt](url) projects to alt (documented decision)", () => {
    assert.equal(projectMarkdown("![an alt](https://example.com/x.png)").plain, "an alt");
});

test("projectMarkdown: image with empty alt projects to empty string", () => {
    assert.equal(projectMarkdown("![](https://example.com/x.png)").plain, "");
});

test("projectMarkdown: heading h1", () => {
    assert.equal(projectMarkdown("# Heading One").plain, "Heading One");
});

test("projectMarkdown: heading h2", () => {
    assert.equal(projectMarkdown("## Heading Two").plain, "Heading Two");
});

test("projectMarkdown: heading h3", () => {
    assert.equal(projectMarkdown("### Heading Three").plain, "Heading Three");
});

test("projectMarkdown: blockquote", () => {
    assert.equal(projectMarkdown("> a quote").plain, "a quote");
});

test("projectMarkdown: nested blockquote", () => {
    assert.equal(projectMarkdown(">> nested quote").plain, "nested quote");
});

test("projectMarkdown: fenced code block content kept verbatim, fence lines dropped", () => {
    const md = "before\n```\nconst x = 1;\n```\nafter";
    const { plain } = projectMarkdown(md);
    assert.equal(plain, "before\n\nconst x = 1;\n\nafter");
});

test("projectMarkdown: fenced code block content is NOT inline-processed", () => {
    const md = "```\n**not bold**\n```";
    const { plain } = projectMarkdown(md);
    assert.equal(plain, "\n**not bold**\n");
});

test("projectMarkdown: tilde-fenced code block", () => {
    const md = "~~~\ncode here\n~~~";
    assert.equal(projectMarkdown(md).plain, "\ncode here\n");
});

test("projectMarkdown: nested emphasis (bold inside italic)", () => {
    assert.equal(projectMarkdown("*italic **bold** italic*").plain, "italic bold italic");
});

test("projectMarkdown: nested emphasis (italic inside bold)", () => {
    assert.equal(projectMarkdown("**bold *italic* bold**").plain, "bold italic bold");
});

test("projectMarkdown: unicode emoji preserved", () => {
    assert.equal(projectMarkdown("**hello \u{1F600} world**").plain, "hello \u{1F600} world");
});

test("projectMarkdown: unicode U+00A0 (no-break space) preserved", () => {
    assert.equal(projectMarkdown("a b").plain, "a b");
});

test("projectMarkdown: unicode CJK preserved", () => {
    assert.equal(projectMarkdown("**你好世界**").plain, "你好世界");
});

test("projectMarkdown: plain paragraph unchanged", () => {
    const md = "Just a plain paragraph with no markdown syntax at all.";
    assert.equal(projectMarkdown(md).plain, md);
});

test("projectMarkdown: multi-paragraph newline preservation", () => {
    const md = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph.";
    assert.equal(projectMarkdown(md).plain, md);
});

test("projectMarkdown: empty markdown", () => {
    const { plain, map } = projectMarkdown("");
    assert.equal(plain, "");
    assert.deepEqual(map, []);
});

// ---------------------------------------------------------------------
// projectMarkdown: map invariant (markdown[map[i]] === plain[i])
// ---------------------------------------------------------------------

test("projectMarkdown: map invariant holds across a mixed-syntax document", () => {
    const md = "# Title\n\nA **bold** and *italic* [link](url) and `code` span.\n\n> quoted\n\n```\nfenced\n```\n";
    const { plain, map } = projectMarkdown(md);
    assert.equal(map.length, plain.length);
    for (let i = 0; i < plain.length; i++) {
        assert.equal(md[map[i]], plain[i], `map mismatch at plain index ${i}`);
    }
});

// ---------------------------------------------------------------------
// projectMarkdown: emphasis flanking rules (Finding 1 — reviewer-mandated
// regression coverage). CommonMark/marked do NOT emphasize intra-word `_`,
// and do NOT treat a whitespace-flanked `*`/`_` run as an emphasis
// delimiter. Without flanking checks the naive equal-length-closing-run
// matcher corrupts realistic tech-newsletter content (snake_case
// identifiers, spaced-out math).
// ---------------------------------------------------------------------

test("projectMarkdown: intra-word underscores (snake_case) are NOT emphasis", () => {
    assert.equal(projectMarkdown("foo_bar_baz").plain, "foo_bar_baz");
});

test("projectMarkdown: spaced asterisks around math are NOT emphasis", () => {
    const md = "solve a * b then c * d";
    assert.equal(projectMarkdown(md).plain, md);
});

test("projectMarkdown: simple italic *text* still strips", () => {
    assert.equal(projectMarkdown("*emphasis*").plain, "emphasis");
});

test("projectMarkdown: simple bold **text** still strips", () => {
    assert.equal(projectMarkdown("**bold**").plain, "bold");
});

test("projectMarkdown: word-boundary underscore italic _text_ still strips", () => {
    assert.equal(projectMarkdown("word _em_ word").plain, "word em word");
});

test("projectMarkdown: mixed snake_case identifier and real emphasis in one line", () => {
    const md = "snake_case and *real em*";
    assert.equal(projectMarkdown(md).plain, "snake_case and real em");
});

test("projectMarkdown: intra-word underscore at start of text (no preceding char) still opens", () => {
    // No char before the first '_' — not intra-word, so this DOES open.
    assert.equal(projectMarkdown("_em_ word").plain, "em word");
});

test("projectMarkdown: trailing intra-word underscore (word_) has no partner and stays literal", () => {
    assert.equal(projectMarkdown("word_ *and* word_").plain, "word_ and word_");
});

test("plainToMarkdownRange: offset after a kept intra-word underscore maps correctly", () => {
    const md = "foo_bar_baz rest of sentence";
    const projection = projectMarkdown(md);
    assert.equal(projection.plain, md); // nothing stripped, identity projection
    const plainStart = projection.plain.indexOf("rest of sentence");
    const plainEnd = plainStart + "rest of sentence".length;
    const range = plainToMarkdownRange(projection, plainStart, plainEnd);
    assert.equal(md.slice(range.start, range.end), "rest of sentence");
    // Identity mapping: markdown offsets equal plain offsets throughout,
    // since no characters were stripped by the (correctly, per Finding 1)
    // non-firing emphasis matcher.
    assert.equal(range.start, plainStart);
    assert.equal(range.end, plainEnd);
});

// ---------------------------------------------------------------------
// projectMarkdown: CRLF normalization (Finding 2)
// ---------------------------------------------------------------------

test("projectMarkdown: CRLF line endings produce no stray \\r in plain", () => {
    const md = "first line\r\nsecond line\r\nthird line";
    const { plain } = projectMarkdown(md);
    assert.equal(plain.includes("\r"), false);
    assert.equal(plain, "first line\nsecond line\nthird line");
});

test("projectMarkdown: CRLF map offsets stay exact past the CRLF boundary", () => {
    const md = "first line\r\nsecond line\r\nthird line";
    const projection = projectMarkdown(md);
    const { plain, map } = projectMarkdown(md);
    assert.equal(map.length, plain.length);
    for (let i = 0; i < plain.length; i++) {
        assert.equal(md[map[i]], plain[i], `map mismatch at plain index ${i}`);
    }
    // A selection on "second line" (after the CRLF boundary) must map back
    // to its exact markdown offsets, skipping over the "\r\n".
    const plainStart = projection.plain.indexOf("second line");
    const plainEnd = plainStart + "second line".length;
    const range = plainToMarkdownRange(projection, plainStart, plainEnd);
    assert.equal(md.slice(range.start, range.end), "second line");
});

// ---------------------------------------------------------------------
// projectMarkdown: surrogate-pair emoji index shift (Finding 3)
// ---------------------------------------------------------------------

test("projectMarkdown: content after a surrogate-pair emoji maps with exact UTF-16 indices", () => {
    // U+1F600 is encoded as a UTF-16 surrogate pair (2 code units), so
    // "hi \u{1F600} bye" is 3 + 2 (surrogate pair) + 1 + 3 = 9 code units
    // long, not 8 (which a naive codepoint-counting bug would produce).
    const md = "hi \u{1F600} bye";
    const projection = projectMarkdown(md);
    assert.equal(projection.plain, md);
    assert.equal(projection.map.length, md.length);
    // Identity mapping (nothing stripped): map[i] === i throughout.
    for (let i = 0; i < md.length; i++) {
        assert.equal(projection.map[i], i, `map mismatch at index ${i}`);
    }
    const byeStart = md.indexOf("bye");
    const range = plainToMarkdownRange(projection, byeStart, byeStart + 3);
    assert.deepEqual(range, { start: byeStart, end: byeStart + 3 });
});

// ---------------------------------------------------------------------
// projectMarkdown: map strictly increasing over mixed syntax (Finding 4)
// ---------------------------------------------------------------------

test("projectMarkdown: map is strictly increasing over a mixed-syntax document (binary-search precondition)", () => {
    const md =
        "# Title\n\nA **bold** and *italic* and _em_ and snake_case_word and\n" +
        "[link](url) and `code span` and a spaced * star * and \u{1F600} emoji.\n\n" +
        "> quoted\n\n```\nfenced\ncontent\n```\n\nlast line";
    const { map } = projectMarkdown(md);
    for (let i = 1; i < map.length; i++) {
        assert.ok(map[i] > map[i - 1], `map not strictly increasing at index ${i}: ${map[i - 1]} -> ${map[i]}`);
    }
});

// ---------------------------------------------------------------------
// projectMarkdown: code-span exact-length closing run (Finding 5)
// ---------------------------------------------------------------------

test("projectMarkdown: single-backtick code span does not close inside a longer backtick run", () => {
    // Content between the single opening backtick and the single closing
    // backtick includes a "``" (length-2) run — that run must NOT be
    // mistaken for a valid closer of the length-1 opener; the true close
    // is the lone backtick after " b".
    const md = "`a`` b`";
    const { plain } = projectMarkdown(md);
    assert.equal(plain, "a`` b");
});

test("projectMarkdown: double-backtick code span containing a single backtick", () => {
    const md = "``code with ` a single backtick``";
    const { plain } = projectMarkdown(md);
    assert.equal(plain, "code with ` a single backtick");
});

// ---------------------------------------------------------------------
// plainToMarkdownRange: offset-map exactness
// ---------------------------------------------------------------------

test("plainToMarkdownRange: unformatted prose round-trips exactly", () => {
    const md = "The quick brown fox jumps over the lazy dog.";
    const projection = projectMarkdown(md);
    const plainStart = projection.plain.indexOf("brown fox");
    const plainEnd = plainStart + "brown fox".length;
    const range = plainToMarkdownRange(projection, plainStart, plainEnd);
    assert.equal(md.slice(range.start, range.end), "brown fox");
});

test("plainToMarkdownRange: selection entirely inside emphasis content is exact", () => {
    const md = "before **bold content** after";
    const projection = projectMarkdown(md);
    const plainStart = projection.plain.indexOf("bold content");
    const plainEnd = plainStart + "bold content".length;
    const range = plainToMarkdownRange(projection, plainStart, plainEnd);
    assert.equal(md.slice(range.start, range.end), "bold content");
});

test("plainToMarkdownRange: selection crossing an emphasis boundary includes the syntax chars (documented, expected)", () => {
    const md = "**bold** text";
    const projection = projectMarkdown(md); // plain: "bold text"
    // Select the full "bold text" span, crossing the closing "**" boundary.
    const range = plainToMarkdownRange(projection, 0, projection.plain.length);
    const sliced = md.slice(range.start, range.end);
    // Contains the full selected semantic content...
    assert.ok(sliced.includes("bold"));
    assert.ok(sliced.includes("text"));
    // ...plus the crossed syntax characters (not a bug — see docstring).
    assert.ok(sliced.includes("**"));
});

test("plainToMarkdownRange: empty range at start", () => {
    const projection = projectMarkdown("hello world");
    const range = plainToMarkdownRange(projection, 0, 0);
    assert.equal(range.start, range.end);
    assert.equal(range.start, 0);
});

test("plainToMarkdownRange: empty range at end of plain text", () => {
    const projection = projectMarkdown("hello world");
    const range = plainToMarkdownRange(projection, projection.plain.length, projection.plain.length);
    assert.equal(range.start, range.end);
});

test("plainToMarkdownRange: full-document selection", () => {
    const md = "hello world";
    const projection = projectMarkdown(md);
    const range = plainToMarkdownRange(projection, 0, projection.plain.length);
    assert.equal(md.slice(range.start, range.end), "hello world");
});

test("plainToMarkdownRange: on empty markdown returns a degenerate zero range", () => {
    const projection = projectMarkdown("");
    const range = plainToMarkdownRange(projection, 0, 0);
    assert.deepEqual(range, { start: 0, end: 0 });
});

// ---------------------------------------------------------------------
// markdownQuoteToPlain: inverse lookup + round-trip
// ---------------------------------------------------------------------

test("markdownQuoteToPlain: round-trips exactly for unformatted prose", () => {
    const md = "The quick brown fox jumps over the lazy dog.";
    const projection = projectMarkdown(md);
    const mdStart = md.indexOf("brown fox");
    const mdEnd = mdStart + "brown fox".length;
    const plainRange = markdownQuoteToPlain(projection, mdStart, mdEnd);
    assert.ok(plainRange);
    assert.equal(projection.plain.slice(plainRange.start, plainRange.end), "brown fox");
});

test("markdownQuoteToPlain: plainToMarkdownRange -> markdownQuoteToPlain round-trip for prose", () => {
    const md = "Plain unformatted prose with no special characters here.";
    const projection = projectMarkdown(md);
    const plainStart = projection.plain.indexOf("unformatted prose");
    const plainEnd = plainStart + "unformatted prose".length;
    const mdRange = plainToMarkdownRange(projection, plainStart, plainEnd);
    const back = markdownQuoteToPlain(projection, mdRange.start, mdRange.end);
    assert.deepEqual(back, { start: plainStart, end: plainEnd });
});

test("markdownQuoteToPlain: range falling entirely inside stripped syntax returns null", () => {
    const md = "**bold**";
    const projection = projectMarkdown(md);
    // md[0:2] is "**" — the opening delimiter, entirely stripped syntax.
    const result = markdownQuoteToPlain(projection, 0, 2);
    assert.equal(result, null);
});

test("markdownQuoteToPlain: range inside a dropped fence line returns null", () => {
    const md = "```\ncode\n```";
    const projection = projectMarkdown(md);
    // md[0:3] is the opening fence "```", fully dropped.
    const result = markdownQuoteToPlain(projection, 0, 3);
    assert.equal(result, null);
});

test("markdownQuoteToPlain: partial overlap with syntax still resolves the plain content", () => {
    const md = "**bold** text";
    const projection = projectMarkdown(md);
    // md[0:9] is "**bold** " (leading syntax + trailing space) — the
    // resolvable plain content within it is "bold ".
    const result = markdownQuoteToPlain(projection, 0, 9);
    assert.ok(result);
    assert.equal(projection.plain.slice(result.start, result.end), "bold ");
});

test("markdownQuoteToPlain: null on empty projection", () => {
    const projection = projectMarkdown("");
    assert.equal(markdownQuoteToPlain(projection, 0, 1), null);
});

test("markdownQuoteToPlain: null when mdStart >= mdEnd", () => {
    const projection = projectMarkdown("hello world");
    assert.equal(markdownQuoteToPlain(projection, 5, 5), null);
    assert.equal(markdownQuoteToPlain(projection, 5, 3), null);
});

// ---------------------------------------------------------------------
// findQuoteInPlain: mirrors tiro/anchors.py's reconcile_anchor
// ---------------------------------------------------------------------

test("findQuoteInPlain: unique quote, no context needed", () => {
    const plain = "The quick brown fox jumps over the lazy dog.";
    const result = findQuoteInPlain(plain, "brown fox", "", "", 0);
    assert.deepEqual(result, { start: 10, end: 19 });
});

test("findQuoteInPlain: absent quote returns null", () => {
    const plain = "The quick brown fox.";
    assert.equal(findQuoteInPlain(plain, "not present", "", "", 0), null);
});

test("findQuoteInPlain: empty quote returns null", () => {
    assert.equal(findQuoteInPlain("some text", "", "", "", 0), null);
});

test("findQuoteInPlain: duplicate quote disambiguated by full prefix+suffix context", () => {
    const plain = "cat sat on the mat. cat ran to the door.";
    // Both occurrences of "cat" exist; only the second has this exact
    // context (" ran " suffix).
    const result = findQuoteInPlain(plain, "cat", ". ", " ran", 0);
    const secondCat = plain.indexOf("cat", 5);
    assert.deepEqual(result, { start: secondCat, end: secondCat + 3 });
});

test("findQuoteInPlain: duplicate quote disambiguated by partial context (prefix only)", () => {
    const plain = "xxcatxx aacatbb";
    // "aacat" prefix matches only the second occurrence.
    const result = findQuoteInPlain(plain, "cat", "aa", "", 0);
    const secondCat = plain.indexOf("cat", 5);
    assert.deepEqual(result, { start: secondCat, end: secondCat + 3 });
});

test("findQuoteInPlain: equal-context duplicates disambiguated by proximity to approxPos", () => {
    const plain = "cat here. cat here. cat here.";
    // All three "cat here" occurrences have identical (empty) context, so
    // score ties; proximity to approxPos must break the tie.
    const third = plain.lastIndexOf("cat");
    const result = findQuoteInPlain(plain, "cat", "", "", third - 1);
    assert.deepEqual(result, { start: third, end: third + 3 });
});

test("findQuoteInPlain: equal-context duplicates with no approxPos take the first occurrence", () => {
    const plain = "cat here. cat here.";
    const first = plain.indexOf("cat");
    const result = findQuoteInPlain(plain, "cat", "", "", undefined);
    assert.deepEqual(result, { start: first, end: first + 3 });
});

test("findQuoteInPlain: quote at position 0", () => {
    const plain = "start of the document";
    const result = findQuoteInPlain(plain, "start", "", " of", 0);
    assert.deepEqual(result, { start: 0, end: 5 });
});

test("findQuoteInPlain: quote at end of file", () => {
    const plain = "the document ends here";
    const result = findQuoteInPlain(plain, "here", "ends ", "", plain.length);
    const idx = plain.indexOf("here");
    assert.deepEqual(result, { start: idx, end: idx + 4 });
});

test("findQuoteInPlain: empty prefix and suffix still finds a unique quote", () => {
    const plain = "a unique needle in this text";
    const result = findQuoteInPlain(plain, "needle", "", "", 0);
    assert.deepEqual(result, { start: 9, end: 15 });
});

test("findQuoteInPlain: quote containing regex metacharacters is matched literally", () => {
    const plain = "cost is $5.00 (approx.) per unit [see note]";
    const result = findQuoteInPlain(plain, "$5.00 (approx.)", "", "", 0);
    const idx = plain.indexOf("$5.00 (approx.)");
    assert.deepEqual(result, { start: idx, end: idx + "$5.00 (approx.)".length });
});

test("findQuoteInPlain: __proto__ as quote text is handled safely as plain string content", () => {
    const plain = "before __proto__ after";
    const result = findQuoteInPlain(plain, "__proto__", "before ", " after", 0);
    assert.deepEqual(result, { start: 7, end: 16 });
});

test("findQuoteInPlain: overlapping repeats are all found as candidates", () => {
    // "aaa" inside "aaaaaa" has 4 overlapping occurrences (indices 0-3).
    const plain = "aaaaaa";
    const result = findQuoteInPlain(plain, "aaa", "", "", 3);
    // With empty context every occurrence ties at score 2 (trivial empty
    // match); proximity to approxPos=3 should pick index 3 or the nearest.
    assert.ok(result);
    assert.equal(plain.slice(result.start, result.end), "aaa");
});

test("findQuoteInPlain: proximity tiebreak picks the closest of several equally-scored candidates", () => {
    const plain = "aaaaaa";
    const result = findQuoteInPlain(plain, "aaa", "", "", 0);
    assert.deepEqual(result, { start: 0, end: 3 });
});

// ---------------------------------------------------------------------
// Normalization-bridge fallback (M2.2 Task 2): safeNFC,
// collapseWhitespaceWithMap, buildNormalizedProjection,
// findQuoteInPlainFallback
// ---------------------------------------------------------------------

test("safeNFC: length-preserving input is NFC-normalized", () => {
    // "é" (e + combining acute, 2 code units) composes to "é"
    // (é, 1 code unit) under NFC -- a LENGTH-CHANGING case, so safeNFC must
    // return the input unchanged rather than the shorter composed form.
    const decomposed = "é";
    assert.equal(safeNFC(decomposed), decomposed);
    // Plain ASCII is already NFC and length-preserving -- returned as-is
    // (value-equal; NFC is a no-op here).
    assert.equal(safeNFC("hello world"), "hello world");
});

test("collapseWhitespaceWithMap: collapses a run of spaces/tabs/newlines to one space", () => {
    const { normalized, map } = collapseWhitespaceWithMap("a   b\t\tc\n\nd");
    assert.equal(normalized, "a b c d");
    // map.length must equal normalized.length, and every mapped index must
    // point back at the FIRST char of its origin run in the source string.
    assert.equal(map.length, normalized.length);
    assert.deepEqual(map, [0, 1, 4, 5, 7, 8, 10]);
});

test("collapseWhitespaceWithMap: no whitespace is an identity transform", () => {
    const { normalized, map } = collapseWhitespaceWithMap("nowhitespace");
    assert.equal(normalized, "nowhitespace");
    assert.deepEqual(map, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
});

test("buildNormalizedProjection: mapEnd recovers the full origin span of a collapsed run", () => {
    const { normalized, map, mapEnd } = buildNormalizedProjection("a    b");
    assert.equal(normalized, "a b");
    assert.deepEqual(map, [0, 1, 5]);
    // The collapsed space (index 1 in normalized) came from a 4-char run
    // "    " spanning original indices [1, 5) -- mapEnd[1] must be 5.
    assert.deepEqual(mapEnd, [1, 5, 6]);
});

test("findQuoteInPlainFallback: resolves when whitespace runs differ between plain and the needle", () => {
    // Simulates a DOM-vs-markdown-projection mismatch: the projection has a
    // hard-wrapped double space where the DOM would have rendered a single
    // space. findQuoteInPlain (no normalization) fails to find the
    // single-spaced needle verbatim; the fallback must succeed.
    const plain = "the quick  brown fox jumps";
    assert.equal(findQuoteInPlain(plain, "quick brown", "the ", " fox"), null);
    const result = findQuoteInPlainFallback(plain, "quick brown", "the ", " fox", 4);
    assert.ok(result);
    assert.equal(plain.slice(result.start, result.end), "quick  brown");
});

test("findQuoteInPlainFallback: returned offsets are in plain-space, not normalized-space", () => {
    // Multiple leading collapsed-whitespace runs before the match shift
    // normalized-space indices left of plain-space indices -- the fallback
    // must translate back correctly, not return raw normalized offsets.
    const plain = "line one\n\n\nline two   has   extra   spaces";
    const needle = "has extra spaces";
    assert.equal(findQuoteInPlain(plain, needle), null);
    const result = findQuoteInPlainFallback(plain, needle, "", "", 30);
    assert.ok(result);
    assert.equal(plain.slice(result.start, result.end), "has   extra   spaces");
});

test("findQuoteInPlainFallback: null when the quote is absent even after normalization", () => {
    const plain = "nothing matches here";
    assert.equal(findQuoteInPlainFallback(plain, "absent phrase", "", "", 0), null);
});

test("findQuoteInPlainFallback: null on empty quote", () => {
    assert.equal(findQuoteInPlainFallback("some text", "", "", "", 0), null);
});

test("findQuoteInPlainFallback: null on empty plain", () => {
    assert.equal(findQuoteInPlainFallback("", "quote", "", "", 0), null);
});

test("findQuoteInPlainFallback: exact match (no normalization needed) still resolves", () => {
    const plain = "a unique needle in this text";
    const result = findQuoteInPlainFallback(plain, "needle", "", "", 0);
    assert.deepEqual(result, { start: 9, end: 15 });
});
