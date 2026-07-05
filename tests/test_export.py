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
