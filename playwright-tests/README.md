# Playwright tests

`phase0.spec.js` is an end-to-end smoke test for the Phase 0 (Security &
Integrity) release: it logs in (or completes first-run setup on a fresh
library), saves an article, opens it in the reader, deletes it, and confirms
it's gone from the inbox. It's a hand-run check for the 0.2 release pass —
not wired into CI yet.

## Running it

You need a Tiro server running somewhere reachable (a scratch library is
fine — the test creates and deletes its own article, but if the library
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

## Notes

- Screenshots taken during manual verification passes (e.g.
  `2026-07-03/*.png`) are gitignored — only the spec, `package.json`, and
  this README are tracked in version control (see `.gitignore`).
- `node_modules/` is also gitignored; run `npm install` locally before
  running tests.
