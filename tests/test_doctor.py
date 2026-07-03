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


def test_fix_repairs_everything(initialized_library):
    from tiro.doctor import fix, scan

    config = initialized_library
    aid, md_path = _make_article(config, "Victim A")
    bid, _ = _make_article(config, "Victim B")
    cid, _ = _make_article(config, "Victim C")

    (config.articles_dir / "stray-orphan.md").write_text("---\ntitle: x\n---\nbody")
    (config.articles_dir / md_path).unlink()
    get_collection().upsert(ids=["article_999999"], documents=["orphan vector"])
    get_collection().delete(ids=[f"article_{bid}"])
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
        # vector_unmarked: Victim C's vector genuinely exists (ingested normally),
        # but its status is forced back to 'pending' to simulate drift.
        conn.execute(
            "UPDATE articles SET vector_status = 'pending' WHERE id = ?", (cid,)
        )
        # unreferenced entity: no article_entities row points at it
        conn.execute(
            "INSERT INTO entities (name, entity_type) VALUES ('Nobody Inc', 'company')"
        )
        conn.commit()
    finally:
        conn.close()
    (config.library / "audio" / "424242.mp3").write_bytes(b"mp3")

    # Sanity: Victim C's vector really is present before fix() touches anything.
    assert get_collection().get(ids=[f"article_{cid}"])["ids"] == [f"article_{cid}"]

    result = fix(config)
    assert result["actions"], "fix must report what it did"

    # Post-fix scan is clean
    report = scan(config)
    assert report["clean"] is True, report

    # Orphaned markdown preserved, not deleted
    assert (config.library / ".orphaned" / "stray-orphan.md").exists()
    # Interrupted delete completed: row gone
    conn = get_connection(config.db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM articles WHERE id = ?", (aid,)
        ).fetchone()["n"] == 0
        # Victim C's status corrected back to 'indexed' without re-embedding
        assert conn.execute(
            "SELECT vector_status FROM articles WHERE id = ?", (cid,)
        ).fetchone()["vector_status"] == "indexed"
    finally:
        conn.close()
    # Victim B re-embedded
    assert get_collection().get(ids=[f"article_{bid}"])["ids"] == [f"article_{bid}"]
    # Victim C was flipped, not re-embedded: fix() should only report
    # re-embedding the one article that actually needed it (Victim B).
    reembed_actions = [a for a in result["actions"] if "re-embedded" in a]
    assert reembed_actions == ["re-embedded 1 article(s)"], result["actions"]


def test_cli_doctor_json(initialized_library, capsys, monkeypatch):
    import json

    from tiro import cli

    _make_article(initialized_library, "CLI Doctor")
    monkeypatch.setattr(
        "sys.argv",
        ["tiro", "--config", str(initialized_library.config_path or "unused"),
         "doctor", "--json"],
    )
    # cmd_doctor must not re-init an already-initialized vectorstore in-process;
    # call it directly with a prepared config instead of via main() if simpler —
    # the contract under test is the JSON shape and exit code.
    from types import SimpleNamespace

    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor(SimpleNamespace(config="unused", fix=False, json=True,
                                       _config_override=initialized_library))
    assert exc.value.code == 0
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["clean"] is True


def test_cli_doctor_fix_json_reflects_post_fix_state(initialized_library, capsys):
    """Review finding 1: `tiro doctor --fix --json` must print the POST-fix
    state (clean, empty issue lists), not the pre-fix scan re-labeled with
    'actions' tacked on. The exit code and the printed JSON must agree."""
    import json
    from types import SimpleNamespace

    from tiro import cli

    config = initialized_library
    aid, md_path = _make_article(config, "Fixable Victim")
    # Plant a single discrepancy: a stray markdown file with no DB row.
    (config.articles_dir / "stray-orphan.md").write_text("---\ntitle: x\n---\nbody")

    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor(SimpleNamespace(config="unused", fix=True, json=True,
                                       _config_override=config))
    assert exc.value.code == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["clean"] is True, parsed
    assert parsed["actions"], "actions must still be reported"
    assert parsed["orphaned_markdown"] == []


def test_fix_does_not_clobber_existing_orphaned_file(initialized_library):
    """Review finding 2: if `.orphaned/<name>` already exists (e.g. from a
    prior repair run), fix() must not overwrite it via rename() — both the
    previously preserved file and the newly discovered stray must survive,
    under distinct names."""
    from tiro.doctor import fix

    config = initialized_library
    orphan_dir = config.library / ".orphaned"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    sentinel = "SENTINEL: previously preserved orphan, must not be destroyed"
    (orphan_dir / "stray-orphan.md").write_text(sentinel)

    new_content = "---\ntitle: new stray\n---\nbody"
    (config.articles_dir / "stray-orphan.md").write_text(new_content)

    result = fix(config)
    assert result["actions"]

    # Original preserved orphan untouched.
    assert (orphan_dir / "stray-orphan.md").read_text() == sentinel
    # New stray preserved under a disambiguated name.
    disambiguated = orphan_dir / "stray-orphan.1.md"
    assert disambiguated.exists()
    assert disambiguated.read_text() == new_content
