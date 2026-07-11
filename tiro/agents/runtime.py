"""Agent run loop: run rows, JSONL traces, retention (Phase 6 K1).

Traces are files, runs are an index (spec §1): the full ordered event
stream lives at {library}/agents/traces/{run_uid}.jsonl; SQLite agent_runs
holds the queryable summary. Pruning deletes trace FILES only, never rows.
"""

import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from tiro.config import TiroConfig

logger = logging.getLogger(__name__)

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
