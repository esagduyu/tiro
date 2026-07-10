// node:test drift guard for the frozen anchor-parity fixtures (Task 2, iOS
// campaign). The committed fixtures/anchor-parity.json is the reference the
// Swift AnchorKit port (tiro-ios, Task 8) is validated against — a silent
// mismatch anchors highlights to the WRONG range on iOS with no server-side
// detection. These tests assert the file parses, meets the schema, and is
// STILL byte-identical to what export_anchor_fixtures.mjs regenerates today
// (so a change to annotate.js that shifts a projection/search result can never
// land without either updating the committed fixtures or failing CI).

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { buildVectors, serialize } from "../../../../../scripts/export_anchor_fixtures.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = join(__dirname, "fixtures", "anchor-parity.json");
const committedText = readFileSync(FIXTURE_PATH, "utf8");

test("anchor-parity.json parses as a JSON array", () => {
    const parsed = JSON.parse(committedText);
    assert.ok(Array.isArray(parsed), "top level must be an array");
});

test("anchor-parity.json has at least 15 vectors", () => {
    const parsed = JSON.parse(committedText);
    assert.ok(parsed.length >= 15, `expected >= 15 vectors, got ${parsed.length}`);
});

test("every vector matches the frozen schema", () => {
    const parsed = JSON.parse(committedText);
    for (const v of parsed) {
        assert.equal(typeof v.name, "string", "name must be a string");
        assert.equal(typeof v.markdown, "string", `${v.name}: markdown must be a string`);
        assert.equal(typeof v.plain, "string", `${v.name}: plain must be a string`);
        assert.ok(Array.isArray(v.map), `${v.name}: map must be an array`);
        assert.equal(v.map.length, v.plain.length, `${v.name}: map.length must equal plain.length`);
        for (const m of v.map) {
            assert.ok(Number.isInteger(m), `${v.name}: map entries must be integers`);
        }
        // map[i] = markdown offset of plain char i (markdown[map[i]] === plain[i]).
        for (let i = 0; i < v.plain.length; i++) {
            assert.equal(
                v.markdown[v.map[i]],
                v.plain[i],
                `${v.name}: map invariant broken at plain index ${i}`
            );
        }
        assert.ok(Array.isArray(v.searches), `${v.name}: searches must be an array`);
        for (const s of v.searches) {
            assert.equal(typeof s.quote, "string", `${v.name}: search.quote must be a string`);
            assert.equal(typeof s.prefix, "string", `${v.name}: search.prefix must be a string`);
            assert.equal(typeof s.suffix, "string", `${v.name}: search.suffix must be a string`);
            // expect_start/expect_end are both integers, or both null (soft-fail).
            const bothNull = s.expect_start === null && s.expect_end === null;
            const bothInt = Number.isInteger(s.expect_start) && Number.isInteger(s.expect_end);
            assert.ok(
                bothNull || bothInt,
                `${v.name}: expect_start/expect_end must be both-int or both-null`
            );
        }
    }
});

test("at least one soft-fail (null-expectation) search exists", () => {
    const parsed = JSON.parse(committedText);
    const nulls = parsed.flatMap((v) => v.searches).filter((s) => s.expect_start === null);
    assert.ok(nulls.length >= 1, "expected at least one null-expectation search vector");
});

test("committed fixtures are byte-identical to a fresh regeneration (drift guard)", () => {
    // buildVectors() is pure (no I/O), so this both proves re-run determinism
    // and that the committed file is in sync with the current exporter/annotate.js.
    const regenerated = serialize(buildVectors());
    assert.equal(
        regenerated,
        committedText,
        "anchor-parity.json is stale — re-run `node scripts/export_anchor_fixtures.mjs` and commit."
    );
});
