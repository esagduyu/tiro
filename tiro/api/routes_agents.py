"""Agent runtime API (Phase 6 K2): registry listing, run history, trace
streaming, manual runs, replay. All routes require auth (registered in
create_app's authed include_router loop — route-walk covered)."""

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tiro.agents import registry
from tiro.agents.base import AgentRunError
from tiro.agents.runtime import run_agent, traces_dir
from tiro.database import get_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])

RUN_STATUSES = {"running", "ok", "error"}


def _known_providers() -> set[str]:
    from tiro import llm

    return set(llm._BACKENDS)


def _row_to_dict(config, row) -> dict:
    d = dict(row)
    d["input"] = json.loads(d.pop("input_json") or "null")
    d["output"] = json.loads(d.pop("output_json") or "null")
    d["citations"] = json.loads(d.pop("citations_json") or "null")
    d["trace_available"] = (
        traces_dir(config) / f"{d['run_uid']}.jsonl").exists()
    return d


class ModelOverride(BaseModel):
    provider: str
    model: str


class ReplayRequest(BaseModel):
    model_override: ModelOverride | None = None


class ManualRunRequest(BaseModel):
    inputs: dict = {}


def _validate_override(override: ModelOverride | None) -> dict | None:
    if override is None:
        return None
    if override.provider not in _known_providers():
        raise HTTPException(
            status_code=400,
            detail=f"unknown provider '{override.provider}'")
    return {"provider": override.provider, "model": override.model}


async def _execute(config, name: str, inputs: dict,
                    model_override=None, replay_of=None) -> dict:
    """Run in a worker thread; failed runs come back as success=false with
    the recorded run_uid (an error run is a result, not an HTTP error)."""
    try:
        res = await asyncio.to_thread(
            run_agent, config, name, inputs,
            model_override=model_override, replay_of=replay_of)
    except AgentRunError as e:
        if e.run_uid is None:
            # pre-run failure: unknown agent (404 handled by callers) or
            # input validation -> caller bug
            logger.warning("agent run rejected pre-run for '%s': %s",
                            name, e)
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"success": False,
                "data": {"run_uid": e.run_uid, "status": "error"},
                "error": str(e)}
    return {"success": True, "data": {
        "run_uid": res.run_uid, "status": "ok",
        "output": res.outputs.model_dump(), "citations": res.citations,
        "tokens_in": res.tokens_in, "tokens_out": res.tokens_out,
        "cost_usd": res.cost_usd,
    }}


@router.get("")
async def list_agents(request: Request):
    """Registry + last run per agent."""
    config = request.app.state.config
    registry.ensure_builtins()
    conn = get_connection(config.db_path)
    try:
        # id (AUTOINCREMENT, monotonic under the global run lock) is the
        # true recency key -- started_at is only second-granularity and
        # two runs of the same agent can tie on it.
        rows = conn.execute("""
            SELECT * FROM agent_runs
            WHERE id IN (SELECT MAX(id) FROM agent_runs GROUP BY agent_name)
        """).fetchall()
    finally:
        conn.close()
    last_by_name = {row["agent_name"]: _row_to_dict(config, row)
                     for row in rows}
    data = [{
        "name": agent.name, "version": agent.version, "tier": agent.tier,
        "inputs": {k: t.__name__ for k, t in agent.inputs.items()},
        "last_run": last_by_name.get(agent.name),
    } for agent in registry.all_agents().values()]
    data.sort(key=lambda a: a["name"])
    return {"success": True, "data": data}


@router.get("/runs")
async def list_runs(request: Request, agent: str | None = None,
                     status: str | None = None, limit: int = 50,
                     offset: int = 0):
    config = request.app.state.config
    if status is not None and status not in RUN_STATUSES:
        raise HTTPException(status_code=400,
                             detail=f"invalid status '{status}'")
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    where, params = [], []
    if agent:
        where.append("agent_name = ?")
        params.append(agent)
    if status:
        where.append("status = ?")
        params.append(status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    conn = get_connection(config.db_path)
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM agent_runs {where_sql}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"""SELECT * FROM agent_runs {where_sql}
                ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
    finally:
        conn.close()
    return {"success": True, "data": {
        "runs": [_row_to_dict(config, r) for r in rows], "total": total,
    }}


@router.get("/runs/{run_uid}")
async def run_detail(run_uid: str, request: Request, trace: int = 0):
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM agent_runs WHERE run_uid = ?", (run_uid,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="run not found")
    if trace:
        path = traces_dir(config) / f"{run_uid}.jsonl"
        if not path.exists():
            raise HTTPException(status_code=404, detail="trace expired")
        return FileResponse(path, media_type="application/x-ndjson")
    return {"success": True, "data": _row_to_dict(config, row)}


@router.post("/runs/{run_uid}/replay")
async def replay_run(run_uid: str, request: Request,
                      payload: ReplayRequest | None = None):
    """Live re-execution (spec §2): fresh tool reads, new run row with
    replay_of set; the original run is never touched."""
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        row = conn.execute(
            "SELECT agent_name, input_json FROM agent_runs WHERE run_uid = ?",
            (run_uid,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="run not found")
    override = _validate_override(payload.model_override if payload else None)
    inputs = json.loads(row["input_json"] or "{}")
    return await _execute(config, row["agent_name"], inputs,
                           model_override=override, replay_of=run_uid)


@router.post("/{name}/run")
async def manual_run(name: str, request: Request, payload: ManualRunRequest):
    config = request.app.state.config
    registry.ensure_builtins()
    if name.startswith("persona:"):
        # Personas are files-as-truth (re-synced per run_agent); the friendly
        # 404 pre-check must warm them too or a cold process rejects a valid
        # persona that run_agent would happily execute (final-review I-1).
        from tiro.agents import personas

        personas.sync_registry(config)
    try:
        registry.get(name)
    except KeyError:
        raise HTTPException(status_code=404,
                             detail=f"unknown agent '{name}'") from None
    return await _execute(config, name, payload.inputs)
