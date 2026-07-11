"""Agent run loop: run rows, JSONL traces, retention (Phase 6 K1).

Traces are files, runs are an index (spec §1): the full ordered event
stream lives at {library}/agents/traces/{run_uid}.jsonl; SQLite agent_runs
holds the queryable summary. Pruning deletes trace FILES only, never rows.
"""

import hashlib
import json
import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from tiro.agents.base import AgentResult, AgentRunError
from tiro.config import TiroConfig
from tiro.database import get_connection
from tiro.migrations import new_ulid

logger = logging.getLogger(__name__)

# One agent at a time (spec §3 "no orchestration"). threading.Lock, not an
# asyncio lock: every caller executes run_agent inside asyncio.to_thread
# worker threads (the same posture as all pre-runtime AI call sites).
_RUN_LOCK = threading.Lock()

TRACE_RESULT_INLINE_MAX = 32 * 1024   # chars of serialized result stored inline
TRACE_PREVIEW_CHARS = 2048            # preview kept when truncating


def traces_dir(config: TiroConfig) -> Path:
    return config.library / "agents" / "traces"


class TraceWriter:
    """Streams JSONL trace events. Opening the file is load-bearing (a run
    without a trace would break structural provenance — the caller treats an
    open failure as a run failure); per-event write failures after a
    successful open are logged once and swallowed (a partial trace beats a
    dead run)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self._seq = 0
        self._write_failed = False

    def _emit(self, line: dict) -> None:
        try:
            self._fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()
        except Exception as e:
            if not self._write_failed:
                logger.error("Trace write failed (continuing without): %s", e)
                self._write_failed = True

    def header(self, *, agent: str, version: str, inputs: dict,
               provider: str, model: str, replay_of: str | None) -> None:
        self._emit({
            "seq": self._seq, "ts": datetime.now(UTC).isoformat(),
            "kind": "run", "agent": agent, "version": version,
            "inputs": inputs, "provider": provider, "model": model,
            "replay_of": replay_of,
        })
        self._seq += 1

    def event(self, kind: str, name: str, args: dict, *, result=None,
              tokens_in: int | None = None, tokens_out: int | None = None,
              cost_usd: float | None = None) -> None:
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        line: dict = {
            "seq": self._seq, "ts": datetime.now(UTC).isoformat(),
            "kind": kind, "name": name, "args": args,
            "result_digest": "sha256:"
            + hashlib.sha256(serialized.encode()).hexdigest(),
        }
        if len(serialized) <= TRACE_RESULT_INLINE_MAX:
            line["result"] = serialized
        else:
            line["truncated"] = True
            line["result_preview"] = serialized[:TRACE_PREVIEW_CHARS]
        for k, v in (("tokens_in", tokens_in), ("tokens_out", tokens_out),
                     ("cost_usd", cost_usd)):
            if v is not None:
                line[k] = v
        self._emit(line)
        self._seq += 1

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def prune_traces(config: TiroConfig) -> None:
    """Age-then-size trace retention (OPEN decision 7). Best-effort: any
    failure is logged and swallowed — pruning must never fail a run. Never
    touches agent_runs rows (status stays queryable; a pruned trace reads
    as 'expired' at the API layer)."""
    try:
        tdir = traces_dir(config)
        if not tdir.exists():
            return
        cutoff = time.time() - config.agent_trace_retention_days * 86400
        files = sorted(tdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        kept = []
        for p in files:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
            else:
                kept.append(p)
        max_bytes = config.agent_trace_max_mb * 1024 * 1024
        total = sum(p.stat().st_size for p in kept)
        for p in kept:                        # oldest-mtime first (LRU)
            if total <= max_bytes:
                break
            total -= p.stat().st_size
            p.unlink(missing_ok=True)
    except Exception as e:
        logger.error("Trace pruning failed (non-fatal): %s", e)


class _ZeroCtx:
    """Stand-in accumulator for closing a row before a context exists."""
    tokens_in = 0
    tokens_out = 0
    cost_usd = 0.0


def _now_sql() -> str:
    # SQLite datetime('now')-comparable (doctor's stuck-run sweep relies on it).
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _open_run_row(config, run_uid, agent, inputs, provider, model, replay_of):
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            """INSERT INTO agent_runs
               (run_uid, agent_name, agent_version, started_at, status,
                provider, model, input_json, replay_of)
               VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)""",
            (run_uid, agent.name, agent.version, _now_sql(),
             provider, model, json.dumps(inputs, default=str), replay_of),
        )
        conn.commit()
    finally:
        conn.close()


def _close_run_row(config, run_uid, *, status, ctx, output_json=None,
                   citations=None, error=None):
    conn = get_connection(config.db_path)
    try:
        conn.execute(
            """UPDATE agent_runs
               SET status = ?, completed_at = ?, output_json = ?,
                   citations_json = ?, tokens_in = ?, tokens_out = ?,
                   cost_usd = ?, error = ?
               WHERE run_uid = ?""",
            (status, _now_sql(), output_json,
             json.dumps(citations) if citations is not None else None,
             ctx.tokens_in, ctx.tokens_out, ctx.cost_usd, error, run_uid),
        )
        conn.commit()
    finally:
        conn.close()


def _validate_inputs(agent, inputs: dict) -> None:
    declared = agent.inputs
    missing = set(declared) - set(inputs)
    extra = set(inputs) - set(declared)
    if missing:
        raise AgentRunError(
            f"{agent.name}: missing input(s) {sorted(missing)}")
    if extra:
        raise AgentRunError(
            f"{agent.name}: unexpected input(s) {sorted(extra)}")
    for key, typ in declared.items():
        if not isinstance(inputs[key], typ):
            raise AgentRunError(
                f"{agent.name}: input {key!r} expected {typ.__name__}, "
                f"got {type(inputs[key]).__name__}")


def run_agent(config: TiroConfig, name: str, inputs: dict, *,
              model_override: dict | None = None,
              replay_of: str | None = None) -> AgentResult:
    """Execute one agent run: validate -> lock -> row -> trace -> run ->
    close. Never raises anything but AgentRunError (original chained as
    __cause__; run_uid attached once a row exists). Spec §3 semantics."""
    from tiro import llm as llm_module
    from tiro.agents import registry
    from tiro.agents.context import RunContext
    from tiro.database import init_db

    registry.ensure_builtins()
    try:
        agent = registry.get(name)
    except KeyError:
        raise AgentRunError(f"unknown agent {name!r}") from None
    _validate_inputs(agent, inputs)

    # Provenance is structural (agent_runs row + trace file), so a run must
    # never fail merely because the caller's library was never `tiro init`-ed
    # (compat wrappers can be invoked against a bare TiroConfig in tests, and
    # in production the DB is always already migrated by app startup).
    # init_db() only touches a FRESH db (checks for the `articles` table),
    # so this is a cheap no-op once the library is real.
    init_db(config.db_path)

    if model_override:
        provider, model = model_override["provider"], model_override["model"]
    else:
        provider, model = llm_module.resolve_tier(config, agent.tier)

    with _RUN_LOCK:
        run_uid = new_ulid()
        _open_run_row(config, run_uid, agent, inputs, provider, model, replay_of)
        try:
            trace = TraceWriter(traces_dir(config) / f"{run_uid}.jsonl")
        except Exception as e:
            # Provenance is structural: no trace file, no run (OPEN decision 13).
            _close_run_row(config, run_uid, status="error", ctx=_ZeroCtx(),
                           error=f"trace open failed: {e}")
            raise AgentRunError(f"{name}: trace open failed: {e}",
                                run_uid=run_uid) from e
        ctx = _ZeroCtx()
        try:
            trace.header(agent=agent.name, version=agent.version,
                         inputs=inputs, provider=provider, model=model,
                         replay_of=replay_of)
            ctx = RunContext(config, trace=trace, run_uid=run_uid,
                             model_override=model_override)
            result = agent.run(ctx, **inputs)
            if not isinstance(result.outputs, agent.output_model):
                raise AgentRunError(
                    f"{name}: run() returned {type(result.outputs).__name__}, "
                    f"declared output_model is {agent.output_model.__name__}")
        except Exception as e:
            trace.close()
            _close_run_row(config, run_uid, status="error", ctx=ctx,
                           error=str(e))
            prune_traces(config)
            if isinstance(e, AgentRunError):
                e.run_uid = run_uid
                raise
            raise AgentRunError(f"{name}: {e}", run_uid=run_uid) from e
        trace.close()
        _close_run_row(
            config, run_uid, status="ok", ctx=ctx,
            output_json=result.outputs.model_dump_json(),
            citations=result.citations,
        )
        prune_traces(config)
        return result
