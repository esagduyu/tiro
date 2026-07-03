<p align="center">
  <img src="tiro/frontend/static/logo-128.png" alt="Tiro" width="80" height="80">
</p>

<h1 align="center">Tiro</h1>

<p align="center"><strong>A local-first reading OS for the AI age.</strong></p>

<p align="center"><em>"...without you the oracle was dumb."</em><br><small>— Cicero to Tiro, 53 BC</small></p>

---

Tiro saves web pages and email newsletters as clean markdown on your machine, enriches them with AI-extracted tags, entities, and summaries, and uses Claude Opus 4.6 for deep cross-document reasoning — daily digests that find contradictions between sources, trust analysis on demand, and learned reading preferences that adapt to you.

Named after [Cicero's freedman](https://en.wikipedia.org/wiki/Marcus_Tullius_Tiro) who preserved and organized his master's works for posterity, Tiro does the same for your digital knowledge.

*Built solo for the [Built with Opus 4.6: a Claude Code Hackathon](https://cerebralvalley.ai/e/claude-code-hackathon) (Feb 10–16, 2026) — a week-long virtual hackathon by Anthropic and Cerebral Valley celebrating one year of Claude Code.*

---

## Video Walkthrough

▶ **[Watch the 3-minute hackathon submission video here](https://www.loom.com/share/61ee7ffe076c4b68abeba6dd80423172)**

---

## Why Tiro?

- **Local-first** — Your data lives on your machine as plain markdown files, SQLite, and ChromaDB. No cloud, no lock-in.
- **Model-agnostic data layer** — Content stored in open formats, portable and usable with any AI.
- **Opinionated intelligence** — Opus 4.6 generates ranked digests, clusters articles by topic and entity, and flags bias and unsourced claims.
- **Minimal friction** — One command to run, clean distraction-free reader UI, full keyboard navigation.
- **Own your context** — One-click export of your entire library as portable markdown + JSON.

---

## Quick Start

**Prerequisites:** Python 3.11+, [uv](https://docs.astral.sh/uv/), [Anthropic API key](https://console.anthropic.com/), optionally [OpenAI API key](https://platform.openai.com/api-keys) for TTS:

```bash
git clone https://github.com/esagduyu/tiro.git
cd tiro
uv sync                       # creates venv + installs all dependencies
uv run tiro init              # creates config, library, prompts for API key
uv run tiro run               # starts server at localhost:8000, opens browser
```

On first launch, Tiro opens `/login` and asks you to set a password (with a confirm field) to protect your library — everything else in the app requires it. Sign in once and you're in.

That's it. Save your first article by pasting a URL into the inbox.

**Seed with demo content:** To quickly populate the library with ~22 articles for a full demo experience:

```bash
uv run python scripts/seed_articles.py    # ingests articles, sets ratings + VIP
```

Then run `tiro run`, rate a few more articles if needed, and click "Classify inbox" to see Opus sort your reading into tiers.

**Read on your phone or tablet:** Run with `--lan` to make Tiro accessible from any device on your local network:

```bash
uv run tiro run --lan         # binds to 0.0.0.0, prints your LAN IP
```

Then open `http://<your-ip>:8000` on your phone. The mobile UI has a responsive sidebar and touch-friendly controls.

> **Tip:** If you use `direnv`, set `ANTHROPIC_API_KEY` in your `.envrc` instead of adding it to config.yaml.
>
> **Note:** All `tiro` commands should be run with `uv run tiro` so they execute inside the project's virtual environment.

---

## Security & your data

Tiro is local-first, but "local" doesn't mean "unprotected" — especially once you turn on `--lan` to read from your phone. Here's what's in place:

- **Password auth.** The first time you run Tiro, `/login` asks you to set a password. It's hashed with bcrypt and stored in `config.yaml`; sessions are cookie-based and slide forward 30 days on each use. Forgot it, or want to rotate it? `uv run tiro set-password` resets it from the command line (existing sessions stay valid until they expire). One caveat: until that first password is set, anyone with access to your machine's localhost could claim the instance by setting it first — Tiro only binds to `127.0.0.1` before a password exists, but set yours immediately on first launch (or run `uv run tiro set-password` before ever starting the server).
- **API tokens for non-browser clients.** The Chrome extension, the MCP server, and any scripts you write don't use your password — they use a token. Create one with `uv run tiro token create <name>` (e.g. `chrome-extension`, `mcp`); the raw token is printed once, so copy it immediately. Paste it into the extension via its popup gear icon. `uv run tiro token list` / `uv run tiro token revoke <id>` manage existing tokens.
- **Content sanitization.** HTML from saved pages and emails is sanitized server-side with [nh3](https://github.com/messense/nh3) before it's ever converted to markdown, and everything rendered client-side goes through [DOMPurify](https://github.com/cure53/DOMPurify) on top of marked.js. The entire frontend — markdown renderer, sanitizer, charts, graph — is vendored under `tiro/frontend/static/vendor/`; nothing loads from a CDN, so Tiro works (and stays auditable) fully offline.
- **`tiro doctor`** reconciles the four stores that make up your library — SQLite, ChromaDB, markdown files, and cached audio — after crashes, manual file edits, or interrupted ingests. Run it with the server stopped: `uv run tiro doctor` reports what's inconsistent, `uv run tiro doctor --fix` repairs it (orphaned markdown is quarantined to `{library}/.orphaned/`, never deleted outright). It deliberately refuses to mass-delete article rows if your `articles/` directory looks like it's been moved or is missing, so a misconfigured path can't be mistaken for "everything was deleted."
- **External-API audit log.** Every call out to Anthropic (Haiku/Opus), OpenAI TTS, IMAP, or SMTP is recorded as one JSONL line in `{library}/audit/YYYY-MM-DD.jsonl` — endpoint, tokens/characters, duration, success, and a best-effort cost estimate. `uv run tiro audit` shows today's calls; `uv run tiro audit --month 2026-07` rolls a month up into per-service totals and estimated spend.
- **LAN mode requires a password.** `uv run tiro run --lan` (or `host: 0.0.0.0` in `config.yaml`) refuses to start without a password configured, since it exposes Tiro to your local network. (An explicit `--insecure-no-auth` escape hatch exists for trusted networks only — not recommended.)

## Features

### Ingestion

- **Save web pages** — Paste a URL, get a clean markdown article with extracted metadata
- **Import emails** — Drag .eml files or bulk import a directory of newsletters
- **Chrome extension** — One-click save from any browser tab (see [Chrome Extension](#chrome-extension) below)
- **Auto-enrichment** — Haiku extracts tags, named entities, and a 2-3 sentence summary on every save

### Intelligence (Opus 4.6)

- **Daily digest** — Three digest variants: ranked by importance, grouped by topic, grouped by entity. Opus finds contradictions between sources, connects threads, and surfaces insights you'd miss. Schedulable for automatic daily generation + email delivery.
- **Ingenuity analysis** — On-demand bias detection, factual confidence scoring, and novelty assessment for any article. Only runs when you ask (saves tokens).
- **Learned preferences** — Rate a few articles, and Opus classifies the rest into must-read / summary-enough / discard tiers based on your demonstrated taste.

### Reading

- **Clean reader** — Distraction-free article view with full markdown rendering
- **Listen to articles** — OpenAI TTS reads articles aloud with streaming playback (starts in ~2s), cached as MP3. Falls back to browser speech synthesis when no OpenAI key is configured.
- **Semantic search** — Find articles by meaning, not just keywords
- **Related articles** — Auto-computed on save with AI-generated connection notes
- **Knowledge graph** — Interactive d3.js force-directed graph showing entities and tags connected by article co-occurrence. Density slider, click-to-explore article panel.
- **Content decay** — Unengaged articles naturally fade from digests over time

### Interface

- **Sidebar navigation** — Persistent left sidebar with Inbox, Digest, Graph, Stats, Settings. Collapses to icons on narrower screens, hamburger menu on mobile.
- **Filter panel** — Right-edge tab opens a slide-out panel with 11 filter facets: AI tier, rating, source, tag, read status, VIP, ingestion method, date range. Active filter pills. URL-synced state.
- **Dark mode** — Toggle between Papyrus (warm cream) and Roman Night (warm charcoal) themes. Persists via localStorage.
- **Theming** — CSS variable-based theme system with 20 `--tiro-*` variables. Roman-inspired palette: terra cotta accent, olive secondary, warm gold for links. Custom theme import support.
- **Pagination** — Configurable page size (25/50/100, or unlimited), server-side offset/limit pagination with keyboard-friendly navigation.

### Productivity

- **Keyboard-first** — Full `j`/`k`/`Enter`/`Esc` navigation, ratings with `1`/`2`/`3`, `f` for filter panel, shortcuts overlay with `?`
- **Gmail integration** — Send digest emails via Gmail SMTP, auto-ingest newsletters via IMAP label monitoring with configurable auto-sync (every 5–60 min)
- **Digest scheduling** — Schedule daily digest generation + email delivery at a set time. Browse previous digests from a history dropdown.
- **Settings page** — Configure email, IMAP sync schedule, TTS, digest schedule, appearance (themes + page size) from the web UI
- **Reading stats** — Charts showing articles saved/read, top topics, source engagement, reading streak
- **Export** — Download your entire library as a portable zip (markdown files + metadata JSON)
- **MCP server** — Query your library from Claude Desktop or Claude Code

---

## Architecture

```
Web UI (localhost:8000 — sidebar nav, dark mode, filter panel, themes)
  ↕ REST API
FastAPI Backend
  ├── Ingestion Engine (readability-lxml + markdownify + IMAP)
  ├── Intelligence Layer (Opus 4.6 — digests, analysis, preferences)
  ├── Lightweight Processing (Haiku — tags, entities, summaries)
  ├── TTS Engine (OpenAI TTS streaming + speechSynthesis fallback)
  ├── Query Layer (ChromaDB semantic search + SQLite metadata)
  ├── Knowledge Graph (d3.js force-directed visualization)
  └── MCP Server (7 tools for Claude integration)
  ↕
Storage Layer (all local)
  ├── articles/*.md      (markdown files with YAML frontmatter)
  ├── audio/*.mp3        (cached TTS audio files)
  ├── tiro.db            (SQLite — metadata, preferences, stats, audio)
  ├── chroma/            (ChromaDB — vector embeddings)
  └── config.yaml
```

**Tech stack:** FastAPI, SQLite, ChromaDB, sentence-transformers, readability-lxml, markdownify, Anthropic API (Opus 4.6 + Haiku 4.5), OpenAI TTS API

---

## CLI Commands

| Command | Description |
|---------|-------------|
| `uv run tiro init` | Initialize library, create databases, prompt for API keys + email setup |
| `uv run tiro run` | Start server at localhost:8000 and open browser |
| `uv run tiro run --lan` | Start server accessible on local network (binds to 0.0.0.0) |
| `uv run tiro run --no-browser` | Start server without opening browser |
| `uv run tiro export -o backup.zip` | Export library as zip (supports `--tag`, `--source-id`, `--rating-min`, `--date-from` filters) |
| `uv run tiro import-emails ./newsletters/` | Bulk import .eml files from a directory |
| `uv run tiro setup-email` | Configure Gmail SMTP + IMAP integration |
| `uv run tiro check-email` | Check IMAP inbox for new newsletters |
| `uv run tiro-mcp` | Start the MCP server (for Claude Desktop/Code integration) |
| `uv run tiro set-password` | Set or reset the Tiro password |
| `uv run tiro token create <name>` | Create an API token for a non-browser client (shown once) |
| `uv run tiro token list` | List existing API tokens |
| `uv run tiro token revoke <id>` | Revoke an API token |
| `uv run tiro doctor [--fix] [--json]` | Check (and optionally repair) consistency across SQLite, ChromaDB, markdown, and audio |
| `uv run tiro audit [--date\|--month] [--service] [--json]` | Show the external-API audit log and cost estimates |
| `uv run tiro status` | Library status and store sizes — works without a running server |
| `uv run tiro delete <id>` | Delete an article by id from all stores |

---

## Chrome Extension

A minimal "Save to Tiro" Chrome extension lives in the `extension/` directory.

### Features

- Shows the current page title and URL before saving
- Detects if the URL is already saved — shows "Already in your library" with a link
- Optional VIP toggle to mark the source as a favorite
- Success confirmation with article title, source, and "Open in Tiro" link
- Error state if the Tiro server isn't running

### Installation

1. Open `chrome://extensions` in Chrome (or any Chromium-based browser)
2. Enable **Developer mode** (toggle in the top-right corner)
3. Click **Load unpacked**
4. Select the `extension/` directory from this repo
5. The Tiro icon (blue circle with white "T") appears in your toolbar

> The Tiro server must be running at `localhost:8000` for the extension to work.

---

## MCP Server — Connect Tiro to Claude

Tiro includes an MCP (Model Context Protocol) server that exposes your reading library to Claude Desktop and Claude Code.

Once you've set a password (see [Security & your data](#security--your-data)), the MCP server needs two extra env vars: `TIRO_API_TOKEN` — create one with `uv run tiro token create mcp` and paste the raw value in, since the server authenticates like any other non-browser client. `TIRO_CONFIG` — an absolute path to your `config.yaml`, because Claude Desktop and Claude Code spawn the MCP server with an arbitrary working directory, so a relative `./config.yaml` usually resolves to the wrong place.

### Available Tools

| Tool | Description |
|------|-------------|
| `search_articles(query, ...)` | Semantic search with optional filters (ai_tier, source_id, tag, rating, date range, etc.) |
| `get_article(article_id)` | Full article content and metadata |
| `get_digest(digest_type)` | Today's daily digest (ranked, by_topic, by_entity) |
| `get_articles_by_tag(tag)` | Articles filtered by topic tag |
| `get_articles_by_source(source)` | Articles filtered by source name or domain |
| `list_filters()` | Available filter facets with counts (tiers, sources, tags, ratings) |
| `save_url(url)` | Save a web page to your library |
| `save_email(file_path)` | Save an .eml newsletter to your library |

### Claude Code

Add to your project's `.mcp.json` (or `~/.claude/settings.json` under `mcpServers`):

```json
{
  "mcpServers": {
    "tiro": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/tiro", "tiro-mcp"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-...",
        "TIRO_API_TOKEN": "<from tiro token create mcp>",
        "TIRO_CONFIG": "/absolute/path/to/config.yaml"
      }
    }
  }
}
```

### Claude Desktop

Add to your Claude Desktop config file:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "tiro": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/tiro", "tiro-mcp"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-...",
        "TIRO_API_TOKEN": "<from tiro token create mcp>",
        "TIRO_CONFIG": "/absolute/path/to/config.yaml"
      }
    }
  }
}
```

Replace `/path/to/tiro` with the actual path to your clone, add your Anthropic API key, and fill in `TIRO_API_TOKEN`/`TIRO_CONFIG` as described above.

---

## Export

Export your entire library (or a filtered subset) as a portable zip bundle:

```bash
uv run tiro export --output my-library.zip
uv run tiro export --output ai-articles.zip --tag ai
uv run tiro export --output favorites.zip --rating-min 1
```

The zip contains:
- `articles/` — All markdown files with YAML frontmatter intact
- `metadata.json` — Full structured data (articles, sources, tags, entities, relations)
- `README.md` — Bundle format documentation

Also available via the API (`GET /api/export`) and the Export button on the Stats page.

---

## Keyboard Shortcuts

### Inbox

| Key | Action |
|-----|--------|
| `j` / `k` | Move down / up through articles |
| `Enter` | Open selected article |
| `s` | Toggle VIP on selected article's source |
| `1` / `2` / `3` | Rate: dislike / like / love |
| `x` | Delete selected article (with confirmation) |
| `/` | Focus search bar |
| `f` | Toggle filter panel |
| `d` | Go to digest |
| `a` | Switch to articles view |
| `c` | Classify / reclassify inbox |
| `g` | Go to stats |
| `v` | Go to knowledge graph |
| `?` | Show shortcuts overlay |

### Reader

| Key | Action |
|-----|--------|
| `b` / `Esc` | Back to inbox |
| `s` | Toggle VIP |
| `1` / `2` / `3` | Rate: dislike / like / love |
| `x` | Delete current article (with confirmation) |
| `p` | Play / pause audio |
| `i` | Toggle analysis panel |
| `r` | Run / re-run analysis (when panel open) |
| `d` | Go to digest |
| `g` | Go to stats |
| `v` | Go to knowledge graph |
| `?` | Show shortcuts overlay |

### Stats

| Key | Action |
|-----|--------|
| `b` / `Esc` | Back to inbox |
| `e` | Export library |
| `v` | Go to knowledge graph |
| `?` | Show shortcuts overlay |

### Graph

| Key | Action |
|-----|--------|
| `b` / `Esc` | Back to inbox |
| `?` | Show shortcuts overlay |

### Settings

| Key | Action |
|-----|--------|
| `b` / `Esc` | Back to inbox |
| `v` | Go to knowledge graph |
| `?` | Show shortcuts overlay |

---

## Project Structure

```
tiro/
├── tiro/                       # Python package
│   ├── app.py                  # FastAPI app, router registration
│   ├── cli.py                  # CLI commands (init, run, export, import-emails, setup-email, check-email)
│   ├── config.py               # Config loading (dataclass + YAML)
│   ├── database.py             # SQLite schema and helpers
│   ├── vectorstore.py          # ChromaDB initialization
│   ├── auth.py                 # Password auth, sessions, API tokens
│   ├── sanitize.py             # Server-side HTML sanitization (nh3)
│   ├── lifecycle.py            # Article delete + ingestion rollback (all four stores)
│   ├── doctor.py               # Cross-store consistency check + repair
│   ├── audit.py                # External-API audit log (JSONL) + cost estimates
│   ├── decay.py                # Content decay system
│   ├── stats.py                # Reading stats tracking
│   ├── export.py               # Library export (zip generation)
│   ├── tts.py                  # OpenAI TTS streaming + caching
│   ├── api/                    # FastAPI route handlers
│   ├── ingestion/              # Web + email content extraction
│   ├── intelligence/           # Opus 4.6 features (digest, analysis, preferences)
│   ├── search/                 # Semantic search + related articles
│   ├── mcp/                    # MCP server for Claude integration
│   └── frontend/               # HTML templates, CSS, JS, themes
├── extension/                  # Chrome extension
├── scripts/                    # Utility scripts
├── pyproject.toml              # Package config
└── tiro-library/               # Default data directory (gitignored)
```

---

## Testing

`uv run pytest` runs the Python test suite (`tests/`). A small end-to-end browser spec also lives at `playwright-tests/phase0.spec.js` (Playwright), covering the first-run setup flow, login, saving an article, and deleting it — see `playwright-tests/README.md` for how to run it.

---

## License

[GNU Affero General Public License v3.0 or later](LICENSE) (AGPL-3.0-or-later).

Tiro is free software you can run, study, modify, and redistribute. If you modify Tiro and offer it as a network service to others, AGPL requires you to make your modified source available to those users. Running Tiro on your own laptop or home server for your own use carries no such obligation.

> *Tiro was previously distributed under the MIT License. Existing contributions made before 2026-05-28 remain under their original MIT terms; subsequent contributions are AGPL-3.0-or-later.*

---

<p align="center"><em>"...without you the oracle was dumb."</em><br><small>— Cicero to Tiro, 53 BC</small></p>
