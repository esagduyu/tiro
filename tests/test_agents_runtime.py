"""Agent runtime kernel tests (Phase 6 K1): contract, registry, context,
runtime loop, traces, doctor integration."""

import ast
import json as _json
import os
import time
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
