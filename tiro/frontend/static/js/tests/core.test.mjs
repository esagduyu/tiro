// node:test coverage for js/core.js — PURE FUNCTIONS ONLY.
//
// Per the M2.0 plan's decision of record: "The JS harness tests PURE
// functions only ... no jsdom, no DOM simulation." esc/num/formatDate/
// timeAgo take/return plain values and do no DOM work, so they run directly
// under node. renderMarkdown/apiFetch/showToast/confirmDialog reference
// window/document/fetch and are deliberately NOT imported or tested here —
// they're exercised by Playwright once a later M2.0 task wires core.js into
// a template.
//
// esc()'s byte-identity against the historical DOM-trick implementation was
// verified separately with headless Chromium (see
// .superpowers/sdd/task-1-report.md for the method); this suite locks that
// verified behavior in as a regression test for `node --test`.

import { test } from "node:test";
import assert from "node:assert/strict";
import { esc, num, formatDate, timeAgo } from "../core.js";

test("esc: table of inputs, matches the historical DOM-trick esc() byte-for-byte", () => {
    const cases = [
        ["", ""],
        ["hello world", "hello world"],
        // '&' must be escaped FIRST — an already-escaped "&amp;" double-escapes,
        // exactly like the DOM-trick version (`el.textContent = str` then
        // `el.innerHTML`) did.
        ["&amp;", "&amp;amp;"],
        ["&", "&amp;"],
        ["<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"],
        ['"quoted"', "&quot;quoted&quot;"],
        ["'single'", "&#39;single&#39;"],
        // Backticks are intentionally NOT escaped — neither were they by the
        // DOM-trick version.
        ["`backtick`", "`backtick`"],
        [
            "a & b < c > d \" e ' f ` g",
            "a &amp; b &lt; c &gt; d &quot; e &#39; f ` g",
        ],
        [
            "&&&<<<>>>\"\"\"'''",
            "&amp;&amp;&amp;&lt;&lt;&lt;&gt;&gt;&gt;&quot;&quot;&quot;&#39;&#39;&#39;",
        ],
        // Literal U+00A0 NO-BREAK SPACE is escaped to &nbsp; by the browser's
        // text-node HTML serialization (verified empirically against the DOM
        // version) — regular ASCII spaces are left untouched.
        [" leading and trailing ", "&nbsp;leading and trailing&nbsp;"],
        ["regular   spaces   preserved", "regular   spaces   preserved"],
        ["&lt;already escaped&gt;", "&amp;lt;already escaped&amp;gt;"],
    ];

    for (const [input, expected] of cases) {
        assert.equal(esc(input), expected, `esc(${JSON.stringify(input)})`);
    }
});

test("esc: null/undefined both become empty string (matches Node.textContent's [LegacyNullToEmptyString] behavior)", () => {
    assert.equal(esc(null), "");
    assert.equal(esc(undefined), "");
});

test("esc: non-string primitives are stringified first", () => {
    assert.equal(esc(123), "123");
    assert.equal(esc(0), "0");
    assert.equal(esc(true), "true");
    assert.equal(esc(false), "false");
    assert.equal(esc(NaN), "NaN");
});

test("num: passes through finite numbers and numeric strings", () => {
    assert.equal(num(5), 5);
    assert.equal(num(0), 0);
    assert.equal(num(-3.5), -3.5);
    assert.equal(num("42"), 42);
});

test("num: falls back to '?' for non-finite/non-numeric input", () => {
    assert.equal(num(NaN), "?");
    assert.equal(num(Infinity), "?");
    assert.equal(num(-Infinity), "?");
    assert.equal(num("abc"), "?");
    assert.equal(num(undefined), "?");
    assert.equal(num(null), 0); // Number(null) === 0, matches historical num()
});

test("formatDate: empty string for falsy input", () => {
    assert.equal(formatDate(null), "");
    assert.equal(formatDate(undefined), "");
    assert.equal(formatDate(""), "");
});

test("formatDate: omits year for dates in the current year", () => {
    const now = new Date();
    const iso = new Date(now.getFullYear(), 0, 15).toISOString();
    assert.equal(formatDate(iso), "Jan 15");
});

test("formatDate: includes year for dates in a different year", () => {
    assert.equal(formatDate(new Date(2020, 5, 3).toISOString()), "Jun 3, 2020");
});

test("timeAgo: boundaries", () => {
    const now = Date.now();
    assert.equal(timeAgo(new Date(now - 30 * 1000)), "just now"); // 30s
    assert.equal(timeAgo(new Date(now - 59 * 1000)), "just now"); // just under 1min
    assert.equal(timeAgo(new Date(now - 60 * 1000)), "1m ago"); // exactly 1min
    assert.equal(timeAgo(new Date(now - 5 * 60 * 1000)), "5m ago");
    assert.equal(timeAgo(new Date(now - 59 * 60 * 1000)), "59m ago"); // just under 1hr
    assert.equal(timeAgo(new Date(now - 60 * 60 * 1000)), "1h ago"); // exactly 1hr
    assert.equal(timeAgo(new Date(now - 23 * 60 * 60 * 1000)), "23h ago"); // just under 1day
    assert.equal(timeAgo(new Date(now - 24 * 60 * 60 * 1000)), "yesterday"); // exactly 1day
    assert.equal(timeAgo(new Date(now - 47 * 60 * 60 * 1000)), "yesterday"); // just under 2days
    assert.equal(timeAgo(new Date(now - 48 * 60 * 60 * 1000)), "2 days ago"); // exactly 2days
    assert.equal(timeAgo(new Date(now - 10 * 24 * 60 * 60 * 1000)), "10 days ago");
});
