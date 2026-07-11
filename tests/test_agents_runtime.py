"""Agent runtime kernel tests (Phase 6 K1): contract, registry, context,
runtime loop, traces, doctor integration."""

import ast
import json as _json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

AGENTS_DIR = Path(__file__).resolve().parent.parent / "tiro" / "agents"


class EchoOutput(BaseModel):
    text: str


class EchoAgent:
    """Minimal conforming agent used across the runtime tests."""

    name = "echo"
    version = "0.1"
    inputs = {"text": str}
    tier = "light"
    output_model = EchoOutput

    def run(self, ctx, *, text):
        reply = ctx.llm("light", f"echo: {text}", purpose="echo_test", max_tokens=64)
        return ctx.result(EchoOutput(text=reply))


@pytest.fixture
def echo_registered():
    from tiro.agents import registry

    agent = EchoAgent()
    registry.register(agent)
    yield agent
    registry.unregister("echo")


# --- Task 1: contract + registry ---------------------------------------


def test_agent_result_shape():
    from tiro.agents.base import AgentResult

    r = AgentResult(
        outputs=EchoOutput(text="x"), citations=["01ABC"],
        tokens_in=1, tokens_out=2, cost_usd=0.0, run_uid="01RUN",
    )
    assert r.citations == ["01ABC"]
    assert r.run_uid == "01RUN"


def test_agent_run_error_is_runtime_error_and_carries_run_uid():
    from tiro.agents.base import AgentRunError

    e = AgentRunError("boom", run_uid="01RUN")
    assert isinstance(e, RuntimeError)
    assert e.run_uid == "01RUN"
    assert AgentRunError("no row").run_uid is None


def test_registry_register_get_unregister(echo_registered):
    from tiro.agents import registry

    assert registry.get("echo") is echo_registered
    assert "echo" in registry.all_agents()
    with pytest.raises(ValueError):
        registry.register(EchoAgent())  # duplicate name
    with pytest.raises(KeyError):
        registry.get("nope")


def test_ensure_builtins_idempotent():
    from tiro.agents import registry

    registry.ensure_builtins()
    before = set(registry.all_agents())
    registry.ensure_builtins()
    assert set(registry.all_agents()) == before


def test_builtin_modules_never_touch_stores_directly():
    """Spec §2: run() is pure orchestration — builtin agent modules must not
    import DB/vector/network modules. AST denylist over tiro/agents/builtin/."""
    forbidden = {
        "sqlite3", "tiro.database", "chromadb", "tiro.vectorstore",
        "httpx", "requests", "socket", "urllib", "anthropic",
    }
    builtin_dir = AGENTS_DIR / "builtin"
    offenders = []
    for py in builtin_dir.glob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for m in mods:
                if m in forbidden or any(m.startswith(f + ".") for f in forbidden):
                    offenders.append(f"{py.name}: {m}")
    assert offenders == []


# --- Task 2: migration 014 ----------------------------------------------

AGENT_RUNS_COLUMNS = {
    "id", "run_uid", "agent_name", "agent_version", "started_at",
    "completed_at", "status", "provider", "model", "input_json",
    "output_json", "citations_json", "tokens_in", "tokens_out",
    "cost_usd", "error", "replay_of",
}


def _table_columns(db_path, table):
    from tiro.database import get_connection

    conn = get_connection(db_path)
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_xinfo({table})")}
    finally:
        conn.close()


def test_fresh_install_has_agent_runs(tmp_path):
    from tiro.database import init_db

    db = tmp_path / "fresh.db"
    init_db(db)
    assert _table_columns(db, "agent_runs") == AGENT_RUNS_COLUMNS


def test_migration_014_upgrades_existing_db(tmp_path):
    from tiro.database import get_connection, init_db, migrate_db

    db = tmp_path / "old.db"
    init_db(db)
    conn = get_connection(db)
    try:
        conn.execute("DROP TABLE agent_runs")  # simulate a pre-014 library
        conn.execute("PRAGMA user_version = 13")
        conn.commit()
    finally:
        conn.close()
    migrate_db(db)
    assert _table_columns(db, "agent_runs") == AGENT_RUNS_COLUMNS
    migrate_db(db)  # idempotent re-run
    assert _table_columns(db, "agent_runs") == AGENT_RUNS_COLUMNS


def test_latest_migration_is_014():
    from tiro.migrations import LATEST_VERSION

    assert LATEST_VERSION == 14  # 015/016 reserved for sync S1/S2, 017 for K3


def test_trace_retention_config_defaults(test_config):
    assert test_config.agent_trace_retention_days == 90
    assert test_config.agent_trace_max_mb == 500


# --- Task 3: trace writer + pruning --------------------------------------


def _read_trace(path):
    return [_json.loads(line) for line in path.read_text().splitlines()]


def test_trace_writer_header_and_events(tmp_path):
    from tiro.agents.runtime import TraceWriter

    p = tmp_path / "01RUN.jsonl"
    tw = TraceWriter(p)
    tw.header(agent="echo", version="0.1", inputs={"text": "hi"},
              provider="fake", model="m", replay_of=None)
    tw.event("llm", "echo_test", {"tier": "light", "prompt": "echo: hi",
             "max_tokens": 64}, result="ok", tokens_in=1, tokens_out=2,
             cost_usd=0.0)
    tw.event("tool", "get_article", {"uid_or_id": 3}, result={"id": 3})
    tw.close()

    lines = _read_trace(p)
    assert [ln["seq"] for ln in lines] == [0, 1, 2]
    assert lines[0]["kind"] == "run" and lines[0]["agent"] == "echo"
    assert lines[0]["replay_of"] is None
    assert lines[1]["kind"] == "llm" and lines[1]["name"] == "echo_test"
    assert lines[1]["args"]["prompt"] == "echo: hi"      # args stored FULL
    assert lines[1]["result"] == '"ok"'                   # JSON-serialized
    assert lines[1]["result_digest"].startswith("sha256:")
    assert lines[1]["tokens_in"] == 1
    assert lines[2]["result"] == '{"id": 3}'
    assert "ts" in lines[1]


def test_trace_writer_truncates_large_results(tmp_path):
    from tiro.agents.runtime import TRACE_PREVIEW_CHARS, TraceWriter

    p = tmp_path / "01BIG.jsonl"
    tw = TraceWriter(p)
    tw.header(agent="a", version="1", inputs={}, provider="fake",
              model="m", replay_of=None)
    big = "x" * (40 * 1024)
    tw.event("tool", "get_article", {"uid_or_id": 1}, result=big)
    tw.close()

    line = _read_trace(p)[1]
    assert line["truncated"] is True
    assert "result" not in line
    assert len(line["result_preview"]) == TRACE_PREVIEW_CHARS
    assert line["result_digest"].startswith("sha256:")


def test_prune_traces_age_and_size(test_config):
    from tiro.agents.runtime import prune_traces, traces_dir

    tdir = traces_dir(test_config)
    tdir.mkdir(parents=True, exist_ok=True)

    old = tdir / "01OLD.jsonl"
    old.write_text("{}\n")
    stale = time.time() - 91 * 86400
    os.utime(old, (stale, stale))

    fresh = tdir / "01NEW.jsonl"
    fresh.write_text("{}\n")

    prune_traces(test_config)
    assert not old.exists()          # over agent_trace_retention_days
    assert fresh.exists()

    # Size cap: shrink the cap to 0 MB — everything goes, oldest first,
    # and prune_traces never raises.
    test_config.agent_trace_max_mb = 0
    prune_traces(test_config)
    assert not fresh.exists()


def test_prune_traces_never_raises_when_dir_missing(test_config):
    from tiro.agents.runtime import prune_traces

    prune_traces(test_config)  # no agents/traces dir at all — must be a no-op


# --- Task 4: RunContext ---------------------------------------------------


def _make_ctx(config, tmp_path, override=None):
    from tiro.agents.context import RunContext
    from tiro.agents.runtime import TraceWriter

    tw = TraceWriter(tmp_path / "ctx-test.jsonl")
    tw.header(agent="t", version="1", inputs={}, provider="fake",
              model="m", replay_of=None)
    return RunContext(config, trace=tw, run_uid="01CTX", model_override=override), tw


def _seed_article(config, title="Ctx Article", body="Some body text."):
    """Insert one article row + markdown file directly (no LLM, no chroma)."""
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
        sid = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO articles (uid, source_id, title, url, slug,
               markdown_path, word_count, reading_time_min, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, 3, 1, datetime('now'))""",
            (uid, sid, title, f"https://example.com/{uid[:6]}", fname[:-3], fname),
        )
        aid = cur.lastrowid
        conn.commit()
        return aid, uid
    finally:
        conn.close()


def test_ctx_llm_traces_audits_and_accumulates(initialized_library, fake_llm, tmp_path):
    fake_llm("hello back")
    ctx, tw = _make_ctx(initialized_library, tmp_path)
    text = ctx.llm("light", "hello", purpose="ctx_test", max_tokens=32)
    tw.close()
    assert text == "hello back"
    assert ctx.tokens_in == 0 and ctx.tokens_out == 0   # fake backend reports 0
    lines = _read_trace(tmp_path / "ctx-test.jsonl")
    llm_line = lines[1]
    assert llm_line["kind"] == "llm" and llm_line["name"] == "ctx_test"
    assert llm_line["args"]["prompt"] == "hello"
    assert llm_line["args"]["tier"] == "light"


def test_ctx_llm_model_override_forces_provider(initialized_library, tmp_path):
    # Override to the fake provider even though config says anthropic:
    # proves the override path resolves the call, and no API key is needed.
    from tiro import llm as llm_module

    llm_module.queue_fake_responses("override worked")
    ctx, tw = _make_ctx(initialized_library, tmp_path,
                        override={"provider": "fake", "model": "fake-1"})
    try:
        assert ctx.llm("heavy", "p", purpose="ovr") == "override worked"
        # the ORIGINAL config object is untouched
        assert initialized_library.ai_heavy_provider == "anthropic"
    finally:
        tw.close()
        llm_module._fake_responses.clear()


def test_ctx_get_article_cites_and_reads_body(initialized_library, tmp_path):
    aid, uid = _seed_article(initialized_library, body="The body.")
    ctx, tw = _make_ctx(initialized_library, tmp_path)
    art = ctx.get_article(aid)
    tw.close()
    assert art["content"] == "The body."
    assert art["uid"] == uid
    assert ctx.citations == [uid]
    art2 = ctx.get_article(uid)               # uid lookup path
    assert art2["id"] == aid
    assert ctx.citations == [uid]             # deduped, order preserved


def test_ctx_get_article_errors_match_analysis_semantics(initialized_library, tmp_path):
    ctx, tw = _make_ctx(initialized_library, tmp_path)
    with pytest.raises(ValueError, match="Article 999 not found"):
        ctx.get_article(999)
    aid, _uid = _seed_article(initialized_library, title="Gone File")
    (initialized_library.articles_dir / "gone-file.md").unlink()
    with pytest.raises(ValueError, match="Markdown file not found"):
        ctx.get_article(aid)
    tw.close()


def test_ctx_get_highlights_window_and_citation(initialized_library, tmp_path):
    from tiro.database import get_connection
    from tiro.migrations import new_ulid

    aid, uid = _seed_article(initialized_library)
    conn = get_connection(initialized_library.db_path)
    try:
        # Real `highlights` schema (tiro/database.py SCHEMA) uses
        # prefix_context/suffix_context and text_position_start/
        # text_position_end — not the prefix_text/position_start names
        # from the original task brief.
        conn.execute(
            """INSERT INTO highlights (uid, article_id, quote_text, prefix_context,
               suffix_context, text_position_start, text_position_end, color,
               content_hash, created_at, updated_at)
               VALUES (?, ?, 'Q', '', '', 0, 1, 'yellow', 'h',
                       datetime('now'), datetime('now'))""",
            (new_ulid(), aid),
        )
        conn.commit()
    finally:
        conn.close()
    ctx, tw = _make_ctx(initialized_library, tmp_path)
    rows = ctx.get_highlights(days=7, limit=50)
    tw.close()
    assert len(rows) == 1
    assert rows[0]["quote"] == "Q" and rows[0]["article_id"] == aid
    assert rows[0]["note"] is None
    assert ctx.citations == [uid]


def test_ctx_result_prune_only(initialized_library, tmp_path):
    _aid, uid = _seed_article(initialized_library)
    ctx, tw = _make_ctx(initialized_library, tmp_path)
    ctx.get_article(uid)
    r = ctx.result(EchoOutput(text="x"), citations=[uid, "01FABRICATED"])
    tw.close()
    assert r.citations == [uid]               # fabricated uid stripped
    assert r.run_uid == "01CTX"
    r2 = ctx.result(EchoOutput(text="y"))     # default = all accumulated
    assert r2.citations == [uid]


# --- Task 4 fix wave: search/wiki/similar coverage ---


def test_ctx_search_backfills_uids_and_cites_in_order(
    initialized_library, tmp_path, monkeypatch
):
    aid1, uid1 = _seed_article(initialized_library, title="First Hit")
    aid2, uid2 = _seed_article(initialized_library, title="Second Hit")

    fake_results = [
        {"id": aid1, "title": "First Hit", "similarity_score": 0.9},
        {"id": aid2, "title": "Second Hit", "similarity_score": 0.5},
    ]

    def fake_search_articles(q, config, limit=10):
        assert q == "foo"
        assert limit == 3
        return fake_results

    monkeypatch.setattr(
        "tiro.search.semantic.search_articles", fake_search_articles
    )

    ctx, tw = _make_ctx(initialized_library, tmp_path)
    results = ctx.search("foo", limit=3)
    tw.close()

    assert [r["uid"] for r in results] == [uid1, uid2]
    assert ctx.citations == [uid1, uid2]

    lines = _read_trace(tmp_path / "ctx-test.jsonl")
    tool_lines = [ln for ln in lines if ln["kind"] == "tool"]
    assert tool_lines[0]["name"] == "search"
    assert tool_lines[0]["args"] == {"q": "foo", "limit": 3}


def test_ctx_get_wiki_page_cites_article_uids(
    initialized_library, tmp_path, monkeypatch
):
    _aid, uid = _seed_article(initialized_library, title="Wiki Subject")

    fake_page = {
        "slug": "entities/some-person",
        "title": "Some Person",
        "article_uids": [uid],
    }

    monkeypatch.setattr(
        "tiro.wiki.read_page",
        lambda config, slug: fake_page if slug == "entities/some-person" else None,
    )

    ctx, tw = _make_ctx(initialized_library, tmp_path)
    page = ctx.get_wiki_page("entities/some-person")
    tw.close()

    assert page == fake_page
    assert ctx.citations == [uid]

    lines = _read_trace(tmp_path / "ctx-test.jsonl")
    tool_lines = [ln for ln in lines if ln["kind"] == "tool"]
    assert tool_lines[0]["name"] == "get_wiki_page"
    assert tool_lines[0]["args"] == {"slug": "entities/some-person"}


def test_ctx_get_wiki_page_none_cites_nothing_but_still_traces(
    initialized_library, tmp_path, monkeypatch
):
    monkeypatch.setattr("tiro.wiki.read_page", lambda config, slug: None)

    ctx, tw = _make_ctx(initialized_library, tmp_path)
    page = ctx.get_wiki_page("entities/missing")
    tw.close()

    assert page is None
    assert ctx.citations == []

    lines = _read_trace(tmp_path / "ctx-test.jsonl")
    tool_lines = [ln for ln in lines if ln["kind"] == "tool"]
    assert tool_lines[0]["name"] == "get_wiki_page"
    assert tool_lines[0]["args"] == {"slug": "entities/missing"}


def test_ctx_similar_articles_maps_similarity_and_cites_both(
    initialized_library, tmp_path, monkeypatch
):
    aid1, uid1 = _seed_article(initialized_library, title="Anchor Article")
    aid2, uid2 = _seed_article(initialized_library, title="Related Article")

    def fake_find_related_articles(article_id, config, limit=5):
        assert article_id == aid1
        assert limit == 5
        return [{"related_article_id": aid2, "similarity_score": 0.87}]

    monkeypatch.setattr(
        "tiro.search.semantic.find_related_articles", fake_find_related_articles
    )

    ctx, tw = _make_ctx(initialized_library, tmp_path)
    out = ctx.similar_articles(uid1, k=5)
    tw.close()

    assert len(out) == 1
    entry = out[0]
    assert entry["id"] == aid2
    assert entry["uid"] == uid2
    assert entry["title"] == "Related Article"
    assert "summary" in entry
    assert entry["similarity"] == 0.87

    # anchor article's uid is cited (via the internal get_article call),
    # then the related article's uid, in that order.
    assert ctx.citations == [uid1, uid2]

    lines = _read_trace(tmp_path / "ctx-test.jsonl")
    tool_lines = [ln for ln in lines if ln["kind"] == "tool"]
    tool_names = [ln["name"] for ln in tool_lines]
    assert "get_article" in tool_names
    assert "similar_articles" in tool_names
    similar_line = [ln for ln in tool_lines if ln["name"] == "similar_articles"][0]
    assert similar_line["args"] == {"article_uid": uid1, "k": 5}


def test_ctx_get_highlights_accepts_real_production_timestamp_format(
    initialized_library, tmp_path
):
    """created_at rows in production are written via
    tiro.annotations._now_iso() as '%Y-%m-%dT%H:%M:%SZ' (real 'T'/'Z' ISO,
    not SQLite's datetime('now') space-separated format used by the sibling
    test above). Guards the days= cutoff string comparison against the
    actual row shape highlights land in."""
    from tiro.database import get_connection
    from tiro.migrations import new_ulid

    aid, uid = _seed_article(initialized_library)
    recent = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_connection(initialized_library.db_path)
    try:
        conn.execute(
            """INSERT INTO highlights (uid, article_id, quote_text, prefix_context,
               suffix_context, text_position_start, text_position_end, color,
               content_hash, created_at, updated_at)
               VALUES (?, ?, 'Realistic Q', '', '', 0, 1, 'yellow', 'h', ?, ?)""",
            (new_ulid(), aid, recent, recent),
        )
        conn.commit()
    finally:
        conn.close()

    ctx, tw = _make_ctx(initialized_library, tmp_path)
    rows = ctx.get_highlights(days=7, limit=50)
    tw.close()

    assert len(rows) == 1
    assert rows[0]["quote"] == "Realistic Q"
    assert rows[0]["article_id"] == aid
    assert ctx.citations == [uid]
