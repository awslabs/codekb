"""Query and component discovery tools — delegates to CodeKBService."""

import asyncio
import json
from typing import Optional
from mcp.server.fastmcp import Context

from ...service.codekb_service import CodeKBService
from ...core.models import SPACE_SEP
from ...core.prompt_spotlighting import wrap_external, wrap_external_fields
from .common import (
    filter_readable_spaces,
    gate_space_read,
    parse_component_input,
    with_error_handling,
)


@with_error_handling
async def find_component(name: str, raw_output: bool = False, ctx: Context = None) -> str:
    """Find a specific component by its exact name (case-sensitive).

    Use this when you know the exact name of a function, class, method, or variable.
    Returns detailed information including file path, line numbers, type, and source code.

    Args:
        name: Fully qualified component name with space prefix (e.g., 'my-space::UserService.authenticate').
              Use the IDs returned by get_component_from_description directly.
        raw_output: If True, returns machine-readable format; if False, human-readable
    """
    codekb_service = CodeKBService.instance()
    sid, component_name = parse_component_input(name)
    await asyncio.to_thread(gate_space_read, ctx, sid)
    result = codekb_service.find_component(sid, component_name)
    if result is None:
        return f"Component '{component_name}' not found."
    return (
        json.dumps(wrap_external_fields(result), default=str)
        if raw_output
        else _format_component(result)
    )


@with_error_handling
async def search_components(pattern: str, raw_output: bool = False, ctx: Context = None) -> str:
    """Search for components using wildcard patterns when you don't know the exact name.

    Use this for fuzzy searching when you only know part of the name. Supports '*' wildcard.
    Examples: 'User*' finds UserService, UserController; '*authenticate*' finds all auth functions.

    Args:
        pattern: Search pattern with space prefix (e.g., 'my-space::User*', 'my-space::*Service').
                 Use the space_id from get_component_from_description results as the prefix.
        raw_output: If True, returns machine-readable format; if False, human-readable
    """
    codekb_service = CodeKBService.instance()
    sid, qualified = parse_component_input(pattern)
    # The "space_id::" prefix only selects the space. Match the rest against
    # component ids, the same way the UI route does.
    search_pattern = qualified.split(SPACE_SEP, 1)[1]
    await asyncio.to_thread(gate_space_read, ctx, sid)
    results = codekb_service.search_components(sid, search_pattern)
    if not results:
        return f"No components matching '{pattern}'"
    if raw_output:
        return json.dumps(wrap_external_fields(results), default=str)
    return _format_component_list(results)


@with_error_handling
async def list_components(
    space_id: str,
    component_type: Optional[str] = None,
    raw_output: bool = False,
    ctx: Context = None,
) -> str:
    """List all components in the codebase, optionally filtered by type.

    Use this to get an overview of what's in the codebase or to see all functions, classes, etc.
    Without a type filter, returns all components. With a type, returns only that type.

    Args:
        space_id: Space identifier to list components from (required).
        component_type: Optional filter - 'function', 'class', 'method', 'variable', or None for all
        raw_output: If True, returns machine-readable format; if False, human-readable
    """
    codekb_service = CodeKBService.instance()
    await asyncio.to_thread(gate_space_read, ctx, space_id)
    results = codekb_service.list_components(space_id, component_type=component_type)
    if raw_output:
        return json.dumps(wrap_external_fields(results), default=str)
    return _format_component_list(results)


@with_error_handling
async def show_component(name: str, raw_output: bool = False, ctx: Context = None) -> str:
    """Display comprehensive details about a specific component including source code and metadata.

    Use this to get full information about a component: its type, location, parameters, return type,
    complete source code, and any stored business descriptions. More detailed than find_component.

    Args:
        name: Fully qualified component name with space prefix (e.g., 'my-space::MyClass.method').
              Use the IDs returned by get_component_from_description directly.
        raw_output: If True, returns machine-readable format; if False, human-readable
    """
    codekb_service = CodeKBService.instance()
    sid, component_name = parse_component_input(name)
    await asyncio.to_thread(gate_space_read, ctx, sid)
    result = codekb_service.find_component(sid, component_name)
    if result is None:
        return f"Component '{component_name}' not found."
    if raw_output:
        return json.dumps(wrap_external_fields(result), default=str)
    return _format_component_detail(result)


@with_error_handling
async def get_stats(space_id: str, raw_output: bool = False, ctx: Context = None) -> str:
    """Get overall statistics about the analyzed codebase.

    Use this to understand the scope and composition of the codebase: total number of components,
    breakdown by type (functions, classes, methods, variables), files analyzed, and call graph size.

    Args:
        space_id: Space identifier to get statistics for (required).
        raw_output: If True, returns machine-readable format; if False, human-readable
    """
    codekb_service = CodeKBService.instance()
    await asyncio.to_thread(gate_space_read, ctx, space_id)
    stats = codekb_service.get_stats(space_id)
    if raw_output:
        return json.dumps(wrap_external_fields(stats), default=str)
    return _format_stats(stats)


# ── formatting helpers ──────────────────────────────────────────────────


def _format_component(comp: dict) -> str:
    """Format a single component dict for human-readable output."""
    lines = [
        f"Component: {comp.get('name', 'unknown')}",
        f"   Type: {comp.get('type', 'unknown')}",
        f"   File: {comp.get('file_path', 'unknown')}",
        f"   Lines: {comp.get('line_number', '?')}-{comp.get('end_line_number', '?')}",
    ]
    desc = comp.get("business_behavior", {}).get("description")
    if desc:
        lines.append(f"   Purpose: {wrap_external(desc)}")
    return "\n".join(lines)


def _format_component_list(components: list[dict]) -> str:
    """Format a list of component dicts for human-readable output."""
    lines = [f"Components ({len(components)}):"]
    for i, comp in enumerate(components[:20]):
        lines.append(
            f"   {i + 1:2d}. {comp.get('name', '?')} ({comp.get('type', '?')}) - {comp.get('file_path', '?')}"
        )
    if len(components) > 20:
        lines.append(f"   ... and {len(components) - 20} more")
    return "\n".join(lines)


def _format_component_detail(comp: dict) -> str:
    """Format detailed component info for human-readable output."""
    lines = [
        f"Component: {comp.get('name', 'unknown')}",
        f"   Type: {comp.get('type', 'unknown')}",
        f"   File: {comp.get('file_path', 'unknown')}",
        f"   Lines: {comp.get('line_number', '?')}-{comp.get('end_line_number', '?')}",
    ]
    desc = comp.get("business_behavior", {}).get("description")
    if desc:
        lines.append(f"   Purpose: {wrap_external(desc)}")
    source = comp.get("source_code")
    if source:
        lines.append(f"\n   Source:\n{wrap_external(source)}")
    return "\n".join(lines)


def _format_stats(stats: dict) -> str:
    """Format stats dict for human-readable output."""
    lines = [
        "Analysis Summary:",
        f"   Total components: {stats.get('total_components', 0)}",
        f"   Files analyzed: {stats.get('files_analyzed', 0)}",
    ]
    by_type = stats.get("components_by_type", {})
    if by_type:
        lines.append("   Components by type:")
        for comp_type, count in by_type.items():
            lines.append(f"     {comp_type}: {count}")
    return "\n".join(lines)


# ── space & hyperspace tools ────────────────────────────────────────────


@with_error_handling
async def list_spaces(ctx: Context = None) -> str:
    """List all spaces by reading lightweight status.json files from S3.

    Use this to get an overview of all available spaces in the system.
    Returns space_id, status, component_count, languages, and other metadata for each space.
    """
    codekb_service = CodeKBService.instance()
    # Off the loop: list_spaces() is an S3 read and filtering runs can_read per space.
    results = await asyncio.to_thread(
        lambda: filter_readable_spaces(ctx, codekb_service.list_spaces())
    )
    return json.dumps(wrap_external_fields(results), default=str)


@with_error_handling
async def list_hyperspaces() -> str:
    """List all hyperspaces from S3.

    Use this to get an overview of all available hyperspaces (named groups of 1 or more spaces).
    Returns hyperspace_id, name, space_ids, and creation metadata for each hyperspace.
    """
    codekb_service = CodeKBService.instance()
    results = codekb_service.list_hyperspaces()
    return json.dumps(wrap_external_fields(results), default=str)


@with_error_handling
async def get_space_details(space_id: str, ctx: Context = None) -> str:
    """Get detailed information about a specific space.

    Returns full space metadata including statistics, git info, languages,
    component counts, and vector store status.

    Args:
        space_id: Unique identifier for the space (e.g., 'my-project').
    """
    codekb_service = CodeKBService.instance()
    await asyncio.to_thread(gate_space_read, ctx, space_id)
    result = codekb_service.get_space(space_id)
    return json.dumps(wrap_external_fields(result), default=str)


@with_error_handling
async def get_hyperspace_details(hyperspace_id: str) -> str:
    """Get detailed information about a specific hyperspace, enriched with space summaries.

    Returns hyperspace config along with current status and component counts
    for each member space.

    Args:
        hyperspace_id: Unique identifier for the hyperspace.
    """
    # Details are signed-in — parity with REST GET /hyperspaces/{id}. Only the
    # member CONTENT paths (overview, semantic search) are gated; membership
    # metadata (ids, status, counts) is not content.
    codekb_service = CodeKBService.instance()
    return json.dumps(
        wrap_external_fields(codekb_service.get_hyperspace(hyperspace_id)), default=str
    )
