"""Agent-CLI backends: spawn shape, envelope parsing, isolation, failure modes."""

import json

import pytest

from tiro import llm_cli
from tiro.llm import llm_call


@pytest.fixture
def fake_cli(monkeypatch):
    """Capture the spawned command; return a scripted CompletedProcess."""
    calls = {}

    def install(stdout: str, returncode: int = 0, stderr: str = ""):
        class P:
            pass

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs
            p = P()
            p.stdout, p.stderr, p.returncode = stdout, stderr, returncode
            return p

        monkeypatch.setattr(llm_cli.subprocess, "run", fake_run)
        monkeypatch.setattr(llm_cli.shutil, "which", lambda name: f"/usr/local/bin/{name}")
        return calls

    return install


def test_claude_cli_happy_path(test_config, fake_cli):
    calls = fake_cli(json.dumps({
        "type": "result", "is_error": False,
        "result": "digest text here", "total_cost_usd": 0.031,
    }))
    test_config.ai_heavy_provider = "claude-cli"
    result = llm_call(test_config, "heavy", "make me a digest", purpose="digest")
    assert result.text == "digest text here"
    assert result.cost_usd == 0.031
    cmd = calls["cmd"]
    assert cmd[0].endswith("claude") and "-p" in cmd
    assert "--output-format" in cmd and "json" in cmd
    # Isolation: spawned from a scratch cwd, not the user's project
    assert str(calls["kwargs"]["cwd"]).endswith("cli-sandbox")


def test_claude_cli_error_envelope_raises(test_config, fake_cli):
    fake_cli(json.dumps({"type": "result", "is_error": True,
                         "result": "rate limit reached"}))
    test_config.ai_heavy_provider = "claude-cli"
    with pytest.raises(RuntimeError, match="rate limit"):
        llm_call(test_config, "heavy", "x", purpose="digest")


def test_missing_binary_is_not_configured(test_config, monkeypatch):
    from tiro.llm import LLMNotConfigured

    monkeypatch.setattr(llm_cli.shutil, "which", lambda name: None)
    test_config.ai_heavy_provider = "claude-cli"
    with pytest.raises(LLMNotConfigured, match="not found"):
        llm_call(test_config, "heavy", "x", purpose="digest")


# --- codex-cli ---------------------------------------------------------
#
# Envelope verified locally 2026-07-04 against codex CLI v0.136.0
# (`codex exec --json`, ChatGPT-subscription auth):
#   codex exec --json --skip-git-repo-check --sandbox read-only \
#     -C <sandbox> -m <model> "<prompt>"
# emits one JSON object per line (JSONL) on stdout:
#   {"type": "thread.started", "thread_id": "..."}
#   {"type": "turn.started"}
#   {"type": "item.completed", "item": {"type": "agent_message", "text": "..."}}
#   {"type": "turn.completed", "usage": {"input_tokens": N, "output_tokens": N, ...}}
# On failure (verified with a bogus model name -> exit code 1):
#   {"type": "error", "message": "<json-encoded API error>"}
#   {"type": "turn.failed", "error": {"message": "<json-encoded API error>"}}
# No total_cost_usd equivalent is reported anywhere in the stream.

def _codex_jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events)


def test_codex_cli_happy_path(test_config, fake_cli):
    stdout = _codex_jsonl(
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message",
                                             "text": "digest text here"}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 10,
                                              "output_tokens": 20, "reasoning_output_tokens": 5}},
    )
    calls = fake_cli(stdout)
    test_config.ai_heavy_provider = "codex-cli"
    result = llm_call(test_config, "heavy", "make me a digest", purpose="digest")
    assert result.text == "digest text here"
    assert result.tokens_in == 100
    assert result.tokens_out == 20
    assert result.cost_usd is None  # codex CLI reports no USD cost
    cmd = calls["cmd"]
    assert cmd[0].endswith("codex") and "exec" in cmd
    assert "--json" in cmd
    # Isolation: spawned from a scratch cwd, not the user's project
    assert str(calls["kwargs"]["cwd"]).endswith("cli-sandbox")
    # Never allowed to write/execute outside the sandbox
    assert "--sandbox" in cmd and "read-only" in cmd
    # Must not block waiting on inherited stdin
    assert calls["kwargs"]["stdin"] is llm_cli.subprocess.DEVNULL


def test_codex_cli_error_envelope_raises(test_config, fake_cli):
    stdout = _codex_jsonl(
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {"type": "error", "message": json.dumps({
            "type": "error", "status": 429,
            "error": {"type": "rate_limit_error", "message": "rate limit reached"},
        })},
        {"type": "turn.failed", "error": {"message": json.dumps({
            "type": "error", "status": 429,
            "error": {"type": "rate_limit_error", "message": "rate limit reached"},
        })}},
    )
    fake_cli(stdout, returncode=1, stderr="Reading additional input from stdin...")
    test_config.ai_heavy_provider = "codex-cli"
    with pytest.raises(RuntimeError, match="rate limit"):
        llm_call(test_config, "heavy", "x", purpose="digest")


def test_codex_missing_binary_is_not_configured(test_config, monkeypatch):
    from tiro.llm import LLMNotConfigured

    monkeypatch.setattr(llm_cli.shutil, "which", lambda name: None)
    test_config.ai_heavy_provider = "codex-cli"
    with pytest.raises(LLMNotConfigured, match="not found"):
        llm_call(test_config, "heavy", "x", purpose="digest")


def test_check_cli_backend_not_installed(test_config, monkeypatch):
    monkeypatch.setattr(llm_cli.shutil, "which", lambda name: None)
    assert llm_cli.check_cli_backend(test_config, "claude-cli") == "not installed"
    assert llm_cli.check_cli_backend(test_config, "codex-cli") == "not installed"


def test_check_cli_backend_ok(test_config, monkeypatch):
    monkeypatch.setattr(llm_cli.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert llm_cli.check_cli_backend(test_config, "claude-cli") == "ok"
    assert llm_cli.check_cli_backend(test_config, "codex-cli") == "ok"


def test_claude_cli_scrubs_anthropic_api_key_from_child_env(
    test_config, fake_cli, monkeypatch
):
    """A stray ANTHROPIC_API_KEY (direnv etc.) must not reach the claude CLI —
    it takes precedence over subscription login inside the CLI, defeating the
    backend's purpose (and failing outright on a depleted key)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stray")
    monkeypatch.setenv("HOME_MARKER_FOR_TEST", "kept")
    calls = fake_cli(json.dumps({"type": "result", "is_error": False, "result": "ok"}))
    test_config.ai_heavy_provider = "claude-cli"
    llm_call(test_config, "heavy", "x", purpose="digest")
    env = calls["kwargs"]["env"]
    assert "ANTHROPIC_API_KEY" not in env
    # The rest of the environment (CLI auth lives in HOME) passes through.
    assert env["HOME_MARKER_FOR_TEST"] == "kept"


def test_codex_cli_scrubs_openai_api_key_from_child_env(
    test_config, fake_cli, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stray")
    stdout = _codex_jsonl(
        {"type": "item.completed", "item": {"id": "i0", "type": "agent_message",
                                            "text": "ok"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    )
    calls = fake_cli(stdout)
    test_config.ai_heavy_provider = "codex-cli"
    llm_call(test_config, "heavy", "x", purpose="digest")
    assert "OPENAI_API_KEY" not in calls["kwargs"]["env"]
