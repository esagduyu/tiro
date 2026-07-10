// node:test coverage for js/reading-progress.js — PURE FUNCTION ONLY.
//
// computeReadingProgress(scrollY, viewportH, bodyTop, bodyHeight) → 0..1.
// reader.js's rAF scroll listener + width write are impure and covered by
// Playwright/visual verification, not here.

import { test } from "node:test";
import assert from "node:assert/strict";
import { computeReadingProgress } from "../reading-progress.js";

test("computeReadingProgress: at the top of a tall article, only the first screenful reads", () => {
    // body starts 200px down the document, 4000px tall, 800px viewport, not scrolled.
    // viewportBottomPastBodyTop = 0 + 800 - 200 = 600; frac = 600/4000 = 0.15
    assert.equal(computeReadingProgress(0, 800, 200, 4000), 0.15);
});

test("computeReadingProgress: reaches exactly 1.0 when the body bottom hits the viewport bottom", () => {
    // body bottom at 200+4000 = 4200. Viewport bottom = scrollY+800. Need scrollY = 3400.
    assert.equal(computeReadingProgress(3400, 800, 200, 4000), 1);
});

test("computeReadingProgress: clamps to 1 when scrolled past the body", () => {
    assert.equal(computeReadingProgress(9999, 800, 200, 4000), 1);
});

test("computeReadingProgress: clamps to 0 before the body enters the viewport bottom", () => {
    // Body far below the fold: viewport bottom hasn't reached bodyTop yet.
    // scrollY 0, viewport 800, bodyTop 2000 → 0+800-2000 = -1200 → clamp 0.
    assert.equal(computeReadingProgress(0, 800, 2000, 4000), 0);
});

test("computeReadingProgress: a short article that fits on screen reads as fully complete", () => {
    // bodyHeight 300 < viewport 800: viewportBottomPastBodyTop = 0+800-100 = 700 > 300 → clamp 1.
    assert.equal(computeReadingProgress(0, 800, 100, 300), 1);
});

test("computeReadingProgress: guards zero / non-finite body height", () => {
    assert.equal(computeReadingProgress(500, 800, 200, 0), 0);
    assert.equal(computeReadingProgress(500, 800, 200, -10), 0);
    assert.equal(computeReadingProgress(500, 800, 200, NaN), 0);
});

test("computeReadingProgress: monotonic and bounded across a scroll sweep", () => {
    let prev = -1;
    for (let y = 0; y <= 5000; y += 250) {
        const p = computeReadingProgress(y, 800, 200, 4000);
        assert.ok(p >= 0 && p <= 1, `progress ${p} out of range at y=${y}`);
        assert.ok(p >= prev, `progress decreased at y=${y}`);
        prev = p;
    }
});
