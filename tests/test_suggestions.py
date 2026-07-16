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
