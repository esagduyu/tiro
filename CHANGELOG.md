# Changelog

All notable changes to Tiro are documented here, grouped by release and domain.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/); versions
follow the roadmap's release targets (see PRODUCT_ROADMAP.md). Dates are the
day the release was tagged.

## [Unreleased]

- Phase 2b (Obsidian bidirectional sync, 0.4.5 slot) — deferred by owner
  decision 2026-07-06; scope intact in PRODUCT_ROADMAP.md.

## [0.6.0] — `feeds-beta` (Phase 4: RSS & imports)

Recurring ingestion arrives: subscribe to RSS/Atom feeds, bulk-import an
existing reading library, and save straight from the browser with a selection
captured as a highlight. `STATIC_VERSION` 66 → 67.

### Added — RSS & feeds
- **Feed subscriptions** (migration 013: `feeds` + `feed_entries`). Subscribe
  by feed URL *or* page URL — `POST /api/feeds` autodiscovers a
  `<link rel="alternate" type="application/rss+xml|atom+xml">` when the URL is
  an HTML page (30s timeout, 5-redirect cap, 10 MB body cap; 409
  `already_subscribed` on a duplicate feed URL). Feeds carry a per-feed
  `fetch_interval_minutes`, conditional-GET validators (etag/last-modified),
  and an `error_count` backoff.
- **Recurring poll loop** on the new `PeriodicTask` scheduler registry
  (`tiro/scheduler.py`, extended not replaced — the imap/digest/vector loops
  were refactored onto it). Each poll cycle writes one audit line. New articles
  land with `ingestion_method="rss"`; the `feed_entries` dedup ledger keeps a
  deleted article from being resurrected by the next poll (its `article_id` is
  nulled, the ledger row survives).
- **Feed management** (`GET/POST /api/feeds`, `PATCH/DELETE /api/feeds/{id}`,
  `POST /api/feeds/{id}/check`, `POST /api/feeds/check-all`) and a `/feeds`
  management page (grouped by folder, status pills, check-now / pause / rename /
  delete-with-or-without-articles). `DELETE ?delete_articles=true` takes an
  `auto_backup` first, then loops the `delete_article` lifecycle coordinator
  per article — never a raw cascade. Sidebar Library entry + **Shift+F**
  keyboard shortcut (inbox & reader; `n` was already taken).
- **OPML round-trip**: `GET /api/feeds/export` (standalone OPML 2.0, nested one
  level by folder) and `POST /api/feeds/import` (multipart upload, flattens
  nested outlines into a `folder` path, dedupes by url, returns
  `{added, skipped, errors}`; rejects >5 MB or unparseable).

### Added — library importers
- **Three importers** — Readwise JSON, Instapaper CSV, Omnivore zip — via
  `POST /api/import/{kind}` (single-slot background job; 409 `import_running`
  when one is active; `GET /api/import/status` polls progress) and CLI verbs
  `tiro import-readwise|import-instapaper|import-omnivore` (always skip
  existing). Content is re-fetched where possible; a paywalled/failed re-fetch
  falls back to a **stub article tagged `import-stub`**. Original timestamps are
  preserved. Imported articles use `ingestion_method="import"`.
- **Anchored highlight import**: Readwise highlights are anchored against the
  re-fetched markdown body with the same D7.4 machinery the reader uses;
  unlocatable highlights are **skipped and counted, never hand-placed**. A
  Settings "Import library" card drives it with a live progress bar.

### Added — Chrome extension advanced save
- Background service worker registers three context-menu items (Save /
  Save as VIP / Save with selection as highlight) and the popup gains a
  save-all-open-tabs action. Selecting text and saving anchors it as a
  highlight server-side via `highlight_text` on `POST /api/ingest/url`
  (soft-fails to no-highlight, still 200, if the selection can't be located).

### Added — owner UX wave
- **Reading progress bar** in the reader (fixed, accent fill, both themes; a
  `ResizeObserver` re-measures when late-loading images reflow the body).
- **Unread-first inbox** with a Library view toggle (`a` key / `?view=library`)
  that reveals read + archived rows — read/unread and active/decayed are
  treated as orthogonal axes.

### Added — export & backup
- `metadata.json` gains an additive `feeds` key (durable subscription columns
  only; transient fetch state and the `feed_entries` ledger excluded).
  `sources.opml` marks feed-backed sources with `type="rss"` + `xmlUrl`.
  `tiro import` merges bundle feeds by url; `tiro backup`/`restore` round-trips
  feed rows wholesale. See EXPORT_SCHEMA.md.

### Fixed
- **`load_config()` now honors `TIRO_CONFIG`** (ON-8 root-cause hardening).
  A bare `load_config()` (from `tiro/app.py`, `scripts/`, or any script run
  from the repo root) previously ignored the `TIRO_CONFIG` env var and
  defaulted to CWD-relative `./config.yaml`, so a load→persist round-trip
  could silently corrupt the owner's real config. Path precedence is now
  explicit-arg > `TIRO_CONFIG` > `./config.yaml`, matching `run.py`/`cli.py`/
  the MCP server. No signature change (additive default → `None`).

### Added — iOS device pairing

- **Device pairing for the iOS client** (`/setup/qr?mode=device`,
  `POST /api/auth/pair`). The `/setup/qr` page becomes two labeled panels —
  browser sign-in (unchanged `login/qr` QR) and app pairing — the latter
  encoding a `tiro://pair?url=…&code=…` QR the native app scans in-app to
  exchange a one-time code for a long-lived `ios:<device_name>` API token.
  Mirrors the QR-login token machinery exactly (sha256-only storage,
  15-minute TTL, atomic single-use consume, generic-400 failures, no-store);
  new `device_pair_codes` table (migration 012); doctor purges expired/used
  codes in the same housekeeping bucket as login tokens.

### Design pass — full frontend redesign (`design/codex-pass`, `STATIC_VERSION` 66)

Whole-app visual and interaction redesign, landed as a sequence of
self-contained tasks (icons/tokens/chrome first, then a page-by-page pass,
then motion/dark-theme/glyph closeout). No backend or API surface changed.

- **Icons**: a single canonical SVG icon set (`tiro/frontend/static/js/icons.js`
  for JS call sites, `_icons.html`'s `icon()` Jinja macro for templates, kept
  in sync by a dedicated test) replaces every emoji/dingbat/HTML-entity glyph
  across the app — nav, toolbars, modals, cards, empty states, close buttons.
- **Chrome**: CSS custom-property token layer (color/spacing/radius/type
  scale, light + dark) and shared component primitives (modal, overlay,
  banner surfaces); rebuilt 240px sidebar with an icon rail; phone chrome
  replaced with a bottom tab bar + Library/More sheets (hamburger menu
  removed).
- **Per-page pass**: inbox (icon cards, toolbar, serif pagination, empty
  states), reader (chrome, audio player, callouts, phone action bar), digest,
  stats, graph, sources, wiki, highlights, settings, login, QR/remote-access
  wizards, and the offline fallback page all restyled on the new token/
  component layer.
- **Motion**: a consistent transition/easing pass across interactive surfaces
  (cards, modals, sheets, toasts) with `prefers-reduced-motion: reduce`
  support throughout, plus a dark-theme (Roman Night) contrast audit.
- **Closeout sweep**: final glyph audit — remaining literal `&times;` close
  buttons (graph.html's node panel, base.html's LAN-over-HTTP banner
  dismiss, reader.html's analysis/highlights panel close buttons) converted
  to the canonical `close` icon; orphaned `.shortcuts-close` and
  `.graph-node-panel-close` CSS rules removed (both close buttons already
  used `.modal-close`); LAN-banner phone padding constant aligned to the
  real phone-header height. `STATIC_VERSION` 65 → 66.

## [0.5.0] — 2026-07-06 · `private-remote-beta` (Phase 3)

Read your library from a phone without giving up local ownership.

### Added — remote access & auth
- **QR one-time login**: `/setup/qr` renders a QR code (segno, inline SVG)
  encoding a single-use, sha256-hashed, 15-minute token; scanning it on a
  phone creates a normal session (`GET /login/qr`, deliberate auth-allowlist
  entry). Tokens are never usable as API bearer tokens or session cookies;
  expired/used tokens are purged by `tiro doctor`.
- **Snooze**: `articles.snoozed_until` (migration 011) with
  `PATCH /api/articles/{id}/snooze` (`until` / presets
  tonight·tomorrow·weekend·next_week / `null` to wake). Snoozed articles are
  hidden from the inbox only — digests, classification, decay, export, stats,
  and MCP still see them. Inbox gains a Snoozed toggle, wake-time chips, and
  a "Wake now" action.
- **mDNS discovery** (opt-in, `mdns_enabled`): advertises `{hostname}.local`
  on the LAN via python-zeroconf; the advertised name is dynamically added to
  the Host allowlist. `tiro status` reports mDNS + remote URL.
- **TLS flags**: `tiro run --cert/--key` (both entry points) pass through to
  uvicorn; validation errors surface before startup.
- **LAN-HTTP warning banner**: dismissable, shown on every page when bound to
  a non-loopback host without TLS; startup logs the auth URL + guidance.
- **Reverse-proxy support**: `extra_allowed_hosts` (exact-match) and
  `trust_proxy_headers` (X-Forwarded-Proto only; X-Forwarded-Host is never
  trusted) — both default off, set by the wizard or manually.

### Added — PWA & offline
- **Installable PWA**: `manifest.webmanifest` + generated 192/512 icons,
  install tags on every page including login.
- **Service worker** (`/sw.js`, version-injected from `STATIC_VERSION`):
  cache-first for static assets, network-first with LRU-50 cache fallback for
  article JSON, `/offline` fallback page listing cached articles (rendered
  through the same marked→DOMPurify pipeline). Never caches authenticated
  HTML, mutations, or auth routes. Every `STATIC_VERSION` bump invalidates
  all SW caches.
- **Offline save queue**: failed saves (network errors only) queue in
  localStorage (cap 20) and drain on reconnect with per-item toasts; poison
  entries are dropped, never retried forever.
- **Add-to-Home-Screen hint**: one-time, mobile-viewport-only.
- **`/setup/remote` wizard**: detects Tailscale (`tailscale status --json`,
  never executes privileged commands), shows the tailored
  `tailscale serve` command, saves `remote_url` (optionally allowlisting its
  hostname live, no restart), and offers a reachability probe.

### Added — triage UX
- **Swipe triage** (pointer events, no gesture library): swipe right =
  archive (mark read), swipe left = snooze preset sheet; direction-locked so
  vertical scrolling is never hijacked; flick support;
  `prefers-reduced-motion` respected.
- **Undo everywhere**: semantic single-slot undo (5s toast + `u` key) for
  swipe archive/snooze and keyboard rate/VIP — restores real server state
  (unread, prior rating, unsnooze). Guarded by per-article sequence tokens
  against out-of-order responses. Deletion keeps its confirm dialog.
- **Triage progress**: "N to zero" pill live-updating across actions and
  undos (single count source shared with the sidebar badge), plus an
  inbox-zero state.

### Changed
- `PATCH /api/articles/{id}/read` accepts an optional `{"is_read": false}`
  body and `PATCH /api/articles/{id}/rate` accepts `{"rating": null}` —
  both backward-compatible; unmark paths never decrement reading stats.
- Logout best-effort clears the service worker's article caches.
- `snoozed_until` is returned by article list, detail, and search payloads.

### Fixed
- zeroconf registration deadlocked FastAPI's event loop (~21s frozen startup,
  registration never succeeded) — fixed two-layer (`use_asyncio=False` +
  `asyncio.to_thread`), live-verified.
- The mDNS-advertised hostname was rejected by our own Host allowlist,
  making discovery self-defeating — the registered name is now allowlisted
  dynamically.
- Undo could restore fabricated state for articles surfaced by search but
  absent from the inbox cache; such actions now skip the undo offer.
- Rapid consecutive ratings could corrupt the undo restore target; a stale
  late-arriving response could clobber newer state (fixed via optimistic
  capture + per-article sequence tokens).
- Snoozed-timestamp parsing broke on Safari (space-separated datetimes).
- Archiving/deleting a snoozed-unread article drifted the unread count.
- `tiro doctor --fix` cache-write failures and misc. hardening (no-store
  headers on QR pages, hostless remote URLs rejected).

## [0.4.0] — 2026-07-06 · `reader-memory-beta` (Phase 2)

Make Tiro a place to think: highlights, notes, and the signals they need.

### Added — annotations (M2.1 backend, M2.2 UI)
- **Highlights & notes with files as truth**: `annotations/{stem}.jsonl` +
  `notes/{stem}.md` sidecars are authoritative; SQLite (`highlights`,
  `notes`, migration 009) is a derived index reconciled files-win at startup
  and by `tiro doctor` (with a mass-deletion guard that refuses to wipe rows
  when sidecars go missing wholesale).
- **W3C-style anchor model** (`tiro/anchors.py`): prefix/quote/suffix +
  offsets + content hash in markdown space; reconciliation statuses
  exact / shifted / hash_mismatch / missing with context-first matching.
- **Reader annotation UI**: select text → 4-color highlight / note / copy.
  Painting uses the CSS Custom Highlight API (the rendered DOM is never
  mutated). Margin panel with per-highlight notes (markdown, live preview),
  color swap, delete, re-anchor warnings; article-level note drawer.
  Unanchorable selections fail soft.
- **`/highlights` review view**: server-side filters (color/source/date),
  grouped by article, keyboard `h`.
- **Sidecar-first CRUD API** (`/api/articles/{id}/annotations`,
  `/api/highlights`, note endpoints) — file writes precede index writes.
- Export/backup/import round-trip annotations; `delete_article` cleans them;
  MCP gains `get_highlights` (11th tool).

### Added — telemetry, Obsidian, digest (M2.3)
- **Reading telemetry** (opt-in, default off, strictly local): per-session
  scroll depth, active seconds, per-section dwell → `reading_sessions`
  (migration 010) via a sendBeacon endpoint that no-ops server-side when
  disabled. Deliberately excluded from exports, and scrubbed from backup
  snapshots. Settings toggle with plain-language copy.
- **Obsidian-compatible write mode** (`obsidian_compatible_mode`): new
  ingests gain `aliases`, `created`, and `related` wikilink frontmatter;
  flag-off output is byte-identical (golden-tested).
- **Digest highlight recap**: a "Highlights this week" section (ranked
  digest variant) from the last 7 days of highlights + notes; zero
  highlights = zero LLM calls.

### Added — frontend platform (M2.0)
- Native **ES modules** restructuring: shared `js/core.js`
  (esc/renderMarkdown/toasts/fetch), per-page entry modules, old top-level
  JS deleted; base.html import map keeps imported modules cache-busted.
- **node:test harness** for pure frontend functions (CI-enforced).

### Changed
- `delete_article` now coordinates seven stores (rows, junctions, ChromaDB,
  markdown, audio, annotation sidecars, reading sessions).
- The `tiro` CLI honors `TIRO_CONFIG` like `run.py`/`tiro-mcp` (footgun fix).
- Agent-CLI AI backends (claude-cli/codex-cli) scrub inherited
  `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` from the child environment so
  subscription auth is actually used.

### Fixed
- Deleting any article cited by a wiki page crashed on a foreign key (M2.1
  final review); annotation reconcile crashed on corrupt/duplicate JSONL
  lines — both now heal gracefully.
- ES-module double-instantiation via versioned script tags (duplicate save
  handler → duplicate articles) — fixed with the import map.
- Anchor reconciliation preferred stale offsets over context matches;
  markdown projection corrupted offsets around snake_case/math asterisks
  (flanking guards added).
- Escape while the selection toolbar was open navigated away from the
  reader; highlight-filter clicks during in-flight fetches were dropped
  (latest-wins tokens).
- Backup snapshots leaked telemetry rows despite the local-only promise
  (now scrubbed, restore-tested).

## [0.3.5] — 2026-07-05 · `wiki-alpha` (Phase 1b)

### Added
- **Library Wiki (W1, on-demand)**: LLM-synthesized, citation-mandatory wiki
  pages over entity/tag nodes (`{library}/wiki/**/*.md`, files as truth,
  derived index reconciled by startup + doctor). Generation uses article
  summaries + user-editable `_schema.md` + the page's own prior body — never
  other wiki pages. Zero resolvable citations = generation discarded.
  `/wiki` list + page views, graph integration, wikilinks resolved
  client-side, `user_pinned_note` survives regeneration.

## [0.3.0] — 2026-07-05 · `local-beta` (Phase 1)

### Added — foundation (M1.0)
- `llm_call()` chokepoint with capability tiers (heavy/light) and providers:
  anthropic, openai-compatible, and opt-in experimental **claude-cli /
  codex-cli subscription backends** (Roadmap Decision #7); fake backend for
  tests; audit logging built into the chokepoint.
- Versioned **schema migrations** (`PRAGMA user_version`, pre-migrate
  backups), ULID `uid` columns on articles/entities/tags, background-task
  **scheduler registry**, single-owner article-list SQL (`tiro/queries.py`),
  prompts-as-data templates, `STATIC_VERSION` cache-busting, **GitHub
  Actions CI** (ruff + pytest on 3.11/3.13).

### Added — backup & portability (M1.1)
- `tiro backup` / `tiro restore`: tar.zst snapshots with portable embeddings,
  atomic writes, in-library-restore safety, auto-backup with retention,
  snapshots API; export expanded (digests/stats/audio/OPML,
  EXPORT_SCHEMA.md); `tiro import` with skip/overwrite/keep-both merging.

### Added — sources, authors, views (M1.2)
- Source delete (lifecycle-coordinated, auto-backup first), merge, rename;
  author-level VIP (derived layer feeding decay + digest ranking); saved
  inbox views (cap 20); `/sources` management UI; `TIRO_*` env config
  overlay; build-it-yourself **Docker** support.

## [0.2.0] — 2026-07-04 · `security-alpha` (Phase 0)

### Added / Security
- **Authentication required everywhere**: bcrypt password, session cookies,
  API tokens (`tiro set-password`, `tiro token …`); route-walk test enforces
  the allowlist as an invariant; MCP gated by `TIRO_API_TOKEN`.
- Host-header validation, `Sec-Fetch-Site` CSRF checks, sanitization
  invariant (nh3 at extraction, DOMPurify at render), vendored frontend
  dependencies (no CDNs), no side-effectful GETs.
- `delete_article()` lifecycle coordinator with staged ingestion rollback;
  `tiro doctor [--fix]` four-store reconciliation; JSONL **audit log** for
  every external API call with cost estimates (`tiro audit`); `/healthz`;
  atomic comment-preserving config persistence (`persist_config`).

## [0.1.x] — 2026-02 · hackathon build

The original "Built with Opus 4.6" hackathon build (frozen at
github.com/esagduyu/project-tiro): ingestion (web/email/IMAP), Haiku
enrichment, Opus digests/analysis/classification, semantic search, related
articles, knowledge graph, TTS, reading stats, export, Chrome extension,
MCP server, keyboard-first UI, themes. Its full history is preserved in
this repository's `main`.
