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
        # unreferenced author: no article_authors row points at it
        conn.execute(
            "INSERT INTO authors (uid, name, canonical_key)"
            " VALUES ('01AUTHORORPHAN00000000000', 'Nobody Author', 'nobody author')"
        )
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
    assert report["unreferenced_authors"] >= 1


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
        # unreferenced author: no article_authors row points at it
        conn.execute(
            "INSERT INTO authors (uid, name, canonical_key)"
            " VALUES ('01AUTHORORPHAN00000000000', 'Nobody Author', 'nobody author')"
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


def test_cli_doctor_text_shows_vector_failed(initialized_library, capsys, monkeypatch):
    """Review finding: cmd_doctor's plain-text output iterates a hardcoded
    key tuple that omitted 'vector_failed'. `tiro doctor` (no --json) on a
    library with failed-no-vector residue must still surface which/how many
    articles are in that class, not just exit 1 silently on this class."""
    from types import SimpleNamespace

    from tiro import cli

    aid, _ = _make_article(initialized_library, "Failed Residue CLI")
    get_collection().delete(ids=[f"article_{aid}"])
    conn = get_connection(initialized_library.db_path)
    try:
        conn.execute("UPDATE articles SET vector_status = 'failed' WHERE id = ?", (aid,))
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        "sys.argv",
        ["tiro", "--config", str(initialized_library.config_path or "unused"),
         "doctor"],
    )

    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor(SimpleNamespace(config="unused", fix=False, json=False,
                                       _config_override=initialized_library))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "vector_failed" in out
    assert str(aid) in out


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


def test_expired_sessions_alone_are_housekeeping_not_failure(initialized_library):
    from tiro.doctor import scan

    _make_article(initialized_library, "Healthy")
    conn = get_connection(initialized_library.db_path)
    try:
        conn.execute("INSERT INTO sessions (token_hash, expires_at) "
                     "VALUES ('old', datetime('now', '-1 day'))")
        conn.commit()
    finally:
        conn.close()
    report = scan(initialized_library)
    assert report["structurally_consistent"] is True
    assert report["clean"] is False  # something exists to clean up
    assert report["expired_sessions"] == 1


def test_fix_surfaces_reembed_failures(initialized_library, monkeypatch):
    from tiro import doctor as doctor_mod
    from tiro.doctor import fix

    aid, _ = _make_article(initialized_library, "Unembeddable")
    get_collection().delete(ids=[f"article_{aid}"])  # indexed-but-missing drift

    monkeypatch.setattr(doctor_mod, "retry_pending_vectors", lambda config: 0)
    result = fix(initialized_library)
    assert result["reembed_failures"] >= 1
    assert any("still pending" in a for a in result["actions"])


def test_fix_refuses_mass_delete_when_articles_dir_missing(initialized_library):
    import shutil

    from tiro.doctor import fix

    a, _ = _make_article(initialized_library, "Survivor A")
    b, _ = _make_article(initialized_library, "Survivor B")
    moved = initialized_library.library / "articles-moved"
    shutil.move(str(initialized_library.articles_dir), str(moved))

    result = fix(initialized_library)
    assert any("REFUSED" in act for act in result["actions"])

    conn = get_connection(initialized_library.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
    finally:
        conn.close()
    assert n == 2, "rows must survive a missing articles dir"


def test_fix_keeps_vector_drift_visible_during_mass_delete_refusal(initialized_library):
    """Important review finding: a refused row is not in deleted_ids, so the
    (d)-class loop used to flip its drifted 'indexed' status to 'pending'
    even though its markdown is unreachable (that's why the refusal fired).
    retry_pending_vectors then marked it 'failed', and 'failed' + no vector +
    present file matches no scan() detection class — the drift became
    permanently invisible. fix() must leave refused rows' vector_status
    untouched so the drift stays visible as vector_missing, and the user's
    mandated re-run (after restoring the dir) heals it."""
    import shutil

    from tiro.doctor import fix, scan

    config = initialized_library
    aid, _ = _make_article(config, "Survivor A")
    bid, _ = _make_article(config, "Drifted Victim")
    # Vector drift on bid: status still 'indexed' but the vector is gone.
    get_collection().delete(ids=[f"article_{bid}"])

    moved = config.library / "articles-moved"
    shutil.move(str(config.articles_dir), str(moved))

    result = fix(config)
    assert any("REFUSED" in act for act in result["actions"])

    conn = get_connection(config.db_path)
    try:
        status = conn.execute(
            "SELECT vector_status FROM articles WHERE id = ?", (bid,)
        ).fetchone()["vector_status"]
    finally:
        conn.close()
    assert status == "indexed", (
        "refused row's drifted status must stay 'indexed' so the drift "
        "remains visible to scan() as vector_missing"
    )

    # Heal: restore the articles dir and re-run. Everything converges clean.
    shutil.move(str(moved), str(config.articles_dir))
    fix(config)
    report = scan(config)
    assert report["clean"] is True, report
    assert get_collection().get(ids=[f"article_{bid}"])["ids"] == [f"article_{bid}"]


def test_fix_does_not_fail_pending_rows_during_mass_delete_refusal(initialized_library):
    """Important review finding: fix() called retry_pending_vectors()
    unconditionally, even when the mass-delete refusal fired. During a
    refusal every markdown file is unreachable (that's why the refusal
    fired), so retry_pending_vectors marks ANY 'pending' row it can't reach
    as 'failed' — including a row that was legitimately 'pending' for an
    unrelated reason (e.g. a ChromaDB outage at ingestion time, well before
    the articles dir went missing). Once flipped to 'failed', the row is
    invisible to scan() (vector_missing needs 'indexed'; vector_unmarked
    needs a vector present) and excluded from reembed_failures (counts only
    'pending'), so after the user restores the dir and re-runs as instructed,
    the article is permanently unsearchable with doctor reporting clean.
    fix() must skip the doomed-and-destructive retry during a refusal and
    leave the row 'pending' so it stays healable."""
    import shutil

    from tiro.doctor import fix, scan

    config = initialized_library
    aid, _ = _make_article(config, "Survivor A")
    bid, _ = _make_article(config, "Outage Residue")
    # Simulate ingestion-time ChromaDB outage residue: status 'pending' with
    # no vector ever written, unrelated to the mass-delete that follows.
    get_collection().delete(ids=[f"article_{bid}"])
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            "UPDATE articles SET vector_status = 'pending' WHERE id = ?", (bid,)
        )
        conn.commit()
    finally:
        conn.close()

    moved = config.library / "articles-moved"
    shutil.move(str(config.articles_dir), str(moved))

    result = fix(config)
    assert any("REFUSED" in act for act in result["actions"])

    conn = get_connection(config.db_path)
    try:
        status = conn.execute(
            "SELECT vector_status FROM articles WHERE id = ?", (bid,)
        ).fetchone()["vector_status"]
    finally:
        conn.close()
    assert status == "pending", (
        "refused row's legitimately-pending status must NOT be flipped to "
        "'failed' by a doomed retry_pending_vectors() call — it must stay "
        "'pending' so it remains healable after the dir is restored"
    )

    # Heal: restore the articles dir and re-run. Everything converges clean.
    shutil.move(str(moved), str(config.articles_dir))
    fix(config)
    report = scan(config)
    assert report["clean"] is True, report
    assert get_collection().get(ids=[f"article_{bid}"])["ids"] == [f"article_{bid}"]


def test_fix_repairs_single_missing_markdown_when_dir_present(initialized_library):
    """Minor: the total_articles > 1 guard boundary. A single article with a
    missing markdown file (articles dir itself still present) is ordinary
    interrupted-delete residue, not mass-delete residue — fix() must repair
    it normally rather than refuse."""
    from tiro.doctor import fix

    config = initialized_library
    aid, md_path = _make_article(config, "Lone Victim")
    (config.articles_dir / md_path).unlink()

    result = fix(config)
    assert not any("REFUSED" in act for act in result["actions"]), result["actions"]

    conn = get_connection(config.db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM articles WHERE id = ?", (aid,)
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 0, "the single missing-markdown row must be deleted, not refused"


def test_scan_normalizes_absolute_markdown_paths(initialized_library):
    from tiro.doctor import scan

    aid, md_name = _make_article(initialized_library, "Legacy Abs Path")
    abs_path = str(initialized_library.articles_dir / md_name)
    conn = get_connection(initialized_library.db_path)
    try:
        conn.execute("UPDATE articles SET markdown_path = ? WHERE id = ?", (abs_path, aid))
        conn.commit()
    finally:
        conn.close()

    report = scan(initialized_library)
    assert not any(r["id"] == aid for r in report["missing_markdown"])
    assert md_name not in report["orphaned_markdown"]


def test_scan_flags_failed_rows_without_vector(initialized_library):
    from tiro.doctor import scan

    aid, _ = _make_article(initialized_library, "Failed Residue")
    get_collection().delete(ids=[f"article_{aid}"])
    conn = get_connection(initialized_library.db_path)
    try:
        conn.execute("UPDATE articles SET vector_status = 'failed' WHERE id = ?", (aid,))
        conn.commit()
    finally:
        conn.close()

    report = scan(initialized_library)
    assert aid in report["vector_failed"]
    assert report["structurally_consistent"] is False


def test_fix_heals_failed_rows_without_vector(initialized_library):
    from tiro.doctor import fix, scan

    aid, _ = _make_article(initialized_library, "Failed But Healable")
    get_collection().delete(ids=[f"article_{aid}"])
    conn = get_connection(initialized_library.db_path)
    try:
        conn.execute("UPDATE articles SET vector_status = 'failed' WHERE id = ?", (aid,))
        conn.commit()
    finally:
        conn.close()

    fix(initialized_library)
    report = scan(initialized_library)
    assert report["clean"] is True, report
    assert get_collection().get(ids=[f"article_{aid}"])["ids"] == [f"article_{aid}"]


def test_scan_detects_wiki_index_drift(initialized_library):
    """A wiki page whose file was hand-deleted leaves an orphan derived row
    -- exactly the mismatch wiki_index_drift is meant to surface."""
    from tiro.doctor import scan
    from tiro.wiki import write_page

    config = initialized_library
    write_page(
        config, slug="entities/anthropic", kind="entity", title="Anthropic",
        entity_type="company", article_uids=[], body="Anthropic body.",
        generated_by=None,
    )
    (config.wiki_dir / "entities" / "anthropic.md").unlink()

    report = scan(config)
    assert report["wiki_index_drift"] >= 1
    # Housekeeping only: does not affect structural consistency / exit code.
    assert report["structurally_consistent"] is True
    assert report["clean"] is False


def test_fix_reconciles_wiki_index_without_touching_surviving_files(initialized_library):
    """--fix runs reconcile_wiki_index() to heal the derived-row mismatch,
    but must NEVER write or delete a wiki page file itself -- files are the
    source of truth. Proven by hashing a surviving page's bytes before and
    after fix()."""
    from tiro.doctor import fix, scan
    from tiro.wiki import write_page

    config = initialized_library
    write_page(
        config, slug="entities/anthropic", kind="entity", title="Anthropic",
        entity_type="company", article_uids=[], body="Anthropic body.",
        generated_by=None,
    )
    write_page(
        config, slug="concepts/testing", kind="concept", title="Testing",
        entity_type=None, article_uids=[], body="Testing body.",
        generated_by=None,
    )
    survivor_path = config.wiki_dir / "concepts" / "testing.md"
    survivor_before = survivor_path.read_bytes()

    # Hand-delete one page file -- orphans its derived wiki_pages row.
    (config.wiki_dir / "entities" / "anthropic.md").unlink()

    result = fix(config)
    assert any("reconciled wiki index" in a for a in result["actions"]), result["actions"]

    report = scan(config)
    assert report["wiki_index_drift"] == 0

    # Surviving file byte-exact, untouched by the fix.
    assert survivor_path.read_bytes() == survivor_before
    # Orphaned row for the deleted file is gone post-reconcile (files win).
    conn = get_connection(config.db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM wiki_pages WHERE slug = 'entities/anthropic'"
        ).fetchone()["n"] == 0
    finally:
        conn.close()
