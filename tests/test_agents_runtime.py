"""Agent runtime kernel tests (Phase 6 K1): contract, registry, context,
runtime loop, traces, doctor integration."""

import ast
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
