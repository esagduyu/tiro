# Playwright tests

`phase0.spec.js` is an end-to-end smoke test for the Phase 0 (Security &
Integrity) release: it logs in (or completes first-run setup on a fresh
library), saves an article, opens it in the reader, deletes it, and confirms
it's gone from the inbox. It's a hand-run check for the 0.2 release pass —
not wired into CI yet.

`annotations.spec.js` (M2.2 Task 2) covers the reader annotation UI: it logs
in/sets up, saves the same `example.com` test article, opens the reader, and
drives a programmatic selection over the first paragraph (`document.
createRange` + `selection.addRange`, then a dispatched `mouseup` — the same
shape a real click-drag selection produces). It asserts the floating
selection toolbar appears (and does NOT appear for a collapsed selection),
that clicking the yellow color dot POSTs `/api/articles/{id}/highlights` and
paints the highlight via the CSS Custom Highlight API (`CSS.highlights.get
('tiro-hl-yellow').size >= 1`) without a page reload, and that the highlight
is still painted after a reload (re-fetched from `GET /api/articles/{id}/
annotations` and repainted). Also asserts zero console errors across the
whole flow. Run it against a SCRATCH library — never the real one, since it
ingests+leaves behind a real article.

`telemetry.spec.js` (M2.3 Task 2) covers the reading-session telemetry
tracker + settings toggle: with telemetry off (the default), it forces the
tab to `visibilitychange -> hidden` (the trigger reader.js listens for) and
asserts NO `/session` POST fires; it then enables telemetry via the actual
`/settings` toggle button, reloads the reader (so the server-rendered
`data-telemetry` attribute picks up the change), generates some scroll/
activity, forces the tab hidden again, and asserts a `/session` POST fires
with a 200 response (there's no GET for `reading_sessions`, so the response
status from the real `sendBeacon`-triggered request is the "a row landed"
evidence — see the spec's comments for why the request body itself isn't
assertable here). Also asserts zero console errors. Leaves the shared
`example.com` test article's telemetry preference set back to disabled at
the end of its second test.

## Running it

You need a Tiro server running somewhere reachable (a scratch library is
fine — the tests create their own article(s); `phase0.spec.js` deletes its
own, `annotations.spec.js` currently leaves its example.com article in place
and re-uses it on a subsequent run rather than re-ingesting. If the library
already has a password set, you need to know it).

```bash
cd playwright-tests
npm install
npx playwright install chromium   # first time only, downloads the browser
npx playwright test
```

## Configuration

Both are optional environment variables:

| Variable        | Default                   | Purpose                              |
|-----------------|----------------------------|---------------------------------------|
| `TIRO_URL`      | `http://localhost:8000`   | Base URL of the running server        |
| `TIRO_PASSWORD` | `phase0-spec-test-pass`   | Password to log in with / set on first run |

Example against a non-default port:

```bash
TIRO_URL=http://localhost:8000 TIRO_PASSWORD=my-scratch-pass npx playwright test
```

Run a single spec file directly:

```bash
TIRO_PASSWORD=my-scratch-pass npx playwright test annotations.spec.js
```

## Notes

- Screenshots taken during manual verification passes (e.g.
  `2026-07-03/*.png`) are gitignored — only the tracked specs,
  `package.json`, and this README are tracked in version control (see
  `.gitignore`).
- `node_modules/` is also gitignored; run `npm install` locally before
  running tests.
