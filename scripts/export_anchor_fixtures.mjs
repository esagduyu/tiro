#!/usr/bin/env node
/*
 * export_anchor_fixtures.mjs — anchor-parity fixture exporter (Task 2, iOS campaign).
 *
 * Produces the FROZEN test-vector file the Swift AnchorKit port (tiro-ios,
 * Task 8) is validated against. A parity bug in that port silently anchors
 * highlights to the WRONG text range on iOS with no server-side detection, so
 * these vectors must record what `annotate.js` ACTUALLY does — never a
 * hand-computed expectation.
 *
 * HOW EXPECTATIONS ARE DERIVED (the whole point): every `plain`, `map`, and
 * search result below is produced by CALLING the real, unmodified exports of
 * `tiro/frontend/static/js/annotate.js` — `projectMarkdown` for the
 * projection, and a `resolveSearch` composed of the real `findQuoteInPlain`
 * + `findQuoteInPlainFallback` for each search. There is not a single
 * hand-written expected offset in this file (grep for `expect` — the only
 * occurrences are the emitted keys, assigned from `.start`/`.end`).
 *
 * FROZEN SCHEMA (normative — ios-app-spec §7 / task-2 brief): the output is a
 * JSON array of vectors:
 *   {
 *     name:     string,
 *     markdown: string,            // the input document
 *     plain:    string,            // projectMarkdown(markdown).plain
 *     map:      number[],          // projectMarkdown(markdown).map, VERBATIM.
 *                                  //   map[i] = markdown offset of plain char i
 *                                  //   (markdown[map[i]] === plain[i]). This is
 *                                  //   already exactly annotate.js's returned
 *                                  //   `map` shape — no conversion applied.
 *     searches: [
 *       { quote, prefix, suffix,   // the search NEEDLES (inputs, hand-chosen)
 *         expect_start,            // resolveSearch(...).start, or null on soft-fail
 *         expect_end }             // resolveSearch(...).end,   or null on soft-fail
 *     ]
 *   }
 *
 * A `null` expect pair means "must SOFT-FAIL" — the search returns null through
 * BOTH the exact locator and the normalization-bridge fallback. Per the M2.2
 * bullet in CLAUDE.md, HTML-entity decoding and smart-punctuation substitution
 * (curly quotes / em-dashes) and length-CHANGING NFC composition are
 * deliberately NOT bridged; those are the null vectors here.
 *
 * approxPos NOTE: the frozen search schema has NO approxPos field, so every
 * search is run with approxPos === undefined. Per `findQuoteInPlain`, that
 * means the proximity tiebreak never fires and the FIRST of the top-scoring
 * candidates wins. This is a deliberate, schema-mandated simplification; the
 * Swift port validates the same call shape.
 *
 * DETERMINISM: vectors and searches are emitted in fixed source insertion
 * order (JS objects/arrays preserve it); output is JSON.stringify(_, null, 2)
 * with a trailing newline. No timestamps, no randomness — re-running is
 * byte-identical (guarded by fixtures.test.mjs).
 *
 * The exporter self-validates: each search may carry an internal `assert`
 * ("found" | "null") that is checked against the REAL result and stripped
 * before emit. If a designated null case ever starts resolving (or vice
 * versa), generation throws instead of silently freezing a wrong vector.
 */

import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { writeFileSync } from "node:fs";
import {
    projectMarkdown,
    findQuoteInPlain,
    findQuoteInPlainFallback,
} from "../tiro/frontend/static/js/annotate.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUTPUT_PATH = join(
    __dirname,
    "..",
    "tiro",
    "frontend",
    "static",
    "js",
    "tests",
    "fixtures",
    "anchor-parity.json"
);

/**
 * The reader's real anchor-resolution order: exact locator first, then the
 * normalization-bridge fallback. Run with approxPos undefined per the frozen
 * schema (no approxPos field). Returns {start, end} or null.
 */
function resolveSearch(plain, quote, prefix, suffix) {
    const primary = findQuoteInPlain(plain, quote, prefix, suffix);
    if (primary) return primary;
    return findQuoteInPlainFallback(plain, quote, prefix, suffix);
}

/**
 * Build one output vector by running the REAL annotate.js functions over
 * `markdown` and each search needle. `searchInputs` entries are the hand-chosen
 * needles ({quote, prefix?, suffix?, assert?}); expectations come only from the
 * real functions. `assert` ("found"|"null") is verified then stripped.
 */
function makeVector(name, markdown, searchInputs = []) {
    const { plain, map } = projectMarkdown(markdown);
    const searches = searchInputs.map((input) => {
        const quote = input.quote;
        const prefix = input.prefix ?? "";
        const suffix = input.suffix ?? "";
        const result = resolveSearch(plain, quote, prefix, suffix);

        if (input.assert === "found" && !result) {
            throw new Error(
                `Vector "${name}": search for ${JSON.stringify(quote)} was expected to RESOLVE but soft-failed.`
            );
        }
        if (input.assert === "null" && result) {
            throw new Error(
                `Vector "${name}": search for ${JSON.stringify(quote)} was expected to SOFT-FAIL but resolved to ${JSON.stringify(result)}.`
            );
        }

        return {
            quote,
            prefix,
            suffix,
            expect_start: result ? result.start : null,
            expect_end: result ? result.end : null,
        };
    });
    return { name, markdown, plain, map, searches };
}

// Unicode escapes used below (kept as escapes so this source stays ASCII and
// diffable; the emitted JSON carries the real characters):
const NBSP = " "; // no-break space
const EMOJI = "\u{1F600}"; // grinning face (surrogate pair)
const ANGSTROM = "Å"; // ANGSTROM SIGN — NFC-composes (singleton) to U+00C5, length-preserving
const A_RING = "Å"; // LATIN CAPITAL A WITH RING ABOVE (precomposed)
const E_ACUTE_DECOMP = "é"; // e + COMBINING ACUTE ACCENT (decomposed é; NFC is length-CHANGING)
const E_ACUTE_PRECOMP = "é"; // é precomposed
const LDQUO = "“"; // left double curly quote
const RDQUO = "”"; // right double curly quote
const ENDASH = "–";

// ---------------------------------------------------------------------
// (a) Every markdown fixture LITERAL that appears in annotate.test.mjs,
//     extracted by hand. Projection-only vectors validate plain+map; the
//     ones carrying `searches` also exercise the locator. Deduped where the
//     test file repeats an identical literal.
// ---------------------------------------------------------------------
const testFileVectors = [
    makeVector("bold-asterisks", "**bold**"),
    makeVector("bold-underscores", "__bold__"),
    makeVector("italic-asterisk", "*italic*"),
    makeVector("italic-underscore", "_italic_"),
    makeVector("bold-italic-asterisks", "***both***"),
    makeVector("bold-italic-underscores", "___both___"),
    makeVector("inline-code", "`code`"),
    makeVector("inline-code-literal-content", "`*not italic*`"),
    makeVector("link", "[a link](https://example.com)", [
        { quote: "a link", assert: "found" },
    ]),
    makeVector("image-alt", "![an alt](https://example.com/x.png)"),
    makeVector("image-empty-alt", "![](https://example.com/x.png)"),
    makeVector("heading-h1", "# Heading One"),
    makeVector("heading-h2", "## Heading Two"),
    makeVector("heading-h3", "### Heading Three"),
    makeVector("blockquote", "> a quote"),
    makeVector("blockquote-nested", ">> nested quote"),
    makeVector(
        "fenced-code-block",
        "before\n```\nconst x = 1;\n```\nafter",
        [{ quote: "const x = 1;", assert: "found" }]
    ),
    makeVector("fenced-code-not-inline-processed", "```\n**not bold**\n```"),
    makeVector("tilde-fenced-code", "~~~\ncode here\n~~~"),
    makeVector("nested-bold-in-italic", "*italic **bold** italic*"),
    makeVector("nested-italic-in-bold", "**bold *italic* bold**"),
    makeVector("emoji-in-bold", `**hello ${EMOJI} world**`),
    makeVector("nbsp-preserved", `a${NBSP}b`),
    makeVector("cjk-in-bold", "**你好世界**"),
    makeVector(
        "plain-paragraph",
        "Just a plain paragraph with no markdown syntax at all."
    ),
    makeVector(
        "multi-paragraph",
        "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    ),
    makeVector("empty-document", ""),
    makeVector(
        "mixed-syntax-map-invariant",
        "# Title\n\nA **bold** and *italic* [link](url) and `code` span.\n\n> quoted\n\n```\nfenced\n```\n"
    ),
    makeVector("snake-case-not-emphasis", "foo_bar_baz"),
    makeVector("spaced-asterisks-not-emphasis", "solve a * b then c * d"),
    makeVector("simple-italic-strips", "*emphasis*"),
    makeVector("word-boundary-underscore-italic", "word _em_ word"),
    makeVector("mixed-snake-and-emphasis", "snake_case and *real em*"),
    makeVector("leading-underscore-opens", "_em_ word"),
    makeVector("trailing-underscore-literal", "word_ *and* word_"),
    makeVector(
        "snake-case-offset-map",
        "foo_bar_baz rest of sentence",
        [{ quote: "rest of sentence", assert: "found" }]
    ),
    makeVector(
        "crlf-line-endings",
        "first line\r\nsecond line\r\nthird line",
        [{ quote: "second line", assert: "found" }]
    ),
    makeVector("surrogate-pair-emoji-shift", `hi ${EMOJI} bye`, [
        { quote: "bye", assert: "found" },
    ]),
    makeVector(
        "mixed-map-strictly-increasing",
        "# Title\n\nA **bold** and *italic* and _em_ and snake_case_word and\n" +
            `[link](url) and \`code span\` and a spaced * star * and ${EMOJI} emoji.\n\n` +
            "> quoted\n\n```\nfenced\ncontent\n```\n\nlast line"
    ),
    makeVector("code-span-exact-close-run", "`a`` b`"),
    makeVector(
        "double-backtick-with-single",
        "``code with ` a single backtick``"
    ),
    makeVector(
        "quick-brown-fox",
        "The quick brown fox jumps over the lazy dog.",
        [{ quote: "brown fox", assert: "found" }]
    ),
    makeVector("bold-content-span", "before **bold content** after", [
        { quote: "bold content", assert: "found" },
    ]),
    makeVector("bold-then-text", "**bold** text"),
    makeVector("hello-world", "hello world"),
    makeVector(
        "plain-unformatted-prose",
        "Plain unformatted prose with no special characters here.",
        [{ quote: "unformatted prose", assert: "found" }]
    ),
    makeVector("fence-only", "```\ncode\n```"),

    // findQuoteInPlain / fallback haystacks from the test file. These have no
    // markdown syntax, so projectMarkdown is an identity projection and the
    // search runs over plain === markdown. Search needles mirror the test
    // cases (run WITHOUT approxPos per the frozen schema).
    makeVector(
        "context-full-disambiguation",
        "cat sat on the mat. cat ran to the door.",
        [{ quote: "cat", prefix: ". ", suffix: " ran", assert: "found" }]
    ),
    makeVector("context-prefix-only", "xxcatxx aacatbb", [
        { quote: "cat", prefix: "aa", assert: "found" },
    ]),
    makeVector(
        "regex-metachars-literal",
        "cost is $5.00 (approx.) per unit [see note]",
        [{ quote: "$5.00 (approx.)", assert: "found" }]
    ),
    // `__proto__` wrapped in inline code so the underscores survive projection
    // literally (as bare markdown, `__proto__` projects to "proto" — the `__`
    // is stripped as bold). Preserves the original test's prototype-pollution
    // safety intent: the search needle is the object-key name "__proto__".
    makeVector("proto-as-quote", "before `__proto__` after", [
        { quote: "__proto__", prefix: "before ", suffix: " after", assert: "found" },
    ]),
    makeVector("overlapping-repeats", "aaaaaa", [
        { quote: "aaa", assert: "found" },
    ]),
    makeVector("unique-needle", "a unique needle in this text", [
        { quote: "needle", assert: "found" },
    ]),
    makeVector(
        "fallback-whitespace-run",
        "the quick  brown fox jumps",
        [{ quote: "quick brown", prefix: "the ", suffix: " fox", assert: "found" }]
    ),
    makeVector(
        "fallback-multi-whitespace",
        "line one\n\n\nline two   has   extra   spaces",
        [{ quote: "has extra spaces", assert: "found" }]
    ),

    // Boundary-condition searches from annotate.test.mjs flagged by pre-freeze
    // review (task-2-report.md addendum). Mirror three findQuoteInPlain cases
    // not yet represented above: equal-context duplicates with NO approxPos
    // (the frozen schema never passes approxPos at all, so this is exactly the
    // call shape every search here already uses — first-candidate-wins pins
    // the FIRST occurrence), a quote at position 0, and a quote at the very
    // end of the document.
    makeVector(
        "equal-context-duplicates-no-approx-first-wins",
        "cat here. cat here.",
        [{ quote: "cat", assert: "found" }]
    ),
    makeVector(
        "quote-at-start-of-document",
        "start of the document",
        [{ quote: "start", suffix: " of", assert: "found" }]
    ),
    makeVector(
        "quote-at-end-of-document",
        "the document ends here",
        [{ quote: "here", prefix: "ends ", assert: "found" }]
    ),
];

// ---------------------------------------------------------------------
// (b) Six additional adversarial documents (brief Step 1b). Each targets a
//     structural corner where a naive Swift port would diverge.
// ---------------------------------------------------------------------
const adversarialVectors = [
    // 1. heading + bold + link dense paragraph
    makeVector(
        "adv-heading-bold-link-dense",
        "## The **Rise** of [Local-First](https://tiro.app) Software\n\n" +
            "We think **local-first** apps with _real_ ownership and " +
            "`no cloud` lock-in are the [future](https://example.com/future).",
        [
            { quote: "Rise", assert: "found" },
            { quote: "Local-First", assert: "found" },
            { quote: "local-first", prefix: "think ", suffix: " apps", assert: "found" },
            { quote: "future", assert: "found" },
        ]
    ),

    // 2. code fence + inline code (fence content is literal, inline backticks stripped)
    makeVector(
        "adv-code-fence-and-inline",
        "Use `git commit` to save your work.\n\n" +
            "```\ndef f(x):\n    return x * 2\n```\n\n" +
            "Then run `git push` to publish.",
        [
            { quote: "git commit", assert: "found" },
            { quote: "return x * 2", assert: "found" },
            { quote: "git push", assert: "found" },
        ]
    ),

    // 3. blockquote + list nesting (only `>` prefixes stripped; list markers kept)
    makeVector(
        "adv-blockquote-list-nesting",
        "> Key points:\n>\n> - first **item**\n> - second _item_\n" +
            ">   - nested detail\n\n1. ordered one\n2. ordered two",
        [
            { quote: "first item", assert: "found" },
            { quote: "second item", assert: "found" },
            { quote: "- nested detail", assert: "found" },
            { quote: "1. ordered one", assert: "found" },
        ]
    ),

    // 4. NFC-composable accents — POSITIVE length-preserving singleton bridge
    //    (U+212B -> U+00C5) resolves; NO decomposed char elsewhere in the doc,
    //    since a length-changing sequence would make safeNFC bail on the whole
    //    string (see the `adv-not-bridged` cafe-null case for that boundary).
    makeVector(
        "adv-nfc-angstrom-bridge",
        `The bond measured 1.5 ${ANGSTROM}ngstrom across the gap.`,
        [
            { quote: `${A_RING}ngstrom`, prefix: "1.5 ", suffix: " across", assert: "found" },
        ]
    ),

    // 5. collapsed-whitespace runs — multi-space within a line survives
    //    projection; single-spaced needles resolve only via the fallback.
    makeVector(
        "adv-collapsed-whitespace",
        "Roses   are red,\n\n\nviolets  are   blue.",
        [
            { quote: "Roses are red", suffix: ",", assert: "found" },
            { quote: "are blue", assert: "found" },
        ]
    ),

    // 6. entity / smart-punctuation — MUST SOFT-FAIL (not bridged). A literal
    //    control proves the search itself works; only the substituted needles
    //    return null.
    makeVector(
        "adv-entity-and-smartpunct-null",
        `Prices rose 5--10% and "quotes" matter; see AT&amp;T today.`,
        [
            { quote: "AT&amp;T", assert: "found" }, // literal control — resolves
            { quote: `5${ENDASH}10%`, assert: "null" }, // en-dash substitution — soft-fail
            { quote: `${LDQUO}quotes${RDQUO}`, assert: "null" }, // curly quotes — soft-fail
            { quote: "AT&T", assert: "null" }, // decoded entity — soft-fail
        ]
    ),

    // 7. (bonus) decomposed é — length-CHANGING NFC is explicitly NOT bridged,
    //    so a precomposed needle soft-fails. This pins the exact boundary that
    //    makes #4's angstrom bridge safe but a decomposed accent unsafe.
    makeVector(
        "adv-nfc-decomposed-not-bridged",
        `Bonjour, we met at the caf${E_ACUTE_DECOMP} downtown.`,
        [
            { quote: `caf${E_ACUTE_PRECOMP}`, prefix: "the ", suffix: " downtown", assert: "null" },
        ]
    ),
];

/**
 * The full ordered vector array. Pure: no I/O, no randomness — calling it
 * twice yields deep-equal output, so the drift guard can import it and compare
 * against the committed file without touching disk.
 * @returns {Array<object>}
 */
export function buildVectors() {
    return [...testFileVectors, ...adversarialVectors];
}

/** Canonical serialization used for the committed file (2-space indent,
 * trailing newline). The single source of the on-disk byte layout. */
export function serialize(vectors) {
    return JSON.stringify(vectors, null, 2) + "\n";
}

export { OUTPUT_PATH };

// Write only when executed directly (`node scripts/export_anchor_fixtures.mjs`),
// never on import — the drift-guard test imports buildVectors/serialize.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
    const vectors = buildVectors();
    writeFileSync(OUTPUT_PATH, serialize(vectors));
    console.log(
        `Wrote ${vectors.length} anchor-parity vectors ` +
            `(${vectors.reduce((n, v) => n + v.searches.length, 0)} searches) to ${OUTPUT_PATH}`
    );
}
