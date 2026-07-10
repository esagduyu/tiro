"""Library export: zip bundle generation."""

import zipfile

from tiro.export import export_library


def test_export_includes_wiki_pages_when_present(test_config):
    """wiki/*.md pages (Phase 1b: LLM-maintained synthesis pages) are part of
    the user's library and must ride along in the export bundle."""
    from tiro.database import init_db, migrate_db

    test_config.articles_dir.mkdir(parents=True, exist_ok=True)
    init_db(test_config.db_path)
    migrate_db(test_config.db_path)

    test_config.wiki_dir.mkdir(parents=True, exist_ok=True)
    (test_config.wiki_dir / "overview.md").write_text("# Overview\n\nSynthesis page.\n")

    zip_path = export_library(test_config)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            assert "wiki/overview.md" in names
    finally:
        zip_path.unlink()


def test_export_includes_wiki_subdir_pages_with_preserved_arcname(test_config):
    """Wiki pages live under kind subdirectories (wiki/entities/*.md,
    wiki/concepts/*.md) -- export must recurse (rglob, not glob) and keep
    the subpath in the zip arcname, not flatten everything to wiki/*.md."""
    from tiro.database import init_db, migrate_db

    test_config.articles_dir.mkdir(parents=True, exist_ok=True)
    init_db(test_config.db_path)
    migrate_db(test_config.db_path)

    entities_dir = test_config.wiki_dir / "entities"
    entities_dir.mkdir(parents=True, exist_ok=True)
    (entities_dir / "anthropic.md").write_text("# Anthropic\n\nSynthesis page.\n")
    # Bookkeeping files at the wiki root must also ride along.
    (test_config.wiki_dir / "_schema.md").write_text("schema")
    (test_config.wiki_dir / "index.md").write_text("index")
    (test_config.wiki_dir / "log.md").write_text("log")

    zip_path = export_library(test_config)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            assert "wiki/entities/anthropic.md" in names
            assert "wiki/_schema.md" in names
            assert "wiki/index.md" in names
            assert "wiki/log.md" in names
    finally:
        zip_path.unlink()


def test_export_omits_wiki_dir_when_absent(test_config):
    """No wiki/ directory (pre-Phase-1b library) — export must not fail or
    fabricate an empty wiki/ entry."""
    from tiro.database import init_db, migrate_db

    test_config.articles_dir.mkdir(parents=True, exist_ok=True)
    init_db(test_config.db_path)
    migrate_db(test_config.db_path)

    zip_path = export_library(test_config)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            assert not any(n.startswith("wiki/") for n in names)
    finally:
        zip_path.unlink()


def test_export_includes_digests_stats_audio(initialized_library):
    import json
    import zipfile

    from tiro.database import get_connection
    from tiro.export import export_library

    config = initialized_library
    conn = get_connection(config.db_path)
    conn.execute("INSERT INTO sources (name, source_type) VALUES ('S', 'web')")
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path, ingenuity_analysis)"
        " VALUES ('01AAAAAAAAAAAAAAAAAAAAAAAA', 1, 'T', 'sl', 'sl.md', '{\"score\": 7}')"
    )
    conn.execute(
        "INSERT INTO digests (date, digest_type, content, article_ids)"
        " VALUES ('2026-07-01', 'ranked', '## D', '[1]')"
    )
    conn.execute(
        "INSERT INTO reading_stats (date, articles_saved) VALUES ('2026-07-01', 3)"
    )
    conn.execute(
        "INSERT INTO audio (article_id, file_path, voice, model, generated_at)"
        " VALUES (1, '1.mp3', 'nova', 'tts-1', '2026-07-01')"
    )
    conn.commit()
    conn.close()
    (config.articles_dir / "sl.md").write_text("---\ntitle: T\n---\nbody")

    zip_path = export_library(config)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            meta = json.loads(zf.read("metadata.json"))
        assert meta["digests"][0]["digest_type"] == "ranked"
        assert meta["reading_stats"][0]["articles_saved"] == 3
        assert meta["audio"][0]["voice"] == "nova"
        assert "file_path" not in meta["audio"][0]
        assert meta["articles"][0]["ingenuity_analysis"] == '{"score": 7}'
        assert meta["articles"][0]["uid"] == "01AAAAAAAAAAAAAAAAAAAAAAAA"
    finally:
        zip_path.unlink()


def test_opml_export(initialized_library):
    import xml.etree.ElementTree as ET

    from tiro.database import get_connection
    from tiro.export import export_opml

    config = initialized_library
    conn = get_connection(config.db_path)
    conn.execute("INSERT INTO sources (name, domain, source_type) VALUES ('Blog & Co', 'blog.example.com', 'web')")
    conn.execute("INSERT INTO sources (name, email_sender, source_type) VALUES ('Letter', 'l@x.com', 'email')")
    conn.commit()
    conn.close()

    xml_text = export_opml(config)
    root = ET.fromstring(xml_text)  # well-formed (— '&' must be escaped)
    outlines = root.findall(".//outline")
    by_text = {o.get("text"): o for o in outlines}
    assert by_text["Blog & Co"].get("htmlUrl") == "https://blog.example.com"
    assert "Letter" in by_text
    assert by_text["Letter"].get("htmlUrl") is None


def test_opml_export_included_in_zip(initialized_library):
    import zipfile

    from tiro.database import get_connection
    from tiro.export import export_library

    config = initialized_library
    conn = get_connection(config.db_path)
    conn.execute("INSERT INTO sources (name, domain, source_type) VALUES ('S', 's.example.com', 'web')")
    conn.commit()
    conn.close()

    zip_path = export_library(config)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            assert "sources.opml" in names
            content = zf.read("sources.opml").decode("utf-8")
            assert "s.example.com" in content
    finally:
        zip_path.unlink()


def test_opml_endpoint(authenticated_client):
    resp = authenticated_client.get("/api/export/opml")
    assert resp.status_code == 200
    assert "opml" in resp.headers["content-type"]


def _seed_feed(config, *, url, title="Feed", folder=None, site_url="https://blog.example.com"):
    """Insert a feed + its backing rss source, returning (source_id, feed_id)."""
    from tiro.database import get_connection
    from tiro.migrations import new_ulid

    conn = get_connection(config.db_path)
    conn.execute(
        "INSERT INTO sources (name, domain, source_type) VALUES (?, ?, 'rss')",
        (title, "blog.example.com"),
    )
    source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute(
        "INSERT INTO feeds (uid, url, title, site_url, folder, source_id, "
        "fetch_interval_minutes, status, last_etag, error_count) "
        "VALUES (?, ?, ?, ?, ?, ?, 30, 'active', 'W/\"etag\"', 4)",
        (new_ulid(), url, title, site_url, folder, source_id),
    )
    feed_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.commit()
    conn.close()
    return source_id, feed_id


def test_export_metadata_has_feeds_key_without_transient_state(initialized_library):
    import json
    import zipfile

    from tiro.export import export_library

    config = initialized_library
    _seed_feed(config, url="https://blog.example.com/feed.xml", title="Blog", folder="Tech")

    zip_path = export_library(config)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            meta = json.loads(zf.read("metadata.json"))
        assert "feeds" in meta
        row = meta["feeds"][0]
        assert row["url"] == "https://blog.example.com/feed.xml"
        assert row["title"] == "Blog"
        assert row["folder"] == "Tech"
        assert row["fetch_interval_minutes"] == 30
        assert row["status"] == "active"
        # spec D5: transient fetch state + feed_entries excluded.
        for excluded in ("last_etag", "last_modified", "error_count", "last_error", "last_fetched_at"):
            assert excluded not in row
        assert "feed_entries" not in meta
    finally:
        zip_path.unlink()


def test_opml_export_marks_feed_backed_source_with_xmlurl(initialized_library):
    import xml.etree.ElementTree as ET

    from tiro.export import export_opml

    config = initialized_library
    _seed_feed(config, url="https://blog.example.com/feed.xml", title="RSS Blog")

    root = ET.fromstring(export_opml(config))
    by_text = {o.get("text"): o for o in root.findall(".//outline")}
    node = by_text["RSS Blog"]
    assert node.get("type") == "rss"
    assert node.get("xmlUrl") == "https://blog.example.com/feed.xml"
    assert node.get("htmlUrl") == "https://blog.example.com"


def test_export_schema_doc_lists_all_metadata_keys():
    from pathlib import Path

    doc = (Path(__file__).parent.parent / "EXPORT_SCHEMA.md").read_text()
    for key in ("digests", "reading_stats", "audio", "sources.opml", "uid", "highlights", "notes", "feeds"):
        assert key in doc, key


# --- Highlights + notes sidecars (Phase 2 M2.1 Task 4) -----------------------


def _seed_article_with_annotations(config, *, stem="art-1", title="T1"):
    """Seed an article + one highlight (with an anchored note) + one
    article-level note, both as SQLite rows AND as sidecar files (mirroring
    what routes_annotations.py's sidecar-first writes produce)."""
    from tiro.annotations import write_annotations, write_note
    from tiro.database import get_connection
    from tiro.migrations import new_ulid

    conn = get_connection(config.db_path)
    conn.execute("INSERT OR IGNORE INTO sources (name, source_type) VALUES ('S', 'web')")
    article_uid = new_ulid()
    conn.execute(
        "INSERT INTO articles (uid, source_id, title, slug, markdown_path)"
        " VALUES (?, 1, ?, ?, ?)",
        (article_uid, title, stem, f"{stem}.md"),
    )
    article_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    h_uid = new_ulid()
    conn.execute(
        """INSERT INTO highlights
           (uid, article_id, quote_text, prefix_context, suffix_context,
            text_position_start, text_position_end, content_hash, color,
            created_at, updated_at)
           VALUES (?, ?, 'quote', 'pre', 'suf', 0, 5, 'hash', 'yellow',
                   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
        (h_uid, article_id),
    )
    highlight_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute(
        """INSERT INTO notes (uid, article_id, highlight_id, body_markdown, created_at, updated_at)
           VALUES (?, ?, ?, 'hl note', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
        (new_ulid(), article_id, highlight_id),
    )
    conn.execute(
        """INSERT INTO notes (uid, article_id, highlight_id, body_markdown, created_at, updated_at)
           VALUES (?, ?, NULL, 'article note', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
        (new_ulid(), article_id),
    )
    conn.commit()
    conn.close()
    (config.articles_dir / f"{stem}.md").write_text(f"---\ntitle: {title}\n---\nbody")

    write_annotations(
        config, stem,
        [{
            "uid": h_uid, "article_uid": article_uid, "quote": "quote",
            "prefix": "pre", "suffix": "suf", "position_start": 0, "position_end": 5,
            "content_hash": "hash", "color": "yellow", "note_markdown": "hl note",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        }],
    )
    write_note(config, stem, "article note")
    return article_id, article_uid


def test_export_includes_annotation_sidecars_for_exported_articles(initialized_library):
    import zipfile

    config = initialized_library
    _seed_article_with_annotations(config)

    zip_path = export_library(config)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            assert "annotations/art-1.jsonl" in names
            assert "notes/art-1.md" in names
    finally:
        zip_path.unlink()


def test_export_metadata_has_highlights_and_notes(initialized_library):
    import json
    import zipfile

    config = initialized_library
    _, article_uid = _seed_article_with_annotations(config)

    zip_path = export_library(config)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            meta = json.loads(zf.read("metadata.json"))
        assert len(meta["highlights"]) == 1
        assert meta["highlights"][0]["article_uid"] == article_uid
        assert meta["highlights"][0]["quote_text"] == "quote"
        assert len(meta["notes"]) == 2
        bodies = {n["body_markdown"] for n in meta["notes"]}
        assert bodies == {"hl note", "article note"}
    finally:
        zip_path.unlink()


def test_filtered_export_only_includes_matching_articles_sidecars(initialized_library):
    """A filtered export (e.g. by source_id) must include ONLY the sidecars
    of the articles that pass the filter, not every sidecar in the library."""
    import zipfile

    from tiro.database import get_connection

    config = initialized_library
    _seed_article_with_annotations(config, stem="art-1", title="T1")
    _seed_article_with_annotations(config, stem="art-2", title="T2")

    conn = get_connection(config.db_path)
    conn.execute("INSERT OR IGNORE INTO sources (name, source_type) VALUES ('Other', 'web')")
    other_source_id = conn.execute(
        "SELECT id FROM sources WHERE name = 'Other'"
    ).fetchone()["id"]
    conn.execute("UPDATE articles SET source_id = ? WHERE slug = 'art-2'", (other_source_id,))
    conn.commit()
    conn.close()

    zip_path = export_library(config, source_id=other_source_id)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            assert "annotations/art-2.jsonl" in names
            assert "notes/art-2.md" in names
            assert "annotations/art-1.jsonl" not in names
            assert "notes/art-1.md" not in names
            import json

            meta = json.loads(zf.read("metadata.json"))
            assert {h["article_id"] for h in meta["highlights"]} == {
                a["id"] for a in meta["articles"]
            }
    finally:
        zip_path.unlink()


def test_export_omits_annotation_dirs_when_absent(test_config):
    import zipfile

    from tiro.database import init_db, migrate_db

    test_config.articles_dir.mkdir(parents=True, exist_ok=True)
    init_db(test_config.db_path)
    migrate_db(test_config.db_path)

    zip_path = export_library(test_config)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            assert not any(n.startswith("annotations/") for n in names)
            assert not any(n.startswith("notes/") for n in names)
    finally:
        zip_path.unlink()
