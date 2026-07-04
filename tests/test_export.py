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
