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


def test_export_schema_doc_lists_all_metadata_keys():
    from pathlib import Path

    doc = (Path(__file__).parent.parent / "EXPORT_SCHEMA.md").read_text()
    for key in ("digests", "reading_stats", "audio", "sources.opml", "uid"):
        assert key in doc, key
