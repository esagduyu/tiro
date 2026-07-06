// Tiro — offline save queue, PURE LOGIC ONLY (M3.1 Task 3).
//
// Decides what the queue looks like after an enqueue/dequeue and how it
// round-trips through localStorage, without touching localStorage, fetch(),
// or any DOM global itself — same "pure functions get node:test coverage"
// posture as core.js's esc/num/formatDate/timeAgo and sw-routing.js's
// swRouteFor. sidebar.js owns the actual localStorage reads/writes, the
// fetch() retry loop, and the DOM indicator; this module only owns the
// array-shape decisions so they can be covered directly under
// `node --test` (see js/tests/save-queue.test.mjs).
//
// A queue entry is `{ url, is_vip, ts }` — `ts` is a client Date.now()
// timestamp recorded at enqueue time (informational only; nothing here
// reads it for ordering, FIFO order is array order).
//
// Storage key lives here (not sidebar.js) so the one string literal is
// shared between the module that reads/writes it and its node tests.
export const SAVE_QUEUE_STORAGE_KEY = "tiro-save-queue";

// Binding spec: "cap 20 — oldest dropped".
export const SAVE_QUEUE_CAP = 20;

/**
 * Add (or update) a queued save.
 *
 * Dedupe by `url`: if the URL is already queued, this UPDATES that entry's
 * `is_vip`/`ts` IN PLACE at its existing position rather than appending a
 * second entry or moving it to the back of the line — re-saving the same
 * URL while offline should not let it cut ahead of (or fall behind) other
 * already-queued URLs. A brand-new URL is appended at the back (FIFO --
 * oldest-queued drains first).
 *
 * If the result exceeds SAVE_QUEUE_CAP, the OLDEST entries (front of the
 * array) are dropped until the cap is met.
 *
 * @param {Array<{url: string, is_vip: boolean, ts: number}>} queue
 * @param {{url: string, is_vip?: boolean, ts?: number}} entry
 * @returns {{queue: Array, dropped: Array}} the new queue array (input is
 *   never mutated) plus any entries dropped to stay under the cap.
 */
export function enqueueSave(queue, entry) {
    const normalized = {
        url: entry.url,
        is_vip: !!entry.is_vip,
        ts: entry.ts ?? Date.now(),
    };

    const existingIdx = queue.findIndex((item) => item.url === entry.url);
    let next;
    if (existingIdx !== -1) {
        next = queue.slice();
        next[existingIdx] = normalized;
    } else {
        next = [...queue, normalized];
    }

    const dropped = [];
    while (next.length > SAVE_QUEUE_CAP) {
        dropped.push(next.shift());
    }

    return { queue: next, dropped };
}

/**
 * Pop the front (oldest) entry off the queue for a retry attempt — FIFO.
 * Does not mutate `queue`. Caller decides whether to commit `rest` back to
 * the queue (e.g. sidebar.js's drain loop keeps `next` at the front, i.e.
 * discards this call's `rest`, when the retry itself hits a network error).
 *
 * @param {Array} queue
 * @returns {{next: object|null, rest: Array}} `next` is `null` for an empty
 *   queue (with `rest` the same empty array).
 */
export function dequeueForRetry(queue) {
    if (queue.length === 0) return { next: null, rest: queue };
    const [next, ...rest] = queue;
    return { next, rest };
}

/**
 * Serialize a queue array for localStorage. Always succeeds — the queue is
 * always plain JSON-serializable data built exclusively by enqueueSave.
 */
export function serializeQueue(queue) {
    return JSON.stringify(queue ?? []);
}

/**
 * Parse a localStorage string back into a queue array. Tolerant of garbage
 * (missing key, invalid JSON, a JSON value that isn't an array, or array
 * entries missing a string `url`) — any of those yield an empty queue (for
 * the whole string) or, for a malformed individual entry, that entry is
 * dropped rather than poisoning the whole parse.
 *
 * @param {string|null|undefined} raw
 * @returns {Array<{url: string, is_vip: boolean, ts: number}>}
 */
export function deserializeQueue(raw) {
    if (!raw) return [];
    let parsed;
    try {
        parsed = JSON.parse(raw);
    } catch (e) {
        return [];
    }
    if (!Array.isArray(parsed)) return [];
    return parsed
        .filter((item) => item && typeof item === "object" && typeof item.url === "string")
        .map((item) => ({
            url: item.url,
            is_vip: !!item.is_vip,
            ts: typeof item.ts === "number" ? item.ts : Date.now(),
        }));
}
