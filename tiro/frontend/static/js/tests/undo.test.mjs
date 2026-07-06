// node:test coverage for js/undo.js — PURE single-slot undo manager, NO
// DOM/timers (M3.2 Task 2). The timer/toast/keyboard binder is T3's job;
// this only tests the pure push/take/clear slot semantics. Nothing imports
// undo.js yet.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createUndoManager, pushUndoable, takeUndo, clearUndo } from "../undo.js";

test("createUndoManager: starts with an empty slot", () => {
    assert.deepEqual(createUndoManager(), { pending: null });
});

test("pushUndoable: installs the entry and reports nothing finalized when the slot was empty", () => {
    const entry = { label: "Archived", undo: () => {} };
    const { mgr, finalized } = pushUndoable(createUndoManager(), entry);
    assert.equal(mgr.pending, entry);
    assert.equal(finalized, null);
});

test("pushUndoable: a second push finalizes the first entry", () => {
    const first = { label: "Archived", undo: () => {} };
    const second = { label: "Snoozed", undo: () => {} };

    const step1 = pushUndoable(createUndoManager(), first);
    const step2 = pushUndoable(step1.mgr, second);

    assert.equal(step2.finalized, first, "the displaced entry is exactly the first one pushed");
    assert.equal(step2.mgr.pending, second, "the slot now holds the second entry");
});

test("takeUndo: consumes the pending entry and empties the slot", () => {
    const entry = { label: "Archived", undo: () => {} };
    const { mgr } = pushUndoable(createUndoManager(), entry);

    const taken = takeUndo(mgr);
    assert.equal(taken.entry, entry);
    assert.deepEqual(taken.mgr, { pending: null });
});

test("takeUndo: on an empty manager returns a null entry and an unchanged (empty) manager", () => {
    const taken = takeUndo(createUndoManager());
    assert.equal(taken.entry, null);
    assert.deepEqual(taken.mgr, { pending: null });
});

test("clearUndo: empties a pending slot and reports the previously pending entry as finalized", () => {
    const entry = { label: "Archived", undo: () => {} };
    const { mgr } = pushUndoable(createUndoManager(), entry);

    const cleared = clearUndo(mgr);
    assert.equal(cleared.finalized, entry);
    assert.deepEqual(cleared.mgr, { pending: null });
});

test("clearUndo: on an empty manager reports null finalized and stays empty", () => {
    const cleared = clearUndo(createUndoManager());
    assert.equal(cleared.finalized, null);
    assert.deepEqual(cleared.mgr, { pending: null });
});

test("purity: none of push/take/clear mutate the manager object passed in", () => {
    const entry1 = { label: "Archived", undo: () => {} };
    const entry2 = { label: "Snoozed", undo: () => {} };

    const mgr0 = createUndoManager();
    const { mgr: mgr1 } = pushUndoable(mgr0, entry1);
    assert.deepEqual(mgr0, { pending: null }, "mgr0 untouched after being pushed from");

    const { mgr: mgr2 } = pushUndoable(mgr1, entry2);
    assert.equal(mgr1.pending, entry1, "mgr1 untouched after being pushed from again");

    const { mgr: mgr3 } = takeUndo(mgr2);
    assert.equal(mgr2.pending, entry2, "mgr2 untouched after takeUndo");
    assert.deepEqual(mgr3, { pending: null });
});
