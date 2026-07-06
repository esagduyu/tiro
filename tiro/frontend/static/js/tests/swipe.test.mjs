// node:test coverage for js/swipe.js — PURE STATE MACHINE, NO DOM (M3.2
// Task 2). This is trust-critical logic (a wrong direction-lock hijacks
// vertical scrolling on every phone), so every assertion here checks EXACT
// transform/action values rather than just truthiness. Nothing imports
// swipe.js yet — T3 wires it into inbox card pointer handlers.

import { test } from "node:test";
import assert from "node:assert/strict";
import {
    createSwipeState,
    swipeEvent,
    clamp,
    computeVelocity,
    SLOP_PX,
    ACT_THRESHOLD_RATIO,
    FLICK_VELOCITY_PX_MS,
} from "../swipe.js";

const CARD_WIDTH = 1000; // large so ACT_THRESHOLD_RATIO * CARD_WIDTH = 350,
// well clear of the flick-shortcut's small displacements below.

function down(state, x, y, t) {
    return swipeEvent(state, { type: "down", x, y, t, cardWidth: CARD_WIDTH });
}
function move(state, x, y, t) {
    return swipeEvent(state, { type: "move", x, y, t, cardWidth: CARD_WIDTH });
}
function up(state, x, y, t) {
    return swipeEvent(state, { type: "up", x, y, t, cardWidth: CARD_WIDTH });
}
function cancel(state, x, y, t) {
    return swipeEvent(state, { type: "cancel", x, y, t, cardWidth: CARD_WIDTH });
}

test("createSwipeState: idle, no fields implying an in-progress gesture", () => {
    assert.deepEqual(createSwipeState(), { phase: "idle" });
});

test("swipeEvent: 'down' initializes phase 'pending' with null transform/action", () => {
    const r = down(createSwipeState(), 0, 0, 0);
    assert.equal(r.state.phase, "pending");
    assert.equal(r.transform, null);
    assert.equal(r.action, null);
});

test("direction-lock matrix: pure-horizontal / pure-vertical / diagonal-favoring-x / diagonal-favoring-y / sub-slop jitter / exact-slop boundary", () => {
    const cases = [
        // [label, dx, dy, expectedPhaseAfterMove, expectedTransform]
        ["pure horizontal", 20, 0, "dragging", { dx: 20 }],
        ["pure vertical", 0, 20, "scrolling", null],
        ["diagonal favoring x (|dx|>|dy|, both over slop)", 15, 10, "dragging", { dx: 15 }],
        ["diagonal favoring y (|dy|>=|dx|, both over slop)", 10, 15, "scrolling", null],
        ["exact tie at |dx|==|dy| resolves to scrolling (never hijack scroll)", 13, 13, "scrolling", null],
        ["sub-slop jitter in every direction: stays pending, no transform", 5, 5, "pending", null],
        [`exactly SLOP_PX (${SLOP_PX}) is NOT enough to lock (strict >)`, SLOP_PX, 0, "pending", null],
        [`SLOP_PX + 1 does lock horizontal`, SLOP_PX + 1, 0, "dragging", { dx: SLOP_PX + 1 }],
    ];

    for (const [label, dx, dy, expectedPhase, expectedTransform] of cases) {
        const s0 = down(createSwipeState(), 0, 0, 0).state;
        const r = move(s0, dx, dy, 16);
        assert.equal(r.state.phase, expectedPhase, `${label}: phase`);
        assert.deepEqual(r.transform, expectedTransform, `${label}: transform`);
        assert.equal(r.action, null, `${label}: action must be null on a non-terminal move`);
    }
});

test("scroll lock is permanent: once scrolling, a later strongly-horizontal move still reports no transform and stays scrolling", () => {
    let s = down(createSwipeState(), 0, 0, 0).state;
    let r = move(s, 0, 20, 16); // locks vertical
    assert.equal(r.state.phase, "scrolling");
    s = r.state;

    r = move(s, 200, 20, 32); // now overwhelmingly horizontal — must NOT re-engage
    assert.equal(r.state.phase, "scrolling");
    assert.equal(r.transform, null);
    assert.equal(r.action, null);

    r = up(r.state, 200, 20, 48);
    assert.equal(r.action, "cancelled");
    assert.equal(r.transform, null);
});

test("release threshold boundaries: 34.9% of cardWidth snaps back, exactly 35% acts, both directions", () => {
    // ACT_THRESHOLD_RATIO * CARD_WIDTH == 350. Release happens long after
    // the lock-engaging move (t=5000 vs t=10) so the flick window has fully
    // decayed and only the plain distance threshold is in play.
    assert.equal(ACT_THRESHOLD_RATIO, 0.35);

    let s = down(createSwipeState(), 0, 0, 0).state;
    s = move(s, 20, 0, 10).state; // engages horizontal lock

    assert.equal(up(s, 349, 0, 5000).action, "cancelled", "349 (34.9%) snaps back");
    assert.equal(up(s, 350, 0, 5000).action, "archive", "350 (exactly 35%) acts");
    assert.equal(up(s, -349, 0, 5000).action, "cancelled", "-349 (34.9%) snaps back");
    assert.equal(up(s, -350, 0, 5000).action, "snooze-sheet", "-350 (exactly 35%) acts");
});

test("flick shortcut: a fast short swipe (well under the 35% distance threshold) still acts because it releases while the velocity window is hot", () => {
    let s = down(createSwipeState(), 0, 0, 0).state;
    s = move(s, 20, 0, 10).state; // engages lock, velocity so far: 20px/10ms = 2px/ms

    const r = up(s, 40, 0, 20); // only 10ms after the move, dx=40 (4% of cardWidth)
    assert.equal(r.action, "archive");
});

test("flick shortcut: the SAME fast movement followed by a stall before release does NOT act — the velocity window matters, not just peak speed", () => {
    let s = down(createSwipeState(), 0, 0, 0).state;
    s = move(s, 20, 0, 10).state; // identical fast move as the previous test

    // Released at the SAME position (dx=40) but 4990ms later, with no
    // intervening move — the trailing 100ms window before "up" contains no
    // samples, so computed velocity is 0 and the flick does not fire.
    const r = up(s, 40, 0, 5000);
    assert.equal(r.action, "cancelled");
});

test("flick direction must be consistent with the release direction (a flick fires with its own sign, not the opposite one)", () => {
    let s = down(createSwipeState(), 0, 0, 0).state;
    s = move(s, -20, 0, 10).state; // fast movement to the LEFT

    const r = up(s, -40, 0, 20);
    assert.equal(r.action, "snooze-sheet");
});

test("cancel: pointercancel always resolves 'cancelled' with a null transform, from pending, dragging, or scrolling", () => {
    // From "pending" (down received, no lock yet).
    let s = down(createSwipeState(), 0, 0, 0).state;
    let r = cancel(s, 3, 3, 5);
    assert.equal(r.action, "cancelled");
    assert.equal(r.transform, null);
    assert.deepEqual(r.state, createSwipeState());

    // From "dragging" (horizontal lock engaged).
    s = move(down(createSwipeState(), 0, 0, 0).state, 20, 0, 10).state;
    r = cancel(s, 150, 0, 20);
    assert.equal(r.action, "cancelled");
    assert.equal(r.transform, null);
    assert.deepEqual(r.state, createSwipeState());

    // From "scrolling" (vertical lock engaged).
    s = move(down(createSwipeState(), 0, 0, 0).state, 0, 20, 10).state;
    r = cancel(s, 0, 150, 20);
    assert.equal(r.action, "cancelled");
    assert.equal(r.transform, null);
    assert.deepEqual(r.state, createSwipeState());
});

test("'up' with no prior 'down' (still idle) resolves cancelled rather than crashing", () => {
    const r = up(createSwipeState(), 100, 0, 0);
    assert.equal(r.action, "cancelled");
    assert.equal(r.transform, null);
});

test("'move' with no prior 'down' (still idle) is a no-op", () => {
    const s0 = createSwipeState();
    const r = move(s0, 100, 0, 0);
    assert.equal(r.transform, null);
    assert.equal(r.action, null);
    assert.deepEqual(r.state, s0);
});

test("transform clamps to +/-cardWidth: overshoot beyond the card never reports a larger dx", () => {
    let s = down(createSwipeState(), 0, 0, 0).state;
    s = move(s, 20, 0, 10).state; // engage lock

    let r = move(s, 5000, 0, 20);
    assert.deepEqual(r.transform, { dx: CARD_WIDTH });

    r = move(s, -5000, 0, 20);
    assert.deepEqual(r.transform, { dx: -CARD_WIDTH });
});

test("state fully resets to idle after any terminal action (archive/snooze-sheet/cancelled)", () => {
    let s = down(createSwipeState(), 0, 0, 0).state;
    s = move(s, 20, 0, 10).state;
    const archived = up(s, 400, 0, 5000);
    assert.deepEqual(archived.state, createSwipeState());

    s = down(createSwipeState(), 0, 0, 0).state;
    s = move(s, -20, 0, 10).state;
    const snoozed = up(s, -400, 0, 5000);
    assert.deepEqual(snoozed.state, createSwipeState());

    s = down(createSwipeState(), 0, 0, 0).state;
    s = move(s, 20, 0, 10).state;
    const cancelled = up(s, 10, 0, 5000);
    assert.deepEqual(cancelled.state, createSwipeState());

    // The reset state is safe to feed a brand-new gesture into.
    const fresh = down(archived.state, 7, 7, 9999);
    assert.equal(fresh.state.phase, "pending");
    assert.equal(fresh.state.downX, 7);
});

test("clamp(): passes values inside the range through unchanged, clamps outside", () => {
    assert.equal(clamp(50, 100), 50);
    assert.equal(clamp(100, 100), 100);
    assert.equal(clamp(101, 100), 100);
    assert.equal(clamp(-100, 100), -100);
    assert.equal(clamp(-101, 100), -100);
    assert.equal(clamp(0, 100), 0);
});

test("computeVelocity(): windowing behavior in isolation", () => {
    // Two samples 20ms apart covering 40px -> 2px/ms, both within the
    // trailing 100ms window measured from the release point.
    assert.equal(computeVelocity([{ x: 0, t: 0 }, { x: 20, t: 10 }], 40, 20), 2);

    // No samples at all -> 0 (nothing to compute from).
    assert.equal(computeVelocity([], 40, 20), 0);

    // All samples fall outside the trailing 100ms window -> 0, even though
    // the raw numbers would otherwise suggest a fast average.
    assert.equal(computeVelocity([{ x: 0, t: 0 }], 1000, 10000), 0);

    // A single in-window sample equal to the release point itself (dt<=0
    // guard) -> 0, not a divide-by-zero/Infinity.
    assert.equal(computeVelocity([{ x: 40, t: 20 }], 40, 20), 0);

    assert.ok(FLICK_VELOCITY_PX_MS > 0, "sanity: the exported threshold is a positive px/ms rate");
});
