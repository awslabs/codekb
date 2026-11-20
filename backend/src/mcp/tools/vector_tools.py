"""Vector store and semantic search tools — delegates to CodeKBService."""

import asyncio
import json
import logging
from typing import Optional
from mcp.server.fastmcp import Context

from ...service.codekb_service import CodeKBService
from ...authz.errors import ReadDenied
from ...core.logging_utils import format_payload
from ...core.prompt_spotlighting import wrap_external_fields
from .common import (
    NO_ACCESS_MESSAGE,
    gate_hyperspace_read,
    preload_space_statuses,
    resolve_search_scope,
    with_error_handling,
)

logger = logging.getLogger(__name__)


@with_error_handling
async def get_component_from_description(
    query: str,
    n_results: Optional[int] = None,
    space_id: Optional[str] = None,
    space_ids: Optional[list[str]] = None,
    hyperspace_id: Optional[str] = None,
    ctx: Context = None,
) -> str:
    """PREFERRED TOOL — Start here when exploring or answering questions about a codebase.

    Semantic search for components by purpose, behavior, or domain concept. This is the
    fastest way to find relevant code when you have a natural-language question or topic
    (e.g., 'advanced operations', 'process payments', 'authentication flow').

    Use this BEFORE list_components, find_component, or show_component unless you already
    know the exact component name. Returns a JSON array of matches, each with:
    "id" (space-qualified, e.g. "my-space::module.function"), "description", "type",
    "file_path", and "space_id". To see implementation details or how a component is
    wired, call a follow-up tool on its "id": get_call_graph_with_source (full source of
    the call tree), show_component (single component), trace_flow, or show_dependencies.

    Scope control (pick ONE, or omit all to search across every space):
    - space_id: Search within a single space.
    - space_ids: Search across a specific list of spaces.
    - hyperspace_id: Search across all spaces in a hyperspace.

    Args:
        query: Natural language description of what you're looking for (e.g., 'process payments').
        n_results: Maximum number of matching components to return (default from config: storage.s3_vectors.default_top_k).
        space_id: Optional single space identifier to search within.
        space_ids: Optional list of space identifiers to search across.
        hyperspace_id: Optional hyperspace identifier — resolves to its member spaces automatically.
    """
    codekb_service = CodeKBService.instance()

    # Resolve the requested scope to the caller's readable space ids.
    if hyperspace_id:
        # Prime the memo with the statuses get_hyperspace just fetched, so the
        # gate below doesn't re-read each member from S3.
        sink: dict = {}
        cfg = await asyncio.to_thread(codekb_service.get_hyperspace, hyperspace_id, status_sink=sink)
        requested = cfg["space_ids"]
        await asyncio.to_thread(preload_space_statuses, ctx, sink)
        # All-or-nothing: refuse if any member is unreadable (gate names them).
        await asyncio.to_thread(gate_hyperspace_read, ctx, requested)
        resolved_ids = requested
    elif space_ids or space_id:
        requested = space_ids if space_ids else [space_id]
        # An explicit list is all-or-nothing (matching the hyperspace path, REST,
        # and the agent): refuse and name the unreadable spaces rather than
        # silently searching a readable subset.
        resolved_ids = await asyncio.to_thread(resolve_search_scope, ctx, requested)
        inaccessible = sorted(set(requested) - set(resolved_ids or []))
        if inaccessible:
            raise ReadDenied(
                "You don't have access to all the requested spaces: " + ", ".join(inaccessible)
            )
    else:
        # No scope → search the caller's full readable set.
        resolved_ids = await asyncio.to_thread(resolve_search_scope, ctx, None)

    # Empty list (not None) means authz is on and the caller can read nothing.
    if resolved_ids is not None and not resolved_ids:
        return NO_ACCESS_MESSAGE

    scope_str = f"{len(resolved_ids)} space(s)" if resolved_ids else "<all spaces>"
    logger.info(
        f"[mcp.get_component_from_description] query_chars={len(query)}, "
        f"n_results={n_results}, scope={scope_str}"
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"[mcp.get_component_from_description] query={format_payload(query)}")
    logger.debug(f"[mcp.get_component_from_description] resolved space_ids={resolved_ids}")

    results = await asyncio.to_thread(
        codekb_service.global_semantic_search,
        query,
        n_results=n_results,
        space_ids=resolved_ids,
    )

    logger.info(f"[mcp.get_component_from_description] returned {len(results)} result(s)")

    if not results:
        return f"No components found for query: '{query}'"

    output = []
    for result in results:
        result_id = result.get("key")
        metadata = result.get("metadata", {})
        result_space_id = metadata.get("space_id", "")

        # Surface the fully-qualified component id so follow-up tools
        # (get_call_graph_with_source, show_component, find_component) can be
        # called directly on it. The call graph and source are intentionally NOT
        # embedded here — fetch them on demand per hit via get_call_graph_with_source
        # to keep this response small.
        if result_id and "::" not in result_id and result_space_id:
            qualified_id = f"{result_space_id}::{result_id}"
        else:
            qualified_id = result_id

        output.append(
            {
                "id": qualified_id,
                "description": metadata.get("description", ""),
                "type": metadata.get("type", ""),
                "file_path": metadata.get("file_path", ""),
                "space_id": result_space_id,
            }
        )

    return json.dumps(wrap_external_fields(output), default=str)
