"""API token management routes (session-authenticated; for Settings UI and CLI parity)."""

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from tiro import auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


class CreateTokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)


@router.get("")
async def list_tokens(request: Request):
    config = request.app.state.config
    return {"success": True, "data": auth.list_api_tokens(config.db_path)}


@router.post("")
async def create_token(body: CreateTokenBody, request: Request):
    config = request.app.state.config
    raw = auth.create_api_token(config.db_path, body.name)
    tokens = auth.list_api_tokens(config.db_path)
    new_id = tokens[-1]["id"] if tokens else None
    logger.info("API token created: %s", body.name)
    return {"success": True, "data": {"id": new_id, "name": body.name, "token": raw}}


@router.delete("/{token_id}")
async def revoke_token(token_id: int, request: Request):
    config = request.app.state.config
    if not auth.revoke_api_token(config.db_path, token_id):
        raise HTTPException(status_code=404, detail="Token not found")
    return {"success": True, "data": {"revoked": token_id}}
