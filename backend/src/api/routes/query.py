"""Query and exploration REST routes.

GET    /spaces/{id}/components              — list/search components
GET    /spaces/{id}/components/{name}       — find component by name
GET    /spaces/{id}/components/{name}/flow  — trace execution flow
GET    /spaces/{id}/components/{name}/deps  — show dependencies
GET    /spaces/{id}/search                  — semantic search
"""

import logging
from collections.abc import Iterable

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ...core.models import derive_display_name
from ...service.codekb_service import CodeKBService, SpaceNotFoundError
from ..auth import require_current_user
from ..gates import gate_read

logger = logging.getLogger(__name__)

router = APIRouter()


# ── UI-only display_name enrichment ──────────────────────────────────────
#
# ``display_name`` is a human-friendly, parent-qualified label derived from the
# component id. It is attached HERE, in the REST layer, and NOWHERE at or below
# the shared service: the service, the MCP tools and the persisted knowledge
# base never carry it. The reason is strict — an LLM driving the MCP tools must
# not key tool chaining on it, because the label is not unique and is not a
# valid lookup key. Only ``id`` is the key. These helpers are deliberately
# private to this module so the enrichment cannot leak into a service response.


def _display_name_for(component: dict) -> str:
    """Return the UI-only display label for a serialized component dict."""
    attributes = component.get("attributes") or {}
    return derive_display_name(
        component.get("id", ""),
        component.get("language", ""),
        parent_component=component.get("parent_component"),
        accessor_kind=attributes.get("accessor_kind"),
    )


def _with_display_name(component: dict) -> dict:
    """Attach ``display_name`` to a component dict in place and return it."""
    component["display_name"] = _display_name_for(component)
    return component


def _display_names_for_ids(ids: Iterable[str], meta_by_id: dict[str, dict]) -> dict[str, str]:
    """Build an id -> display_name lookup for referenced component ids.

    Flow and dependency responses reference components by bare id string, so the
    label is supplied as a sibling lookup rather than attached to the string.
    Metadata (language / parent / accessor kind) comes from the space's own
    component list so Terraform and Dart accessor ids render exactly as they do
    everywhere else; an id absent from the catalog falls back to id-only
    derivation.
    """
    names: dict[str, str] = {}
    for cid in ids:
        if not cid or cid in names:
            continue
        meta = meta_by_id.get(cid)
        names[cid] = _display_name_for(meta) if meta is not None else derive_display_name(cid, "")
    return names


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
            results = codekb_service.search_components(space_id, pattern, component_type=type)
        else:
            results = codekb_service.list_components(space_id, component_type=type)
        return [_with_display_name(component) for component in results]
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

    return _with_display_name(result)


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

    meta_by_id = {c["id"]: c for c in codekb_service.list_components(space_id)}
    referenced = [result.get("start_component")]
    for step in result.get("flow_steps", []):
        referenced.append(step.get("component"))
        referenced.append(step.get("caller"))
    result["display_names"] = _display_names_for_ids(referenced, meta_by_id)
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

    meta_by_id = {c["id"]: c for c in codekb_service.list_components(space_id)}
    referenced = [result.get("component")]
    for key in (
        "direct_dependencies",
        "transitive_dependencies",
        "circular_dependencies",
        "dependents",
    ):
        referenced.extend(result.get(key, []))
    result["display_names"] = _display_names_for_ids(referenced, meta_by_id)
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
