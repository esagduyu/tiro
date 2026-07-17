"""Suggestions storage + migration 017 (Phase 6 K3)."""

import json

import pytest

SUGGESTIONS_COLUMNS = {
    "id", "uid", "persona", "kind", "payload_json",
    "citations_json", "created_at", "status",
}


def _table_columns(db_path, table):
    from tiro.database import get_connection

    conn = get_connection(db_path)
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_xinfo({table})")}
    finally:
        conn.close()


def test_fresh_install_has_suggestions(tmp_path):
    from tiro.database import init_db

    db = tmp_path / "fresh.db"
    init_db(db)
    assert _table_columns(db, "suggestions") == SUGGESTIONS_COLUMNS


def test_migration_017_upgrades_existing_db(tmp_path):
    from tiro.database import get_connection, init_db, migrate_db

    db = tmp_path / "old.db"
    init_db(db)
    conn = get_connection(db)
    try:
        conn.execute("DROP TABLE suggestions")   # simulate a pre-017 library
        conn.execute("PRAGMA user_version = 16")
        conn.commit()
    finally:
        conn.close()
    migrate_db(db)
    assert _table_columns(db, "suggestions") == SUGGESTIONS_COLUMNS
    migrate_db(db)                               # idempotent re-run
    assert _table_columns(db, "suggestions") == SUGGESTIONS_COLUMNS


def test_suggestions_is_migration_017():
    # K3 claimed exactly migration 017 (suggestions). Assert that specific
    # claim rather than "017 is newest" or "no gaps": migration 016 (sync S2)
    # lands on a concurrent branch and later milestones add more — the
    # durable invariant is the number, not the ceiling (7c2ca0b precedent;
    # transient 15->17 gap on this branch authorized by D17, coordinator
    # enforces merge order).
    from tiro.migrations import MIGRATIONS

    versions = [v for v, _, _ in MIGRATIONS]
    assert len(versions) == len(set(versions))        # no duplicate claims
    assert versions == sorted(versions)               # ordered
    by_version = {v: desc for v, desc, _ in MIGRATIONS}
    assert "suggestions" in by_version[17]


def test_personas_disabled_config_default(test_config):
    assert test_config.personas_disabled == []


# --- Task 3: storage + ctx.suggest ---------------------------------------


def _seed_article(config, title="Sugg Article", body="Body text here."):
    """One article row + markdown file, no LLM/chroma (K1 test pattern)."""
    from tiro.database import get_connection
    from tiro.migrations import new_ulid

    uid = new_ulid()
    fname = f"{title.lower().replace(' ', '-')}.md"
    config.articles_dir.mkdir(parents=True, exist_ok=True)
    (config.articles_dir / fname).write_text(f"---\ntitle: {title}\n---\n\n{body}")
    conn = get_connection(config.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO sources (name, domain, source_type) VALUES (?, ?, 'web')",
            (f"src-{uid[:6]}", f"{uid[:6]}.example.com"),
        )
        cur = conn.execute(
            """INSERT INTO articles (uid, source_id, title, url, slug,
               markdown_path, word_count, reading_time_min, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, 3, 1, datetime('now'))""",
            (uid, cur.lastrowid, title, f"https://example.com/{uid[:6]}",
             fname[:-3], fname),
        )
        aid = cur.lastrowid
        conn.commit()
        return aid, uid
    finally:
        conn.close()


def _make_ctx(config, tmp_path, agent_name="persona:test"):
    from tiro.agents.context import RunContext
    from tiro.agents.runtime import TraceWriter

    tw = TraceWriter(tmp_path / "sugg-ctx.jsonl")
    tw.header(agent=agent_name, version="1", inputs={}, provider="fake",
              model="m", replay_of=None)
    return RunContext(config, trace=tw, run_uid="01SUG",
                      agent_name=agent_name), tw


def test_create_list_get_set_status(initialized_library):
    from tiro.suggestions import (
        create_suggestion,
        get_suggestion,
        list_suggestions,
        set_suggestion_status,
    )

    s = create_suggestion(
        initialized_library, persona="persona:devils-advocate", kind="note",
        payload={"article_id": 7, "markdown": "Counterpoint."},
        citations=["01AAA"])
    assert s["uid"] and s["status"] == "pending"

    rows = list_suggestions(initialized_library, status="pending")
    assert len(rows) == 1
    assert rows[0]["payload"]["markdown"] == "Counterpoint."
    assert rows[0]["citations"] == ["01AAA"]

    assert list_suggestions(initialized_library, article_id=7)
    assert list_suggestions(initialized_library, article_id=8) == []

    assert set_suggestion_status(initialized_library, s["uid"], "dismissed")
    assert get_suggestion(initialized_library, s["uid"])["status"] == "dismissed"
    # pending-only transition: a second flip is refused
    assert not set_suggestion_status(initialized_library, s["uid"], "accepted")


def test_create_suggestion_rejects_unknown_kind(initialized_library):
    from tiro.suggestions import create_suggestion

    with pytest.raises(ValueError, match="kind"):
        create_suggestion(initialized_library, persona="p", kind="exploit",
                          payload={}, citations=[])


def test_ctx_suggest_prunes_fabricated_citations(initialized_library, tmp_path):
    from tiro.suggestions import list_suggestions

    aid, uid = _seed_article(initialized_library)
    ctx, tw = _make_ctx(initialized_library, tmp_path)
    ctx.get_article(aid)                       # legit read -> accumulated
    sug_uid = ctx.suggest("note",
                          {"article_id": aid, "markdown": "hi"},
                          citations=[uid, "01FABRICATED"])
    tw.close()
    row = list_suggestions(initialized_library)[0]
    assert row["uid"] == sug_uid
    assert row["citations"] == [uid]           # fabricated uid stripped
    assert row["persona"] == "persona:test"
    # traced as a tool event
    lines = [json.loads(ln) for ln in
             (tmp_path / "sugg-ctx.jsonl").read_text().splitlines()]
    assert any(ln.get("name") == "suggest" for ln in lines)


def test_ctx_list_recent_articles_window_and_citation(initialized_library, tmp_path):
    from tiro.database import get_connection

    aid, uid = _seed_article(initialized_library, title="Fresh One")
    old_aid, _ = _seed_article(initialized_library, title="Stale One")
    conn = get_connection(initialized_library.db_path)
    try:
        conn.execute(
            "UPDATE articles SET ingested_at = datetime('now', '-3 days') "
            "WHERE id = ?", (old_aid,))
        conn.commit()
    finally:
        conn.close()
    ctx, tw = _make_ctx(initialized_library, tmp_path)
    rows = ctx.list_recent_articles(hours=24)
    tw.close()
    assert [r["id"] for r in rows] == [aid]
    assert ctx.citations == [uid]


# --- Task 6: accept appliers ----------------------------------------------


def _mk_suggestion(config, kind, payload, citations=None):
    from tiro.suggestions import create_suggestion

    return create_suggestion(config, persona="persona:test", kind=kind,
                             payload=payload, citations=citations or [])


def test_apply_note_creates_then_appends(initialized_library):
    from tiro.annotations import read_note, sidecar_stem
    from tiro.database import get_connection
    from tiro.suggestions import apply_suggestion

    aid, _uid = _seed_article(initialized_library, title="Note Target")
    s1 = _mk_suggestion(initialized_library, "note",
                        {"article_id": aid, "markdown": "First insight."})
    apply_suggestion(initialized_library, s1)

    conn = get_connection(initialized_library.db_path)
    try:
        row = conn.execute("SELECT markdown_path FROM articles WHERE id = ?",
                           (aid,)).fetchone()
    finally:
        conn.close()
    stem = sidecar_stem(row)
    assert read_note(initialized_library, stem) is not None
    assert "First insight." in read_note(initialized_library, stem)

    s2 = _mk_suggestion(initialized_library, "note",
                        {"article_id": aid, "markdown": "Second insight."})
    apply_suggestion(initialized_library, s2)
    note = read_note(initialized_library, stem)
    assert "First insight." in note and "Second insight." in note
    assert '*Suggested by persona "persona:test":*' in note


def test_apply_tier_same_write_as_classifier(initialized_library):
    from tiro.database import get_connection
    from tiro.suggestions import SuggestionApplyError, apply_suggestion

    aid, _uid = _seed_article(initialized_library, title="Tier Target")
    apply_suggestion(initialized_library, _mk_suggestion(
        initialized_library, "tier_suggestion",
        {"article_id": aid, "tier": "must-read"}))
    conn = get_connection(initialized_library.db_path)
    try:
        assert conn.execute("SELECT ai_tier FROM articles WHERE id = ?",
                            (aid,)).fetchone()["ai_tier"] == "must-read"
    finally:
        conn.close()
    with pytest.raises(SuggestionApplyError, match="tier"):
        apply_suggestion(initialized_library, _mk_suggestion(
            initialized_library, "tier_suggestion",
            {"article_id": aid, "tier": "banana"}))


def test_apply_digest_section_appends_or_409s(initialized_library):
    from datetime import date

    from tiro.database import get_connection
    from tiro.suggestions import SuggestionApplyError, apply_suggestion

    s = _mk_suggestion(initialized_library, "digest_section",
                       {"title": "Daily Themes", "markdown": "Theme body."})
    with pytest.raises(SuggestionApplyError, match="no cached digest"):
        apply_suggestion(initialized_library, s)

    today = date.today().isoformat()
    conn = get_connection(initialized_library.db_path)
    try:
        conn.execute(
            "INSERT INTO digests (date, digest_type, content, article_ids) "
            "VALUES (?, 'ranked', 'Existing digest.', '[]')", (today,))
        conn.commit()
    finally:
        conn.close()
    apply_suggestion(initialized_library, s)
    conn = get_connection(initialized_library.db_path)
    try:
        content = conn.execute(
            "SELECT content FROM digests WHERE date = ? AND digest_type = "
            "'ranked'", (today,)).fetchone()["content"]
    finally:
        conn.close()
    assert content.startswith("Existing digest.")
    assert "## Daily Themes" in content and "Theme body." in content


def test_apply_wiki_page_updates_existing_only(initialized_library):
    from tiro.suggestions import SuggestionApplyError, apply_suggestion
    from tiro.wiki import read_page, write_page

    with pytest.raises(SuggestionApplyError, match="does not exist"):
        apply_suggestion(initialized_library, _mk_suggestion(
            initialized_library, "wiki_page",
            {"slug": "entities/nobody", "markdown": "New body."}))

    write_page(initialized_library, slug="entities/ada", kind="entity",
               title="Ada", entity_type="person", article_uids=[],
               body="Old body.", generated_by="test",
               user_pinned_note="KEEP ME")
    apply_suggestion(initialized_library, _mk_suggestion(
        initialized_library, "wiki_page",
        {"slug": "entities/ada", "markdown": "Persona body."}))
    page = read_page(initialized_library, "entities/ada")
    assert "Persona body." in page["body"]
    assert page["user_pinned_note"] == "KEEP ME"       # survives, always


def test_apply_contradiction_has_no_applier_yet(initialized_library):
    from tiro.suggestions import SuggestionApplyError, apply_suggestion

    with pytest.raises(SuggestionApplyError, match="no applier"):
        apply_suggestion(initialized_library, _mk_suggestion(
            initialized_library, "contradiction", {"claim": "x"}))


# --- Task 9: export/backup posture -----------------------------------------


def test_export_bundle_excludes_suggestions(initialized_library):
    import json
    import zipfile

    from tiro.export import export_library

    _seed_article(initialized_library, title="Export Check")
    _mk_suggestion(initialized_library, "note",
                   {"article_id": 1, "markdown": "m"})
    out = export_library(initialized_library)
    try:
        with zipfile.ZipFile(out) as zf:
            meta = json.loads(zf.read("metadata.json"))
        assert "suggestions" not in meta
    finally:
        out.unlink(missing_ok=True)


def test_backup_snapshot_carries_personas_dir(initialized_library):
    # Snapshots are tar+zstd (not a stdlib-recognized tarfile compression
    # scheme) -- mirror tests/test_backup.py's own zstandard-stream-reader
    # helper rather than a bare tarfile.open("r:*").
    import tarfile

    import zstandard

    from tiro.agents.personas import ensure_personas
    from tiro.backup import create_snapshot

    ensure_personas(initialized_library)
    snapshot = create_snapshot(initialized_library)
    dctx = zstandard.ZstdDecompressor()
    with snapshot.open("rb") as raw, dctx.stream_reader(raw) as z:
        with tarfile.open(mode="r|", fileobj=z) as tar:
            names = [m.name for m in tar]
    assert any("personas/devils-advocate.md" in n for n in names)
