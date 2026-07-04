"""Agent-CLI backends: run the user's own locally-authenticated AI CLI as an
llm_call() provider (Roadmap Decision #7 — local-only alpha feature).

ToS note (recorded, eyes-open): Anthropic's Claude Code terms direct
third-party products to API keys; the owner-decided posture is that spawning
the USER'S OWN locally-authenticated CLI on their own machine, opt-in, is a
different animal from a hosted service intermediating credentials. This
provider must never be a default, and hosted Tiro must never expose it.

Isolation: spawned with cwd set to an empty sandbox dir inside the library so
the CLI cannot pick up a project CLAUDE.md/AGENTS.md from wherever the Tiro
server happens to run. The user's HOME stays visible (that's where the CLI's
auth lives — the entire point).
"""

import json
import logging
import shutil
import subprocess

from tiro.config import TiroConfig

logger = logging.getLogger(__name__)

CLI_TIMEOUT_SECONDS = 300


def _sandbox_dir(config: TiroConfig):
    d = config.library / ".cli-sandbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_binary(path_or_name: str) -> str | None:
    return shutil.which(path_or_name)


def run_claude_cli(config: TiroConfig, model: str, prompt: str, *,
                   system: str | None, max_tokens: int):
    """claude -p invocation. Envelope (verified 2026-07-04 scoping brief):
    {"type": "result", "result": str, "is_error": bool, "total_cost_usd": float, ...}
    max_tokens is accepted for interface parity but not passed (no CLI flag)."""
    from tiro.llm import LLMNotConfigured, LLMResult

    exe = _resolve_binary(config.ai_claude_cli_path)
    if not exe:
        raise LLMNotConfigured(
            f"claude CLI not found ('{config.ai_claude_cli_path}') — install Claude Code "
            "or set ai_claude_cli_path"
        )
    cmd = [exe, "-p", prompt, "--model", model, "--output-format", "json"]
    if system:
        cmd += ["--system-prompt", system]
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=CLI_TIMEOUT_SECONDS, cwd=_sandbox_dir(config),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}")
    data = json.loads(proc.stdout)
    if data.get("is_error"):
        raise RuntimeError(f"claude CLI error: {str(data.get('result', ''))[:500]}")
    return LLMResult(
        text=data.get("result", ""),
        provider="claude-cli",
        model=model,
        cost_usd=data.get("total_cost_usd"),
    )


def _extract_codex_error_text(event: dict) -> str:
    """Codex's "error"/"turn.failed" events carry a JSON-ENCODED STRING as
    their message (the underlying API's error envelope, double-encoded).
    Unwrap it to the human-readable message where possible, falling back to
    the raw text so we never raise an empty error."""
    raw = event.get("message")
    if raw is None:
        raw = (event.get("error") or {}).get("message")
    if raw is None:
        raw = json.dumps(event.get("error") or event)
    try:
        inner = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return str(raw)
    if isinstance(inner, dict):
        msg = (inner.get("error") or {}).get("message") or inner.get("message")
        if msg:
            return str(msg)
    return str(raw)


def run_codex_cli(config: TiroConfig, model: str, prompt: str, *,
                  system: str | None, max_tokens: int):
    """codex exec invocation. Envelope (verified locally 2026-07-04 against
    codex CLI v0.136.0, ChatGPT-subscription auth — `codex exec --json`):
    one JSON object per line (JSONL) on stdout —
      {"type": "item.completed", "item": {"type": "agent_message", "text": ...}}
      {"type": "turn.completed", "usage": {"input_tokens": N, "output_tokens": N, ...}}
    On failure: {"type": "error", ...} / {"type": "turn.failed", "error": {...}}
    (also observed with a nonzero exit code, but the USEFUL error text lives
    in these stdout events, not stderr — stderr may just carry an unrelated
    "Reading additional input from stdin..." notice).
    No total_cost_usd equivalent is reported anywhere in the stream.
    max_tokens is accepted for interface parity but not passed (no CLI flag).
    --sandbox read-only: this is a text-generation call, not a coding task —
    codex must never be allowed to write files or run shell commands here.
    stdin=DEVNULL: codex exec reads stdin for additional instructions when
    it isn't a tty; explicitly closing it avoids any risk of it blocking on
    an inherited stdin when spawned from a long-running server process."""
    from tiro.llm import LLMNotConfigured, LLMResult

    exe = _resolve_binary(config.ai_codex_cli_path)
    if not exe:
        raise LLMNotConfigured(
            f"codex CLI not found ('{config.ai_codex_cli_path}') — install Codex CLI "
            "or set ai_codex_cli_path"
        )
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    cmd = [
        exe, "exec", "--json", "--skip-git-repo-check",
        "--sandbox", "read-only", "-C", str(_sandbox_dir(config)),
        "-m", model, full_prompt,
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=CLI_TIMEOUT_SECONDS, cwd=_sandbox_dir(config),
        stdin=subprocess.DEVNULL,
    )

    text = None
    tokens_in = tokens_out = None
    error_text = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype in ("error", "turn.failed"):
            error_text = _extract_codex_error_text(event)
        elif etype == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                text = item.get("text", "")
        elif etype == "turn.completed":
            usage = event.get("usage") or {}
            tokens_in = usage.get("input_tokens")
            tokens_out = usage.get("output_tokens")

    if error_text:
        raise RuntimeError(f"codex CLI error: {error_text[:500]}")
    if proc.returncode != 0:
        raise RuntimeError(f"codex CLI failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}")
    if text is None:
        raise RuntimeError(f"codex CLI produced no agent message (stdout: {proc.stdout.strip()[:500]})")

    return LLMResult(
        text=text,
        provider="codex-cli",
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=None,  # codex CLI does not report a USD cost anywhere
    )


def check_cli_backend(config: TiroConfig, provider: str) -> str:
    """Cheap install/auth probe for `tiro status`. Never raises."""
    name = config.ai_claude_cli_path if provider == "claude-cli" else config.ai_codex_cli_path
    exe = _resolve_binary(name)
    if not exe:
        return "not installed"
    return "ok"
