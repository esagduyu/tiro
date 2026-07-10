/* Tiro — reading-progress pure core (owner UX wave 1).
 *
 * Pure scroll→progress math, NO DOM. Extracted (like swipe.js/undo.js) so the
 * fraction computation is node-testable in isolation; reader.js owns the
 * impure rAF-throttled scroll listener + the `#reading-progress` width write.
 *
 * The progress bar tracks scroll through the ARTICLE BODY specifically (not
 * the whole page): it reaches 1.0 (100%) exactly when the reader-body's bottom
 * edge scrolls up past the bottom of the viewport, so the bar completes when
 * the last line of the article clears the fold rather than when the (longer)
 * page — footer, related-articles, phone action bar — is fully scrolled.
 *
 * This is DELIBERATELY independent of reader.js's telemetry scroll-depth math
 * (`updateTelemetryScrollDepth`), which measures against the full document
 * `scrollHeight`, not the body element's bounds — a different denominator for
 * a different purpose (an importance-ranking signal vs. a visual fill). Sharing
 * would couple two unrelated numbers; keeping them separate is the clean call.
 */

/**
 * Fraction (0..1, clamped) of the article body scrolled past the bottom of
 * the viewport.
 *
 * @param {number} scrollY   window.scrollY (document scroll offset, px)
 * @param {number} viewportH window.innerHeight (px)
 * @param {number} bodyTop   absolute document offset of the body's top edge
 *                           (getBoundingClientRect().top + scrollY), px
 * @param {number} bodyHeight the body element's rendered height, px
 * @returns {number} 0..1
 */
export function computeReadingProgress(scrollY, viewportH, bodyTop, bodyHeight) {
    // A zero/negative/non-finite body height has no meaningful progress to
    // report (article not laid out yet, or measured empty) — report 0 rather
    // than dividing by zero.
    if (!Number.isFinite(bodyHeight) || bodyHeight <= 0) return 0;
    // Viewport-bottom position in document coordinates, relative to the body's
    // top. When it reaches bodyHeight, the body's bottom edge sits at the
    // bottom of the viewport → fully read.
    const viewportBottomPastBodyTop = scrollY + viewportH - bodyTop;
    const frac = viewportBottomPastBodyTop / bodyHeight;
    if (frac <= 0) return 0;
    if (frac >= 1) return 1;
    return frac;
}
