"""Query and exploration REST routes.

GET    /spaces/{id}/components              — list/search components
GET    /spaces/{id}/components/{name}       — find component by name
GET    /spaces/{id}/components/{name}/flow  — trace execution flow
GET    /spaces/{id}/components/{name}/deps  — show dependencies
GET    /spaces/{id}/search                  — semantic search
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ...service.codekb_service import CodeKBService, SpaceNotFoundError
from ..auth import require_current_user
from ..gates import gate_read

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{space_id}/components")
async def list_or_search_components(
    request: Request,
    space_id: str,
    pattern: str | None = Query(None, description="Wildcard search pattern (e.g. User*, *Service)"),
    type: str | None = Query(None, description="Filter by type: function, class, method, variable"),
    _authz: None = Depends(gate_read),
) -> list[dict]:
    """List or search components.

    Use ?pattern= for wildcard search and ?type= to filter by component type.
    The two combine: a pattern search is narrowed to the requested type.
    """
    require_current_user(request)
    codekb_service = CodeKBService.instance()
    try:
        if pattern:
            return codekb_service.search_components(space_id, pattern, component_type=type)
        return codekb_service.list_components(space_id, component_type=type)
    except SpaceNotFoundError:
        raise HTTPException(status_code=404, detail=f"Space not found: {space_id}")


@router.get("/{space_id}/components/{name}")
async def find_component(
    request: Request, space_id: str, name: str, _authz: None = Depends(gate_read)
) -> dict:
    """Find a component by exact name."""
    require_current_user(request)
    codekb_service = CodeKBService.instance()
    try:
        result = codekb_service.find_component(space_id, name)
    except SpaceNotFoundError:
        raise HTTPException(status_code=404, detail=f"Space not found: {space_id}")
    if result is None:
        raise HTTPException(status_code=404, detail=f"Component not found: {name}")

    # Enrich with vector store description when available (replaces catalog heuristic)
    vector_desc = codekb_service.get_vector_description(space_id, result["id"])
    if vector_desc:
        result["business_behavior"]["description"] = vector_desc

    return result


@router.get("/{space_id}/components/{name}/flow")
async def trace_flow(
    request: Request,
    space_id: str,
    name: str,
    depth: int = Query(10, ge=1, le=50, description="Max call depth"),
    _authz: None = Depends(gate_read),
) -> dict:
    """Trace execution flow from a component."""
    require_current_user(request)
    codekb_service = CodeKBService.instance()
    try:
        result = codekb_service.trace_flow(space_id, name, depth)
    except SpaceNotFoundError:
        raise HTTPException(status_code=404, detail=f"Space not found: {space_id}")
    if result is None:
        raise HTTPException(status_code=404, detail=f"Component not found or no flow: {name}")
    return result


@router.get("/{space_id}/components/{name}/deps")
async def show_dependencies(
    request: Request, space_id: str, name: str, _authz: None = Depends(gate_read)
) -> dict:
    """Show dependencies for a component."""
    require_current_user(request)
    codekb_service = CodeKBService.instance()
    try:
        result = codekb_service.show_dependencies(space_id, name)
    except SpaceNotFoundError:
        raise HTTPException(status_code=404, detail=f"Space not found: {space_id}")
    if result is None:
        raise HTTPException(status_code=404, detail=f"Component not found: {name}")
    return result


@router.get("/{space_id}/search")
async def semantic_search(
    request: Request,
    space_id: str,
    q: str = Query(..., description="Natural language search query"),
    limit: int | None = Query(
        None,
        ge=1,
        le=50,
        description="Max results (defaults to storage.s3_vectors.default_top_k from config)",
    ),
    _authz: None = Depends(gate_read),
) -> list[dict]:
    """Semantic search across component descriptions using vector store."""
    user = require_current_user(request)
    codekb_service = CodeKBService.instance()
    logger.info(f"GET /spaces/{space_id}/search q={q!r}, limit={limit}, user={user.sub}")
    try:
        results = codekb_service.semantic_search(q, n_results=limit, space_id=space_id)
        logger.info(f"GET /spaces/{space_id}/search returned {len(results)} result(s) for q={q!r}")
        return results
    except SpaceNotFoundError:
        logger.info(f"Space not found during search: {space_id}")
        raise HTTPException(status_code=404, detail=f"Space not found: {space_id}")
    except Exception as e:
        logger.error(f"Semantic search failed for space '{space_id}', q='{q}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Search failed")
