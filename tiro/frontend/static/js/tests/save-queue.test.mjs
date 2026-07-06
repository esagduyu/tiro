// node:test coverage for js/save-queue.js — the offline save queue's PURE
// logic core (M3.1 Task 3). See save-queue.js's own header comment for why
// this is a plain module (no localStorage/fetch/DOM) — same posture as
// core.js's esc/num/formatDate/timeAgo and sw-routing.js's swRouteFor.

import { test } from "node:test";
import assert from "node:assert/strict";
import {
    enqueueSave,
    dequeueForRetry,
    serializeQueue,
    deserializeQueue,
    SAVE_QUEUE_STORAGE_KEY,
    SAVE_QUEUE_CAP,
} from "../save-queue.js";

test("SAVE_QUEUE_STORAGE_KEY and SAVE_QUEUE_CAP match the binding spec", () => {
    assert.equal(SAVE_QUEUE_STORAGE_KEY, "tiro-save-queue");
    assert.equal(SAVE_QUEUE_CAP, 20);
});

test("enqueueSave: appends a brand-new URL at the back", () => {
    const { queue, dropped } = enqueueSave([], { url: "https://a.example", is_vip: false, ts: 1 });
    assert.deepEqual(queue, [{ url: "https://a.example", is_vip: false, ts: 1 }]);
    assert.deepEqual(dropped, []);

    const { queue: queue2 } = enqueueSave(queue, { url: "https://b.example", is_vip: true, ts: 2 });
    assert.deepEqual(queue2, [
        { url: "https://a.example", is_vip: false, ts: 1 },
        { url: "https://b.example", is_vip: true, ts: 2 },
    ]);
});

test("enqueueSave: re-queuing an already-queued URL updates it IN PLACE, no duplicate, no reordering", () => {
    const initial = [
        { url: "https://a.example", is_vip: false, ts: 1 },
        { url: "https://b.example", is_vip: false, ts: 2 },
        { url: "https://c.example", is_vip: false, ts: 3 },
    ];
    const { queue, dropped } = enqueueSave(initial, { url: "https://a.example", is_vip: true, ts: 99 });
    assert.equal(queue.length, 3);
    assert.deepEqual(dropped, []);
    // Position preserved (still first) -- fields updated.
    assert.deepEqual(queue[0], { url: "https://a.example", is_vip: true, ts: 99 });
    assert.deepEqual(queue[1], { url: "https://b.example", is_vip: false, ts: 2 });
    assert.deepEqual(queue[2], { url: "https://c.example", is_vip: false, ts: 3 });
});

test("enqueueSave: does not mutate the input queue array", () => {
    const initial = [{ url: "https://a.example", is_vip: false, ts: 1 }];
    const frozenCopy = JSON.parse(JSON.stringify(initial));
    enqueueSave(initial, { url: "https://a.example", is_vip: true, ts: 2 });
    enqueueSave(initial, { url: "https://b.example", is_vip: false, ts: 3 });
    assert.deepEqual(initial, frozenCopy);
});

test("enqueueSave: cap at 20 -- oldest dropped, newest survives", () => {
    let queue = [];
    for (let i = 0; i < 20; i++) {
        ({ queue } = enqueueSave(queue, { url: `https://${i}.example`, is_vip: false, ts: i }));
    }
    assert.equal(queue.length, 20);

    const { queue: overCap, dropped } = enqueueSave(queue, { url: "https://20.example", is_vip: false, ts: 20 });
    assert.equal(overCap.length, 20);
    assert.equal(dropped.length, 1);
    assert.equal(dropped[0].url, "https://0.example", "oldest (front) entry must be the one dropped");
    assert.equal(overCap[0].url, "https://1.example");
    assert.equal(overCap[overCap.length - 1].url, "https://20.example");
});

test("enqueueSave: updating an existing entry never triggers a drop (count unchanged)", () => {
    let queue = [];
    for (let i = 0; i < 20; i++) {
        ({ queue } = enqueueSave(queue, { url: `https://${i}.example`, is_vip: false, ts: i }));
    }
    const { queue: updated, dropped } = enqueueSave(queue, { url: "https://5.example", is_vip: true, ts: 999 });
    assert.equal(updated.length, 20);
    assert.deepEqual(dropped, []);
    assert.equal(updated[5].is_vip, true);
});

test("dequeueForRetry: FIFO order, empty queue yields null", () => {
    const queue = [
        { url: "https://a.example", is_vip: false, ts: 1 },
        { url: "https://b.example", is_vip: false, ts: 2 },
    ];
    const { next, rest } = dequeueForRetry(queue);
    assert.deepEqual(next, { url: "https://a.example", is_vip: false, ts: 1 });
    assert.deepEqual(rest, [{ url: "https://b.example", is_vip: false, ts: 2 }]);
    // Input untouched.
    assert.equal(queue.length, 2);

    const { next: emptyNext, rest: emptyRest } = dequeueForRetry([]);
    assert.equal(emptyNext, null);
    assert.deepEqual(emptyRest, []);
});

test("serialize/deserialize: round-trip preserves order and fields", () => {
    const queue = [
        { url: "https://a.example", is_vip: false, ts: 111 },
        { url: "https://b.example", is_vip: true, ts: 222 },
    ];
    const raw = serializeQueue(queue);
    assert.equal(typeof raw, "string");
    const roundTripped = deserializeQueue(raw);
    assert.deepEqual(roundTripped, queue);
});

test("serializeQueue: null/undefined queue serializes as an empty array", () => {
    assert.equal(serializeQueue(null), "[]");
    assert.equal(serializeQueue(undefined), "[]");
    assert.equal(serializeQueue([]), "[]");
});

test("deserializeQueue: tolerant of garbage -- bad JSON, wrong shape, non-array all yield []", () => {
    assert.deepEqual(deserializeQueue(null), []);
    assert.deepEqual(deserializeQueue(undefined), []);
    assert.deepEqual(deserializeQueue(""), []);
    assert.deepEqual(deserializeQueue("not json at all {{{"), []);
    assert.deepEqual(deserializeQueue("null"), []);
    assert.deepEqual(deserializeQueue("42"), []);
    assert.deepEqual(deserializeQueue('"just a string"'), []);
    assert.deepEqual(deserializeQueue("{}"), []);
});

test("deserializeQueue: drops malformed entries within an otherwise-valid array, keeps the rest", () => {
    const raw = JSON.stringify([
        { url: "https://good.example", is_vip: true, ts: 5 },
        { url: 123, is_vip: false, ts: 6 }, // url not a string
        null,
        "just a string",
        { is_vip: false, ts: 7 }, // missing url entirely
        { url: "https://also-good.example" }, // missing is_vip/ts -- defaults filled
    ]);
    const result = deserializeQueue(raw);
    assert.equal(result.length, 2);
    assert.deepEqual(result[0], { url: "https://good.example", is_vip: true, ts: 5 });
    assert.equal(result[1].url, "https://also-good.example");
    assert.equal(result[1].is_vip, false);
    assert.equal(typeof result[1].ts, "number");
});
