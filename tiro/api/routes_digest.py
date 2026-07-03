"""Digest API routes."""

import asyncio
import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tiro.intelligence.digest import (
    generate_digest,
    get_cached_digest,
    get_digest_by_date,
    get_digest_dates,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/digest", tags=["digest"])


class DigestGenerateRequest(BaseModel):
    unread_only: bool = False


@router.get("/today")
async def digest_today(request: Request):
    """Return today's cached digest (all three variants). Pure read —
    generation is POST /api/digest/today (M4b: no side effects via GET)."""
    config = request.app.state.config
    today = date.today().isoformat()
    cached = await asyncio.to_thread(get_cached_digest, config, today)
    if cached:
        logger.info("Returning cached digest for %s", today)
        return {"success": True, "data": cached, "cached": True}
    raise HTTPException(status_code=404, detail="No digest cached yet")


@router.post("/today")
async def digest_generate(request: Request, payload: DigestGenerateRequest | None = None):
    """Generate a fresh digest (all three variants) with Opus."""
    config = request.app.state.config
    unread_only = payload.unread_only if payload else False

    # Offloaded to thread — Opus call can take 10-30s
    try:
        result = await asyncio.to_thread(generate_digest, config, unread_only=unread_only)
        return {"success": True, "data": result, "cached": False}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Digest generation failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Digest generation failed")


@router.get("/today/{digest_type}")
async def digest_by_type(digest_type: str, request: Request):
    """Get a specific cached digest variant: ranked, by_topic, or by_entity.
    Pure read — 404 on miss; POST /api/digest/today to generate."""
    if digest_type not in ("ranked", "by_topic", "by_entity"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid digest type '{digest_type}'. Must be: ranked, by_topic, by_entity",
        )

    config = request.app.state.config
    today = date.today().isoformat()
    cached = await asyncio.to_thread(get_cached_digest, config, today, digest_type)
    if cached:
        return {"success": True, "data": cached, "cached": True}
    raise HTTPException(status_code=404, detail=f"No cached '{digest_type}' digest")


@router.get("/history")
async def digest_history(request: Request):
    """Get list of dates with cached digests."""
    config = request.app.state.config
    dates = await asyncio.to_thread(get_digest_dates, config)
    return {"success": True, "data": dates}


@router.get("/date/{target_date}")
async def digest_by_date_endpoint(target_date: str, request: Request):
    """Get cached digest for a specific date."""
    config = request.app.state.config
    result = await asyncio.to_thread(get_digest_by_date, config, target_date)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No digest found for {target_date}")
    return {"success": True, "data": result}
