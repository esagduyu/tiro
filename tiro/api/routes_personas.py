"""Persona management + suggestion accept/dismiss routes (Phase 6 K3).

Suggestion payloads travel as JSON DATA end-to-end; the client renders
markdown through renderMarkdown (marked -> DOMPurify) -- sanitize-on-render.
Accept runs the standard validated writes via tiro.suggestions appliers.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from tiro.agents.personas import load_personas, personas_dir, sync_registry
from tiro.config import persist_config
from tiro.suggestions import (
    SUGGESTION_STATUSES,
    SuggestionApplyError,
    apply_suggestion,
    get_suggestion,
    list_suggestions,
    set_suggestion_status,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/personas")
async def get_personas(request: Request):
    config = request.app.state.config
    sync_registry(config)                       # also runs ensure_personas
    personas, errors = load_personas(config)
    disabled = set(config.personas_disabled or [])
    data = [{
        "slug": p.slug, "name": p.name, "version": p.version,
        "scope": p.scope, "schedule": p.schedule, "tier": p.tier,
        "output": p.output, "enabled": p.slug not in disabled,
        "error": None, "path": str(p.path),
    } for p in personas]
    data += [{
        "slug": slug, "name": slug, "version": None, "scope": None,
        "schedule": None, "tier": None, "output": None,
        "enabled": slug not in disabled, "error": err,
        "path": str(personas_dir(config) / f"{slug}.md"),
    } for slug, err in errors.items()]
    data.sort(key=lambda p: p["slug"])
    return {"success": True, "data": data}


def _set_enabled(request: Request, slug: str, enabled: bool):
    config = request.app.state.config
    known = {p.stem for p in personas_dir(config).glob("*.md")} \
        if personas_dir(config).exists() else set()
    if slug not in known:
        raise HTTPException(status_code=404, detail="unknown persona")
    disabled = [s for s in (config.personas_disabled or []) if s != slug]
    if not enabled:
        disabled.append(slug)
    config.personas_disabled = disabled
    persist_config(config, {"personas_disabled": disabled})
    sync_registry(config)
    return {"success": True, "data": {"slug": slug, "enabled": enabled}}


@router.post("/api/personas/{slug}/enable")
async def enable_persona(slug: str, request: Request):
    return _set_enabled(request, slug, True)


@router.post("/api/personas/{slug}/disable")
async def disable_persona(slug: str, request: Request):
    return _set_enabled(request, slug, False)


@router.get("/api/suggestions")
async def get_suggestions(request: Request, status: str | None = None,
                          article_id: int | None = None):
    config = request.app.state.config
    if status is not None and status not in SUGGESTION_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status")
    rows = list_suggestions(config, status=status, article_id=article_id)
    return {"success": True, "data": {"suggestions": rows}}


def _load_pending(config, uid: str) -> dict:
    suggestion = get_suggestion(config, uid)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="unknown suggestion")
    if suggestion["status"] != "pending":
        raise HTTPException(status_code=409, detail="already resolved")
    return suggestion


@router.post("/api/suggestions/{uid}/accept")
async def accept_suggestion(uid: str, request: Request):
    config = request.app.state.config
    suggestion = _load_pending(config, uid)
    try:
        applied = apply_suggestion(config, suggestion)   # apply FIRST
    except SuggestionApplyError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if not set_suggestion_status(config, uid, "accepted"):
        raise HTTPException(status_code=409, detail="already resolved")
    return {"success": True, "data": {"uid": uid, "applied": applied}}


@router.post("/api/suggestions/{uid}/dismiss")
async def dismiss_suggestion(uid: str, request: Request):
    config = request.app.state.config
    _load_pending(config, uid)
    if not set_suggestion_status(config, uid, "dismissed"):
        raise HTTPException(status_code=409, detail="already resolved")
    return {"success": True, "data": {"uid": uid}}
