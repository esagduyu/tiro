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
