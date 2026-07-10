import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { ICONS, icon } from "../icons.js";

const here = dirname(fileURLToPath(import.meta.url));
const macroSrc = readFileSync(
  join(here, "..", "..", "..", "templates", "_icons.html"), "utf8");

test("icon() renders svg with viewBox, class, aria-hidden", () => {
  const s = icon("inbox");
  assert.match(s, /^<svg viewBox="0 0 24 24" width="17" height="17"/);
  assert.match(s, /class="ti ti-inbox"/);
  assert.match(s, /aria-hidden="true"/);
  assert.ok(s.includes(ICONS.inbox.body));
});

test("icon() honors size/cls/strokeWidth and fill flag", () => {
  const s = icon("star", { size: 12, cls: "vip-star" });
  assert.match(s, /width="12" height="12"/);
  assert.match(s, /class="ti ti-star vip-star"/);
  assert.match(s, /fill="currentColor"/); // star is fill: true
  const t = icon("close", { strokeWidth: 1.8 });
  assert.match(t, /stroke-width="1.8"/);
  assert.match(t, /fill="none"/);
});

test("icon() throws on unknown name", () => {
  assert.throws(() => icon("nope"), /unknown icon: nope/);
});

test("expected icon names all exist", () => {
  const names = ["bookmark","inbox","layers","graph","bar-chart","settings",
    "book-open","library","highlighter","star","heart","thumb-up","thumb-down",
    "info","trash","refresh","external","close","file-text","tag","copy",
    "message","zap","basket","download","sun","moon","search","logout","clock",
    "archive","undo","play","pause","pencil","note","check","chevron-left",
    "chevron-right","chevron-up","chevron-down","kebab","ellipsis","filter",
    "grid","plus","mail","rss","globe","alert","calendar","send","eye",
    "headphones","phone"];
  for (const n of names) assert.ok(ICONS[n], `missing icon: ${n}`);
  assert.equal(Object.keys(ICONS).length, names.length,
    "ICONS has extra/missing entries vs spec §4");
});

test("_icons.html macro paths match ICONS verbatim", () => {
  // The macro file carries a flat {% set paths = { "name": '<path .../>' , ... } %}
  // Every entry's body string must equal ICONS[name].body exactly.
  const m = macroSrc.match(/\{%-?\s*set paths = \{([\s\S]*?)\}\s*-?%\}/);
  assert.ok(m, "no paths dict in _icons.html");
  const entryRe = /"([a-z-]+)":\s*'((?:[^'\\]|\\.)*)'/g;
  let count = 0, e;
  while ((e = entryRe.exec(m[1])) !== null) {
    const [, name, body] = e;
    assert.ok(ICONS[name], `macro has unknown icon ${name}`);
    assert.equal(body, ICONS[name].body, `body mismatch for ${name}`);
    count++;
  }
  assert.equal(count, Object.keys(ICONS).length, "macro missing icons");
});
