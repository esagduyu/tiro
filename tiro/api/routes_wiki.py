"""Wiki API routes (Phase 1b Task 6).

Exposes the derived `wiki_pages` index for listing, individual page reads
(with a resolved citation map for Task 9's renderer), and on-demand
generate/regenerate through `tiro.wiki_gen`. Generation is offloaded via
`asyncio.to_thread` (the LLM call is synchronous and can take a while) and
guarded against duplicate concurrent requests for the same node/slug with a
simple module-level set -- mirrors the digest generation in-flight guard
idea, just server-side instead of the client-side `digestGenerating` flag.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tiro.database import get_connection
from tiro.wiki import read_page
from tiro.wiki_gen import (
    CITATION_RE,
    WikiGenerationError,
    generate_wiki_page,
    regenerate_wiki_page,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wiki", tags=["wiki"])

# In-flight guards: module-level sets, add/discard in try/finally around the
# to_thread call. Keyed by (node_type, node_id) for generate (a node has no
# page yet on first generation, so there's no slug to key on) and by slug for
# regenerate. A concurrent duplicate request gets 409 rather than racing two
# LLM calls against the same page file.
_generating_nodes: set[tuple[str, int]] = set()
_regenerating_slugs: set[str] = set()


class WikiGenerateRequest(BaseModel):
    node_type: str
    node_id: int


def _build_citations(config, body: str) -> dict[str, int]:
    """Resolve `[[stem|label]]` targets in `body` against `articles.markdown_path`
    stems (`markdown_path = stem + '.md'`). Unresolvable stems are simply
    absent from the map -- the renderer (Task 9) treats a missing entry as a
    dead/unlinked citation rather than erroring."""
    stems = list(dict.fromkeys(CITATION_RE.findall(body)))
    if not stems:
        return {}
    conn = get_connection(config.db_path)
    try:
        placeholders = ",".join("?" for _ in stems)
        paths = [f"{stem}.md" for stem in stems]
        rows = conn.execute(
            f"SELECT id, markdown_path FROM articles WHERE markdown_path IN ({placeholders})",
            paths,
        ).fetchall()
        by_path = {row["markdown_path"]: row["id"] for row in rows}
        citations: dict[str, int] = {}
        for stem in stems:
            article_id = by_path.get(f"{stem}.md")
            if article_id is not None:
                citations[stem] = article_id
        return citations
    finally:
        conn.close()


@router.get("")
async def list_wiki_pages(request: Request):
    """List all wiki pages from the derived index, ordered kind then title."""
    config = request.app.state.config
    conn = get_connection(config.db_path)
    try:
        rows = conn.execute(
            "SELECT id, uid, slug, kind, title, entity_type, status, "
            "source_count, updated_at FROM wiki_pages "
            "ORDER BY kind, title COLLATE NOCASE"
        ).fetchall()
        return {"success": True, "data": {"pages": [dict(r) for r in rows]}}
    finally:
        conn.close()


@router.get("/{slug:path}")
async def get_wiki_page(slug: str, request: Request):
    """Read a wiki page by slug, plus a resolved citation map (`{stem:
    article_id}`) for the renderer. 404 on an unknown slug."""
    config = request.app.state.config
    try:
        page = await asyncio.to_thread(read_page, config, slug)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    if page is None:
        raise HTTPException(status_code=404, detail=f"Unknown wiki page: {slug!r}")
    citations = await asyncio.to_thread(_build_citations, config, page["body"])
    return {"success": True, "data": {**page, "citations": citations}}


@router.post("/generate")
async def generate(payload: WikiGenerateRequest, request: Request):
    """Generate (create) or update the wiki page for an entity/tag node."""
    config = request.app.state.config
    key = (payload.node_type, payload.node_id)
    if key in _generating_nodes:
        raise HTTPException(
            status_code=409, detail="Generation already in progress for this node"
        )
    _generating_nodes.add(key)
    try:
        result = await asyncio.to_thread(
            generate_wiki_page, config, payload.node_type, payload.node_id
        )
        return {"success": True, "data": result}
    except WikiGenerationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    finally:
        _generating_nodes.discard(key)


@router.post("/{slug:path}/regenerate")
async def regenerate(slug: str, request: Request):
    """Regenerate an existing wiki page from scratch (prior body discarded,
    uid + pinned note preserved)."""
    config = request.app.state.config
    if slug in _regenerating_slugs:
        raise HTTPException(
            status_code=409, detail="Regeneration already in progress for this page"
        )
    _regenerating_slugs.add(slug)
    try:
        result = await asyncio.to_thread(regenerate_wiki_page, config, slug)
        return {"success": True, "data": result}
    except WikiGenerationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    finally:
        _regenerating_slugs.discard(slug)
