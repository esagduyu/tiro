/* Tiro — single-slot undo manager pure core (M3.2 Task 2).
 *
 * Pure single-pending-entry manager, NO DOM, no timers. This module only
 * tracks "is there an undoable action pending, and what is it" — the timer
 * that expires the undo window, the toast that shows it, and the keyboard
 * shortcut that triggers it are all T3's DOM binder, layered on top of this
 * core. Nothing imports this module yet (M3.2 scaffolding, zero behavior
 * change).
 *
 * ## Shape
 *
 * `createUndoManager()` returns `{pending: null}`. An entry is
 * `{label, undo}` — `label` is caller-defined (e.g. "Archived", the binder
 * decides what to show), `undo` is a zero-arg callback the binder invokes
 * if the user actually undoes; this core never calls `undo` itself.
 *
 * Only ONE entry can be pending at a time (single-slot, matching the swipe
 * UI's one-card-at-a-time triage flow — archiving/snoozing a second card
 * while the first's undo window is still open finalizes the first rather
 * than stacking).
 *
 * ## API
 *
 *   - `pushUndoable(mgr, entry) -> {mgr, finalized}` — installs `entry` as
 *     the new pending slot. If a DIFFERENT entry was already pending,
 *     `finalized` is that previous entry (the binder is responsible for
 *     running whatever cleanup "finalizing" means for it — e.g. actually
 *     committing a snooze/archive that was only tentative during its own
 *     undo window); this core never invokes `undo` or any other callback
 *     itself, it only tells the caller which entry got displaced.
 *     `finalized` is `null` when the slot was empty.
 *   - `takeUndo(mgr) -> {entry, mgr}` — consumes and returns the pending
 *     entry (`null` if none), leaving the slot empty. This is what the
 *     binder calls when the user actually triggers undo.
 *   - `clearUndo(mgr) -> {mgr, finalized}` — empties the slot without
 *     "taking" it for undo; `finalized` is the previous entry (`null` if
 *     none) so the binder can run the same finalize/cleanup path as a
 *     displaced push (e.g. the undo window's timer expiring naturally).
 *
 * All three functions are pure: `mgr` is never mutated, a fresh object is
 * always returned.
 */

/** Empty manager — no undo pending. */
export function createUndoManager() {
    return { pending: null };
}

/**
 * Install `entry` ({label, undo}) as the new pending undo, displacing
 * whatever was pending before.
 *
 * @param {{pending: object|null}} mgr
 * @param {{label: string, undo: Function}} entry
 * @returns {{mgr: {pending: object}, finalized: object|null}} `finalized`
 *   is the entry that was pending before this push (for the binder to
 *   finalize/clean up), or `null` if the slot was empty.
 */
export function pushUndoable(mgr, entry) {
    return {
        mgr: { pending: entry },
        finalized: mgr.pending || null,
    };
}

/**
 * Consume the pending entry (if any), emptying the slot.
 *
 * @param {{pending: object|null}} mgr
 * @returns {{entry: object|null, mgr: {pending: null}}}
 */
export function takeUndo(mgr) {
    return { entry: mgr.pending || null, mgr: { pending: null } };
}

/**
 * Empty the slot without treating it as "taken" for undo — used when the
 * undo window expires naturally (the binder's timer fires) rather than the
 * user clicking undo.
 *
 * @param {{pending: object|null}} mgr
 * @returns {{mgr: {pending: null}, finalized: object|null}} `finalized` is
 *   whatever was pending before the clear, or `null` if the slot was
 *   already empty.
 */
export function clearUndo(mgr) {
    return { mgr: { pending: null }, finalized: mgr.pending || null };
}
