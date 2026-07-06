/* Tiro — swipe-triage gesture state machine (M3.2 Task 2).
 *
 * Pure state machine, NO DOM. Nothing imports this module yet — T3 wires it
 * into inbox card pointer handlers. This is trust-critical logic: a wrong
 * direction-lock decision hijacks vertical page scrolling on every phone, so
 * the machine is deliberately conservative about engaging horizontally and
 * ties always resolve in favor of NOT hijacking scroll.
 *
 * ## States
 *
 * `createSwipeState()` returns an idle state (`phase: "idle"`). A caller
 * feeds it a stream of events via `swipeEvent(state, ev)`, where
 * `ev = {type: "down"|"move"|"up"|"cancel", x, y, t, cardWidth}` (`t` is any
 * monotonic millisecond clock, e.g. `event.timeStamp` or `Date.now()`).
 * `swipeEvent` never mutates its `state` argument — it always returns a
 * fresh `{state, transform, action}` object.
 *
 * Phases (internal to `state`, not part of the public contract beyond what
 * `transform`/`action` expose):
 *   - `"idle"` — no gesture in progress.
 *   - `"pending"` — a pointer is down but neither lock has engaged yet
 *     (movement so far is within the 12px slop, or ambiguous).
 *   - `"dragging"` — horizontal lock engaged; `transform` tracks the card's
 *     translateX.
 *   - `"scrolling"` — vertical lock engaged; the machine reports no
 *     transform for the rest of the gesture so the browser's native scroll
 *     is never fought. This is a PERMANENT lock for the gesture's lifetime
 *     — once "scrolling", a later horizontal-favoring move cannot re-engage
 *     "dragging" (see the module's binding spec: "Once locked horizontal,
 *     report transforms" — the converse, re-locking away from an existing
 *     lock, never happens for EITHER lock).
 *
 * ## Direction lock
 *
 * Measured from the "down" point on every "move" while `phase === "pending"`:
 *   - `|dx| > SLOP_PX` (12) AND `|dx| > |dy|` -> lock horizontal
 *     ("dragging").
 *   - `|dy| > SLOP_PX` (12) AND `|dy| >= |dx|` -> lock vertical
 *     ("scrolling"). Note the `>=`: an exact dx/dy tie resolves to
 *     scrolling, not dragging — ties favor the browser's native scroll,
 *     never the swipe gesture.
 *   - Neither condition holds (sub-slop jitter, or the ambiguous zone where
 *     `dx === dy` sits at/under slop) -> stay "pending", no transform.
 *
 * Once either lock engages, it holds until a terminal event ("up" or
 * "cancel") returns the state to "idle".
 *
 * ## Transform
 *
 * `null` whenever the gesture is not in "dragging" phase. While dragging,
 * `{dx}` where `dx` is the horizontal displacement from the down point,
 * clamped to `±cardWidth` (a hard clamp — no overshoot damping, per the
 * binding spec's "keep simple" instruction).
 *
 * ## Release (terminal events)
 *
 * On "up" while "dragging": compares the UNCLAMPED dx against
 * `ACT_THRESHOLD_RATIO * cardWidth` (0.35):
 *   - `dx >= threshold` -> `action: "archive"`.
 *   - `dx <= -threshold` -> `action: "snooze-sheet"`.
 *   - otherwise, check the flick shortcut (see below); if it fires, act in
 *     the flick's direction; otherwise `action: "cancelled"` (caller snaps
 *     the card back).
 *
 * On "up" while "pending" or "scrolling", or on "cancel" in ANY phase ->
 * `action: "cancelled"`, `transform: null`. (A "cancel" is unconditional per
 * the binding spec: "pointercancel -> cancelled always".)
 *
 * After any terminal action the returned `state` is idle again, ready for
 * the next gesture.
 *
 * ## Flick shortcut
 *
 * Rule implemented (must be documented per the binding spec, since the
 * brief leaves the exact window/velocity free for this task to pick):
 *
 *   - The machine keeps a rolling buffer of `{x, t}` samples recorded on
 *     "down" and every "move" while dragging, pruned on each recording to
 *     only the last `FLICK_WINDOW_MS` (100ms) relative to that event's own
 *     timestamp.
 *   - On "up", the buffer is pruned once more relative to the "up" event's
 *     own timestamp (`ev.t`), so a gesture that moved fast and then went
 *     motionless for a while before releasing ends up with an EMPTY (or
 *     single-sample) window — no stale samples survive across a stall.
 *   - If fewer than 2 samples remain in the window (including "up"'s own
 *     point, which always counts as the window's latest sample), velocity
 *     is 0 -> no flick.
 *   - Otherwise velocity = `(ev.x - earliestSampleInWindow.x) / (ev.t -
 *     earliestSampleInWindow.t)` in px/ms.
 *   - The flick fires when `|velocity| >= FLICK_VELOCITY_PX_MS` (0.5px/ms)
 *     AND its sign is "consistent" with the overall gesture direction: if
 *     the overall `dx` (from the down point, not just the window) is
 *     nonzero, the velocity's sign must match `dx`'s sign; if `dx` is
 *     exactly 0, the velocity's own sign decides the direction outright.
 *   - This means: a fast, short swipe that releases WHILE still moving
 *     acts even under the 35% distance threshold (the window is still hot
 *     at "up" time). A fast swipe followed by a stall before release does
 *     NOT act via the flick path (the window has decayed to ~0 velocity by
 *     "up" time) — it falls through to the plain distance-threshold check,
 *     same as any other slow release.
 *
 * ## Testability
 *
 * `clamp`, `computeVelocity`, and the tunable constants (`SLOP_PX`,
 * `ACT_THRESHOLD_RATIO`, `FLICK_VELOCITY_PX_MS`, `FLICK_WINDOW_MS`) are
 * exported so tests can assert on threshold boundaries directly rather than
 * only through end-to-end event sequences.
 */

/** Distance (px) from the down point before either direction lock can
 * engage. Movement within this radius in any direction stays "pending". */
export const SLOP_PX = 12;

/** Fraction of cardWidth the release dx must reach (in either direction) to
 * act (archive/snooze-sheet) without help from the flick shortcut. */
export const ACT_THRESHOLD_RATIO = 0.35;

/** Minimum px/ms velocity (over the trailing FLICK_WINDOW_MS) for a
 * below-threshold release to still act via the flick shortcut. */
export const FLICK_VELOCITY_PX_MS = 0.5;

/** Trailing window (ms) of recent samples used for the flick velocity
 * calculation. Samples older than this relative to the current event's own
 * timestamp are dropped every time the buffer is touched. */
export const FLICK_WINDOW_MS = 100;

/** Clamp `value` to the closed interval `[-limit, limit]`. `limit` is
 * expected non-negative (a cardWidth); a negative/zero limit clamps
 * everything to `-limit`/`0` respectively, which is harmless degenerate
 * behavior rather than a crash. */
export function clamp(value, limit) {
    if (value > limit) return limit;
    if (value < -limit) return -limit;
    return value;
}

/**
 * Average velocity (px/ms) over `samples` filtered to the trailing
 * `FLICK_WINDOW_MS` window ending at `(latestX, latestT)`. `samples` is an
 * array of `{x, t}` in chronological order (oldest first); `latestX`/
 * `latestT` is the release point itself, which always counts as the
 * window's newest sample even though it is not itself pushed into
 * `samples`.
 *
 * Returns 0 when fewer than 2 points fall in the window (including the
 * release point) or when the window's time span is non-positive (guards
 * a div-by-zero if two samples share a timestamp) — both cases mean "no
 * reliable recent velocity to report", i.e. no flick.
 */
export function computeVelocity(samples, latestX, latestT) {
    const windowStart = latestT - FLICK_WINDOW_MS;
    const inWindow = samples.filter((s) => s.t >= windowStart);
    if (inWindow.length === 0) return 0;

    const earliest = inWindow[0];
    const dt = latestT - earliest.t;
    if (dt <= 0) return 0;

    return (latestX - earliest.x) / dt;
}

/** Prune `samples` (chronological `{x, t}` array) to those within
 * `FLICK_WINDOW_MS` of `nowT`, then append `{x: nowX, t: nowT}`. Used to
 * keep the rolling buffer bounded on every "down"/"move" while dragging. */
function recordSample(samples, nowX, nowT) {
    const windowStart = nowT - FLICK_WINDOW_MS;
    const kept = samples.filter((s) => s.t >= windowStart);
    kept.push({ x: nowX, t: nowT });
    return kept;
}

/** Idle state — no gesture in progress. */
export function createSwipeState() {
    return { phase: "idle" };
}

/** Terminal result: back to idle, no transform, with the given action
 * (`"archive"|"snooze-sheet"|"cancelled"`). */
function terminal(action) {
    return { state: createSwipeState(), transform: null, action };
}

/** No-op result: state unchanged (a new equal object, per the pure/
 * immutable convention), no transform, no action. Used for spurious events
 * that don't apply to the current phase (e.g. a "move"/"up" with no prior
 * "down"). */
function noop(state) {
    return { state, transform: null, action: null };
}

/**
 * Advance the machine by one event. See module docstring for the full
 * contract. Never mutates `state` or `ev`.
 *
 * @param {object} state - previous state, from createSwipeState() or a
 *   prior swipeEvent() call.
 * @param {{type: "down"|"move"|"up"|"cancel", x: number, y: number,
 *   t: number, cardWidth: number}} ev
 * @returns {{state: object, transform: {dx: number}|null,
 *   action: "archive"|"snooze-sheet"|"cancelled"|null}}
 */
export function swipeEvent(state, ev) {
    if (ev.type === "cancel") {
        // Unconditional per the binding spec — even if nothing was engaged
        // (idle/pending), a pointercancel always resolves as "cancelled".
        return terminal("cancelled");
    }

    if (ev.type === "down") {
        // A fresh "down" always (re)starts gesture tracking, regardless of
        // whatever phase preceded it — a real pointer stream never sends a
        // second "down" without an intervening terminal event, but
        // restarting cleanly here is the safe/simple behavior if it did.
        return {
            state: {
                phase: "pending",
                downX: ev.x,
                downY: ev.y,
                downT: ev.t,
                cardWidth: ev.cardWidth,
                samples: recordSample([], ev.x, ev.t),
            },
            transform: null,
            action: null,
        };
    }

    if (ev.type === "move") {
        if (state.phase === "idle") return noop(state);

        if (state.phase === "scrolling") {
            // Permanent lock: never re-evaluate, never hijack scroll.
            return { state, transform: null, action: null };
        }

        const dx = ev.x - state.downX;
        const dy = ev.y - state.downY;

        if (state.phase === "pending") {
            const absDx = Math.abs(dx);
            const absDy = Math.abs(dy);

            if (absDx > SLOP_PX && absDx > absDy) {
                // Horizontal lock engages.
                const nextState = {
                    ...state,
                    phase: "dragging",
                    samples: recordSample(state.samples, ev.x, ev.t),
                };
                return {
                    state: nextState,
                    transform: { dx: clamp(dx, state.cardWidth) },
                    action: null,
                };
            }

            if (absDy > SLOP_PX && absDy >= absDx) {
                // Vertical lock engages — permanent for this gesture.
                return {
                    state: { ...state, phase: "scrolling" },
                    transform: null,
                    action: null,
                };
            }

            // Still within slop / ambiguous: stay pending, track samples
            // so a fast pre-slop flick isn't lost once the lock does
            // engage on a later move.
            return {
                state: { ...state, samples: recordSample(state.samples, ev.x, ev.t) },
                transform: null,
                action: null,
            };
        }

        // state.phase === "dragging": already locked horizontal, keep
        // reporting transforms — no re-locking away from an engaged lock.
        const nextState = {
            ...state,
            samples: recordSample(state.samples, ev.x, ev.t),
        };
        return {
            state: nextState,
            transform: { dx: clamp(dx, state.cardWidth) },
            action: null,
        };
    }

    if (ev.type === "up") {
        if (state.phase !== "dragging") {
            // Released while pending (never engaged either lock) or while
            // scrolling (vertical lock owns this gesture) — always
            // cancelled, no action taken on the card.
            return terminal("cancelled");
        }

        const dx = ev.x - state.downX;
        const threshold = ACT_THRESHOLD_RATIO * state.cardWidth;

        if (dx >= threshold) return terminal("archive");
        if (dx <= -threshold) return terminal("snooze-sheet");

        const velocity = computeVelocity(state.samples, ev.x, ev.t);
        if (Math.abs(velocity) >= FLICK_VELOCITY_PX_MS) {
            const direction = dx !== 0 ? Math.sign(dx) : Math.sign(velocity);
            if (Math.sign(velocity) === direction) {
                return terminal(direction > 0 ? "archive" : "snooze-sheet");
            }
        }

        return terminal("cancelled");
    }

    // Unknown event type: no-op, unchanged state.
    return noop(state);
}
