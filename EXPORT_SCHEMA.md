# Tiro Export Bundle Schema

This document describes the zip bundle produced by `tiro export`, `GET /api/export`,
and the Stats page Export button (`tiro/export.py`). It is for anyone writing a
third-party importer against a Tiro export.

## Bundle layout

```
tiro-export-<random>.zip
├── articles/
│   └── <slug>.md        # one file per exported article; YAML frontmatter + markdown body
├── wiki/                 # optional — only present if the library has synthesis pages (Phase 1b)
│   └── <name>.md
├── annotations/           # optional — one <stem>.jsonl per exported article that has highlights (Phase 2 M2.1)
│   └── <stem>.jsonl
├── notes/                 # optional — one <stem>.md per exported article that has an article-level note (Phase 2 M2.1)
│   └── <stem>.md
├── metadata.json          # full structured data — see below
├── sources.opml           # OPML 2.0 listing of sources
└── README.md              # bundle-local copy of this format's basics
```

`articles/` and `wiki/` filenames are not otherwise referenced by `metadata.json`
except via `articles[*].markdown_path` (bare filename, matches the `articles/`
entry) — there is no manifest cross-linking `wiki/` pages. `annotations/<stem>.jsonl`
and `notes/<stem>.md` ARE cross-linked: `<stem>` is the same stem as the matching
`articles/<stem>.md` file (i.e. `Path(articles[*].markdown_path).stem`) — this is
how highlights/notes are matched back to their owning article, both in the
sidecar files themselves and via `highlights[*].article_id`/`notes[*].article_id`
in `metadata.json`. Unlike `wiki/`, `annotations/`/`notes/` ARE scoped to the
export's article filters: a filtered export includes only the sidecars of the
articles it actually exported, never a sidecar for an excluded article.

**`tiro import` (`tiro/importer.py`) does not import `wiki/`.** A bundle
carries `wiki/` when the source library has synthesis pages, but `import_bundle`
only reverses `articles`/`sources`/`tags`/`entities`/junctions — the same way
it skips `digests`/`reading_stats`/`audio`/`relations` (regenerable caches or
this-library activity). Wiki page merge across libraries is out of scope for
Phase 1b wave W1. Snapshots/restore (`tiro/backup.py`), which replace the
whole library rather than merging into an existing one, DO round-trip
`wiki/` faithfully since they copy the directory wholesale.

**`tiro import` DOES import `annotations/`/`notes/`** (Phase 2 M2.1 Task 4):
for each bundle article, its highlight/note sidecar lines are merged into the
matching local article's own sidecars, keyed by each highlight's `uid` —
`skip` keeps every existing local line untouched and only adds bundle lines
whose uid isn't already present locally; `overwrite` replaces both of the
local article's sidecar files wholesale with the bundle's; `keep-both` copies
the bundle's lines under the newly-created duplicate article's own (fresh)
stem, minting fresh uids so they can never collide with the original's. A
bundle article with neither an `annotations/<stem>.jsonl` file nor a
`notes/<stem>.md` file is a no-op for that article (nothing to merge). Older
bundles that predate Task 4 have neither the directories nor the
`highlights`/`notes` metadata keys — importing them is unaffected. After
every article in the run has been processed, `import_bundle` runs
`reconcile_annotations()` once to rebuild the derived `highlights`/`notes`
SQLite rows from whatever sidecar files ended up on disk (files-win, same as
every other sidecar write path in the codebase).

## metadata.json keys

Top-level: `exported_at` (ISO 8601 timestamp), `tiro_version` (string), `filters`
(the tag/source_id/rating_min/date_from filters used, any may be `null`), plus
the twelve data keys below. Each is a JSON array of row objects; column names match
the row's SQL source verbatim (snake_case).

| Key | Row shape | Notes |
|---|---|---|
| `articles` | `id, uid, source_id, title, author, url, slug, markdown_path, summary, word_count, reading_time_min, published_at, ingested_at, is_read, rating, opened_count, ai_tier, relevance_weight, ingenuity_analysis, ingestion_method, vector_status, snoozed_until, display_date, source_name, source_type, source_is_vip` | `uid` is a TEXT ULID (e.g. `01AAAAAAAAAAAAAAAAAAAAAAAA`), unique, the stable identity for the row. `ingenuity_analysis` is a JSON string or `null`, not parsed. `snoozed_until` is a naive-UTC timestamp string or `null` — ephemeral inbox-triage state (Phase 2), not content; `tiro/importer.py` does NOT round-trip it (a bundle-imported article always lands with `snoozed_until = NULL`, regardless of what the source library had), so re-importing a bundle never resurrects a stale snooze. `display_date` is `published_at` coalesced with `ingested_at`. `source_*` columns are joined in from `sources`, not native article columns. Scoped to the export's filters. |
| `sources` | `id, name, domain, email_sender, source_type, is_vip, created_at` | All sources, unfiltered (referenced sources for filtered articles are a subset of this). |
| `tags` | `id, uid, name` | Only tags referenced by an exported article. `uid` is a TEXT ULID. |
| `entities` | `id, uid, name, entity_type, canonical_key` | Only entities referenced by an exported article. `uid` is a TEXT ULID. `canonical_key` (TEXT) is the normalized dedup key entities are merged on (`entity_type` + `canonical_key` is unique) — use it, not `name`, to detect "same entity" across casing/whitespace variants. |
| `relations` | `article_id, related_article_id, similarity_score, connection_note` | Only rows where both `article_id` and `related_article_id` are in the exported set. `similarity_score` is a float 0–1; `connection_note` may be `null`. |
| `article_tags` | `article_id, tag_id` | Junction table, foreign keys into `articles`/`tags` by numeric `id` (not `uid`) — see Identity section. |
| `article_entities` | `article_id, entity_id` | Junction table, same `id`-based caveat as `article_tags`. |
| `digests` | `date, digest_type, content, article_ids, created_at` | **Whole-library**, not scoped to the export's article filters — a digest can reference articles outside the current filter. `digest_type` is one of `ranked`/`by_topic`/`by_entity`. `article_ids` is a JSON-encoded string of numeric IDs, not parsed here. |
| `reading_stats` | `date, articles_saved, articles_read, articles_rated, total_reading_time_min` | **Whole-library**, one row per calendar date; not article-filtered (daily aggregates aren't per-article). |
| `audio` | `article_id, voice, model, duration_seconds, file_size_bytes, generated_at` | Scoped to the exported articles. Deliberately omits the internal `file_path` column — cached MP3s are not included in the bundle. |
| `highlights` | `id, uid, article_id, article_uid, quote_text, prefix_context, suffix_context, text_position_start, text_position_end, content_hash, color, created_at, updated_at` | Scoped to the exported articles. `article_uid` is joined in for cross-database re-keying (same rationale as `source_*` on `articles`). This is the SQLite-derived-row shape (`_get_highlights`), a fallback for importers that don't read the `annotations/<stem>.jsonl` sidecar files directly — the sidecar files are the actual source of truth. |
| `notes` | `id, uid, article_id, article_uid, highlight_id, body_markdown, created_at, updated_at` | Scoped to the exported articles; covers BOTH note kinds. `highlight_id` set = a highlight-anchored note (inlined in that highlight's sidecar line as `note_markdown`, not a separate file); `highlight_id: null` = the one article-level note (`notes/<stem>.md`). Same sidecar-files-are-truth caveat as `highlights` above. |

## Markdown frontmatter fields

Each file under `articles/` has YAML frontmatter:

```yaml
---
title: "Article Title"
author: "Author Name"       # may be null
source: "source.com"        # sources.name at ingest time, not source_id
url: "https://..."          # empty string for email-derived articles
published: 2026-02-10       # date only (YYYY-MM-DD)
ingested: 2026-02-11T14:30:00
tags: ["ai", "technology"]
entities: ["Company A", "Person B"]
word_count: 2450
reading_time: 10 min
summary: "..."               # present only once Haiku enrichment has completed
---
```

Frontmatter does **not** carry `uid` — the file's identity link to its
`metadata.json` row is the filename itself, matched against
`articles[*].markdown_path`.

## sources.opml semantics

Standard OPML 2.0, one `<outline>` per source with `text`/`title` set to
`sources.name`. `htmlUrl` is present (`https://{domain}`) only for web sources
with a `domain`; email sources have no `htmlUrl` and no other URL attribute.
This is forward-looking for Phase 4 RSS support — no `xmlUrl`/feed URL exists
in the schema yet, so treat OPML here as a source-name/site directory, not a
subscribable feed list.

## Identity & conflict semantics

`uid` (a ULID, assigned once at row creation and never reused) is the stable
identity for `articles`, `tags`, and `entities` — prefer it for matching
records across two exports of the same library, or when importing into an
existing library. Junction tables (`article_tags`, `article_entities`,
`relations`) reference rows by the numeric SQLite `id`, which is **not**
stable across databases; resolve `id` → `uid` via the corresponding array
before using a junction row against a different database.

When reconciling an export against an external or pre-existing library where
`uid` doesn't (yet) match — e.g. a bundle exported before an article had a
`uid` backfilled, or content from a different Tiro instance — importers
should fall back in this order: (1) exact `uid` match, (2) exact `url` match
(web articles only — `url` is empty for email-derived articles and unreliable
for matching), (3) `title` + source name (the `source` frontmatter field /
`articles[*].source_name`) as a last resort. Treat entities as the same
real-world entity when `entity_type` + `canonical_key` match, even if `name`
differs.

## Versioning

`tiro_version` (top-level `metadata.json` field) records the exact Tiro
release that produced the bundle. This document describes bundle format
1.x: within 1.x, changes are additive only — new keys or row fields may
appear in later Tiro versions, but existing keys are never removed or
repurposed, and importers should ignore unrecognized keys/fields rather than
fail on them. A breaking, non-additive change would be called out here as a
2.x bundle format.
