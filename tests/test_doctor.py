"""M5: tiro doctor — four-store consistency scan and repair."""

from pathlib import Path

import pytest

from tiro.database import get_connection
from tiro.vectorstore import get_collection

FIXTURE = Path(__file__).parent / "fixtures" / "newsletter.eml"


def _make_article(config, title="Doctor Test"):
    """Full article across all stores (offline: no AI metadata)."""
    from tiro.ingestion.email import parse_eml
    from tiro.ingestion.processor import process_article

    ex = parse_eml(FIXTURE.read_bytes())
    ex["title"] = title
    result = process_article(**ex, config=config, ingestion_method="email")
    return result["id"], result["markdown_path"]


def test_scan_clean_library(initialized_library):
    from tiro.doctor import scan

    _make_article(initialized_library)
    report = scan(initialized_library)
    assert report["clean"] is True
    assert report["orphaned_markdown"] == []
    assert report["missing_markdown"] == []
    assert report["orphaned_vectors"] == []
    assert report["vector_missing"] == []


def test_scan_detects_all_classes(initialized_library):
    from tiro.doctor import scan

    config = initialized_library
    aid, md_path = _make_article(config, "Victim A")
    bid, _ = _make_article(config, "Victim B")

    # (a) stray markdown file with no row
    (config.articles_dir / "stray-orphan.md").write_text("---\ntitle: x\n---\nbody")
    # (b) row whose markdown file is gone
    (config.articles_dir / md_path).unlink()
    # (c) vector with no row
    get_collection().upsert(ids=["article_999999"], documents=["orphan vector"])
    # (d) status says indexed but vector missing
    get_collection().delete(ids=[f"article_{bid}"])
    # (e) audio row without file + audio file without row
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            "INSERT INTO audio (article_id, file_path, voice, model, generated_at) "
            "VALUES (?, ?, 'nova', 'tts-1', '2026-01-01')",
            (bid, f"{bid}.mp3"),
        )
        conn.execute(
            "INSERT INTO sessions (token_hash, expires_at) "
            "VALUES ('deadbeef', datetime('now', '-1 day'))"
        )
        conn.execute("INSERT INTO tags (name) VALUES ('never-used')")
        conn.commit()
    finally:
        conn.close()
    (config.library / "audio" / "424242.mp3").write_bytes(b"mp3")

    report = scan(config)
    assert report["clean"] is False
    assert "stray-orphan.md" in report["orphaned_markdown"]
    assert any(r["id"] == aid for r in report["missing_markdown"])
    assert "article_999999" in report["orphaned_vectors"]
    assert bid in report["vector_missing"]
    assert bid in report["audio_rows_missing_file"]
    assert "424242.mp3" in report["audio_files_without_row"]
    assert report["expired_sessions"] == 1
    assert report["unreferenced_tags"] >= 1
