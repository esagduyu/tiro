// node:test coverage for js/wiki.js's PURE functions: resolveWikilinks and
// escapeMarkdownLinkText.
//
// These two are the highest-risk area in the whole M2.0 migration (per
// CLAUDE.md's XSS invariant note): wiki page bodies are LLM-generated and
// may embed `[[stem|label]]` citation tokens whose `label` text is
// untrusted. resolveWikilinks splices that label into markdown source
// (`[label](/articles/{id})`) BEFORE renderMarkdown/DOMPurify ever see it,
// so a label containing unescaped `]`, `)`, or `\` could hijack the link
// target to an attacker-chosen URL even though the final HTML is still
// DOMPurify-sanitized. Neither function touches DOM/DOMPurify/marked, so
// both are testable in isolation exactly as written in js/wiki.js — no
// refactor was needed to make them pure-testable (per the task brief's
// "if it touches DOM, refactor minimally" instruction: it doesn't).
//
// Adversarial cases below are the exact table named in the P1b review the
// task brief points at: a label containing `)`, a label containing `(`, a
// `__proto__` stem probing prototype-pollution-style lookups on a plain
// object citations map, and a plain unresolvable stem.

import { test } from "node:test";
import assert from "node:assert/strict";
import { resolveWikilinks, escapeMarkdownLinkText } from "../wiki.js";

test("escapeMarkdownLinkText: escapes backslash, brackets, and close-paren", () => {
    assert.equal(escapeMarkdownLinkText("plain text"), "plain text");
    assert.equal(escapeMarkdownLinkText("a\\b"), "a\\\\b");
    assert.equal(escapeMarkdownLinkText("[brackets]"), "\\[brackets\\]");
    assert.equal(escapeMarkdownLinkText("close)paren"), "close\\)paren");
    // Open paren is NOT escaped — only `)` can prematurely close a `(...)`
    // link-target span; a bare `(` is inert in that position.
    assert.equal(escapeMarkdownLinkText("open(paren"), "open(paren");
    assert.equal(
        escapeMarkdownLinkText("click](http://evil.com)"),
        "click\\](http://evil.com\\)",
    );
});

test("resolveWikilinks: adversarial table — label with ')', label with '(', __proto__ stem, unresolvable stem", () => {
    const citations = { "some-article": 42 };

    // 1. Label containing ')' must not be able to splice a premature
    //    `](url)` sequence into the emitted markdown link.
    assert.equal(
        resolveWikilinks("[[some-article|click)here]]", citations),
        "[click\\)here](/articles/42)",
    );

    // 2. Label containing '(' — inert on its own, but exercised alongside
    //    ')' to confirm the pairing doesn't get "helpfully" balanced/escaped
    //    differently when both appear together.
    assert.equal(
        resolveWikilinks("[[some-article|open(and)close]]", citations),
        "[open(and\\)close](/articles/42)",
    );

    // 3. `__proto__` as the stem: citations is a plain object built from
    //    JSON, so a malicious/unlucky stem of "__proto__" must NOT resolve
    //    via prototype lookup (Object.prototype.__proto__ is not a number,
    //    but the check must actively guard via hasOwnProperty, not just
    //    happen to fail typeof — this locks that guard in as a regression
    //    test). Must render as plain escaped label text, no link.
    assert.equal(
        resolveWikilinks("[[__proto__|Prototype]]", citations),
        "Prototype",
    );
    assert.equal(
        resolveWikilinks("[[__proto__]]", citations),
        "__proto__",
    );

    // 4. Plain unresolvable stem (not in citations map at all): plain text,
    //    never a dead/error link.
    assert.equal(
        resolveWikilinks("[[nonexistent-stem|Some Label]]", citations),
        "Some Label",
    );
    assert.equal(
        resolveWikilinks("[[nonexistent-stem]]", citations),
        "nonexistent-stem",
    );
});

test("resolveWikilinks: resolvable citation renders as a real markdown link", () => {
    const citations = { "my-article": 7 };
    assert.equal(
        resolveWikilinks("See [[my-article|this piece]] for details.", citations),
        "See [this piece](/articles/7) for details.",
    );
    // No label given: falls back to the stem itself as the label.
    assert.equal(
        resolveWikilinks("See [[my-article]] for details.", citations),
        "See [my-article](/articles/7) for details.",
    );
});

test("resolveWikilinks: whitespace-only label falls back to the stem", () => {
    const citations = { "my-article": 7 };
    assert.equal(
        resolveWikilinks("[[my-article|   ]]", citations),
        "[my-article](/articles/7)",
    );
});

test("resolveWikilinks: non-numeric citation value (e.g. string id) is treated as unresolved", () => {
    // Defensive: the API always returns numeric article ids, but the check
    // is `typeof articleId !== "number"`, not just truthiness — a stray
    // string/object value must not be treated as resolved.
    const citations = { "weird-article": "7" };
    assert.equal(
        resolveWikilinks("[[weird-article|Weird]]", citations),
        "Weird",
    );
});

test("resolveWikilinks: missing/undefined citations map treated as empty", () => {
    assert.equal(resolveWikilinks("[[stem|Label]]", undefined), "Label");
    assert.equal(resolveWikilinks("[[stem|Label]]", null), "Label");
});

test("resolveWikilinks: multiple citations in one body, mixed resolved/unresolved", () => {
    const citations = { a: 1, b: 2 };
    assert.equal(
        resolveWikilinks("[[a|First]] and [[b|Second]] and [[c|Third]].", citations),
        "[First](/articles/1) and [Second](/articles/2) and Third.",
    );
});

test("resolveWikilinks: leaves non-wikilink text untouched", () => {
    const body = "Regular markdown *text* with no citations at all.";
    assert.equal(resolveWikilinks(body, {}), body);
});
