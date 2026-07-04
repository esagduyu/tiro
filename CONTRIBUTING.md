# Contributing to Tiro

Thanks for your interest. Tiro is a solo-maintained, local-first project in public alpha (0.2.x) — small, focused contributions are the easiest to review and land.

## Dev setup

Prerequisites: Python 3.11+ and [uv](https://docs.astral.sh/uv/). Never use pip directly — all dependency and environment management goes through uv.

```bash
git clone https://github.com/esagduyu/tiro.git
cd tiro
uv sync              # creates the venv and installs everything (incl. dev deps)
uv run tiro init     # creates config.yaml + a local library (optional for tests)
uv run tiro run      # starts the server at localhost:8000
```

## The test bar

```bash
uv run pytest
```

The suite (169 tests at 0.2.0) must pass with **zero warnings** — warnings are treated as failures in review. Tests are fully isolated (temp library, offline, CWD-guarded), so they never touch your real config or library. Add tests with any behavior change; for UI work there is also a Playwright end-to-end spec (`playwright-tests/README.md`).

## Invariants you must not break

`CLAUDE.md` — particularly its "Load-bearing conventions" block — is the authoritative list. The headlines:

- **Auth is fail-closed.** Every route requires auth except the login/setup/status/logout/healthz allowlist. A route-walk test enforces this over `app.routes`, so any route you add is automatically covered — don't weaken the test to make a new route pass; gate the route.
- **Sanitization boundary.** Server-side `sanitize_html` (nh3) runs in the extraction functions (`tiro/ingestion/web.py` / `email.py`) *before* markdown conversion — content reaching `process_article()` is already sanitized. Client-side, markdown renders through marked → DOMPurify, and every server string hitting an `innerHTML` sink goes through `esc()`/`num()`.
- **No CDN at runtime.** Frontend deps are vendored in `tiro/frontend/static/vendor/` (test-enforced). When changing static JS/CSS, bump **all** `?v=N` cache-bust params across every template together (single shared counter — grep first).
- **Config writes go through `persist_config()`** (`tiro/config.py`) — never write `config.yaml` directly. Grep-gated by tests: zero `Path("config.yaml")` and zero `write_text(yaml.dump` under `tiro/`.
- **Four-store consistency.** Articles live in SQLite, ChromaDB, markdown files, and (optionally) cached MP3s. All deletion goes through `tiro/lifecycle.py`'s `delete_article()`; `tiro doctor` reconciles residuals. Don't add a fifth ad-hoc cleanup path.
- **No side-effectful GETs.** Anything that triggers a model call or a write is a POST.

## Docs layout

- `PRODUCT_ROADMAP.md` is the forward-looking source of truth; `PROJECT_TIRO_SPEC.md` is historical (hackathon spec) — don't plan against it.
- `docs/` is **gitignored by design** (local-only plans and notes). Never `git add -f` anything under it.

## Pull requests

- Keep PRs small and single-purpose; describe the *why*, not just the what.
- `uv run pytest` green with zero warnings before opening.
- New dependencies must be AGPL-compatible (MIT/BSD/Apache-2.0/ISC/LGPL/GPL/AGPL are fine; SSPL, BUSL, and "non-commercial" licenses are not) — say so in the PR.
- Update `README.md`/`CLAUDE.md` if you change user-facing behavior or a convention.

## License of contributions

Tiro is licensed [AGPL-3.0-or-later](LICENSE). By submitting a contribution you agree it is licensed under AGPL-3.0-or-later. (Historical note: contributions made before 2026-05-28 remain under their original MIT terms per the grandfather clause in the README.)
