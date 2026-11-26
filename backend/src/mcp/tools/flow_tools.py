"""Flow analysis and dependency tools — delegates to CodeKBService."""

import asyncio
import json
from typing import Dict, Any
from mcp.server.fastmcp import Context

from ...service.codekb_service import CodeKBService
from ...core.config import require_config
from ...core.prompt_spotlighting import wrap_external, wrap_external_fields
from .common import gate_space_read, parse_component_input, with_error_handling


@with_error_handling
async def trace_flow(
    component: str, depth: int = 10, raw_output: bool = False, ctx: Context = None
) -> str:
    """Trace the complete execution flow starting from a function, showing all function calls it makes.

    Use this to understand what happens when a function executes: which functions it calls,
    which functions those call, etc. Shows the full call chain up to the specified depth.
    Essential for understanding execution paths and dependencies.

    Args:
        component: Fully qualified component name with space prefix (e.g., 'my-space::module.function').
                   Use the IDs returned by get_component_from_description directly.
        depth: Maximum call depth to trace (default 10, higher = more complete but slower)
        raw_output: If True, returns machine-readable format; if False, human-readable
    """
    codekb_service = CodeKBService.instance()
    sid, component_name = parse_component_input(component)
    await asyncio.to_thread(gate_space_read, ctx, sid)
    result = codekb_service.trace_flow(sid, component_name, depth)
    if result is None:
        return f"Component '{component_name}' not found or no flow steps detected."
    if raw_output:
        return json.dumps(wrap_external_fields(result), default=str)
    return _format_flow(result)


@with_error_handling
async def show_dependencies(component: str, raw_output: bool = False, ctx: Context = None) -> str:
    """Show what a component depends on (outgoing dependencies) and what depends on it (incoming).

    Use this to understand a component's relationships: what it calls (dependencies) and
    what calls it (dependents). Useful for impact analysis and understanding coupling.

    Args:
        component: Fully qualified component name with space prefix (e.g., 'my-space::module.function').
                   Use the IDs returned by get_component_from_description directly.
        raw_output: If True, returns machine-readable format; if False, human-readable
    """
    codekb_service = CodeKBService.instance()
    sid, component_name = parse_component_input(component)
    await asyncio.to_thread(gate_space_read, ctx, sid)
    result = codekb_service.show_dependencies(sid, component_name)
    if result is None:
        return f"Component '{component_name}' not found."
    if raw_output:
        return json.dumps(wrap_external_fields(result), default=str)
    return _format_dependencies(result)


@with_error_handling
async def get_call_graph_with_source(
    component_id: str, depth: int | None = None, raw_output: bool = False, ctx: Context = None
) -> str:
    """Get the complete call graph starting from a function WITH full source code for every function in the graph.

    Use this when you need to understand not just WHAT functions are called, but also HOW they work.
    Returns the call graph structure plus the complete source code of every function involved.
    Perfect for deep analysis, AI summarization, or understanding complex workflows.

    Args:
        component_id: Fully qualified component name with space prefix (e.g., 'my-space::module.function').
                      Use the IDs returned by get_component_from_description directly.
        depth: Maximum call depth to include (default: the server's configured depth,
               the same one summarization uses; higher = more complete call tree)
        raw_output: If True, returns compact format; if False, formatted with indentation
    """
    codekb_service = CodeKBService.instance()
    sid, component_name = parse_component_input(component_id)
    await asyncio.to_thread(gate_space_read, ctx, sid)
    if depth is None:
        depth = int(require_config("analysis.call_graph_depth"))
    call_graph = codekb_service.get_call_graph_with_source(sid, component_name, depth)
    if call_graph is None:
        return f"Component '{component_name}' not found or is not a function/method."

    output = [f"Call Graph for: {component_name}", "=" * 60, ""]
    seen = set()
    _format_call_graph(call_graph, output, indent=0, raw=raw_output, seen=seen)
    return "\n".join(output)


# ── formatting helpers ──────────────────────────────────────────────────


def _format_flow(flow: dict) -> str:
    """Format a flow dict for human-readable output."""
    lines = [
        "Execution Flow:",
        f"   Start: {flow.get('start_component', '?')}",
        f"   Steps: {len(flow.get('flow_steps', []))}",
        f"   Depth: {flow.get('total_depth', 0)}",
    ]
    circular = flow.get("circular_calls", [])
    if circular:
        lines.append(f"   Circular calls: {', '.join(circular)}")
    steps = flow.get("flow_steps", [])
    lines.append("\n   Flow Steps:")
    for i, step in enumerate(steps[:15]):
        indent = "   " + "  " * step.get("depth", 0)
        # An annotation step is implicit: the component is a handler
        # the framework runs for the caller, not a call in the caller's body.
        arrow = (
            "--(advised by, implicit)-->"
            if step.get("call_type") == "annotation"
            else "-->"
        )
        lines.append(
            f"   {i + 1:2d}. {indent}{step.get('caller', '?')} {arrow} {step.get('component', '?')}"
        )
    if len(steps) > 15:
        lines.append(f"   ... and {len(steps) - 15} more steps")
    return "\n".join(lines)


def _format_dependencies(deps: dict) -> str:
    """Format a dependency dict for human-readable output."""
    direct = deps.get("direct_dependencies", [])
    transitive = deps.get("transitive_dependencies", [])
    lines = [
        f"Dependencies for {deps.get('component', '?')}:",
        f"   Direct: {len(direct)}",
        f"   Transitive: {len(transitive)}",
        f"   Depth: {deps.get('dependency_depth', 0)}",
        f"   Coupling: {deps.get('coupling_strength', '?')}",
    ]
    if direct:
        lines.append("\n   Direct Dependencies:")
        for dep in direct[:10]:
            lines.append(f"     {dep}")
        if len(direct) > 10:
            lines.append(f"     ... and {len(direct) - 10} more")
    advised_by = deps.get("advised_by", [])
    if advised_by:
        lines.append("\n   Advised by (implicit annotation handlers, not direct calls):")
        for dep in advised_by[:10]:
            lines.append(f"     {dep}")
        if len(advised_by) > 10:
            lines.append(f"     ... and {len(advised_by) - 10} more")
    advises = deps.get("advises", [])
    if advises:
        lines.append("\n   Advises (methods this handler implicitly wraps):")
        for dep in advises[:10]:
            lines.append(f"     {dep}")
        if len(advises) > 10:
            lines.append(f"     ... and {len(advises) - 10} more")
    return "\n".join(lines)


def _format_call_graph(
    node: Dict[str, Any], output: list, indent: int = 0, raw: bool = False, seen: set = None
):
    """Format call graph for display, skipping duplicate functions."""
    if seen is None:
        seen = set()

    func_id = node["id"]
    if func_id in seen:
        return
    seen.add(func_id)

    prefix = "  " * indent
    # Rendered over an implicit annotation edge: the node is a handler
    # the framework runs for the annotated caller above, not a direct callee.
    is_annotation_edge = node.get("edge_kind") == "annotation"
    # Rendered under a direct call (the parent visibly calls it) while ALSO
    # being an annotation handler elsewhere in this graph: a non-exclusive
    # note that never denies the direct call. Both markers are constant text.
    is_dual_role = node.get("also_annotation_handler") and not is_annotation_edge
    # Mirror direction: rendered under an annotation edge while some node in
    # this graph ALSO calls it directly — the rendering edge is still the
    # implicit one, so this is an additional note, not a replacement.
    also_called_directly = node.get("also_called_directly") and is_annotation_edge
    if raw:
        header = f"{prefix}{node['id']} ({node['type']})"
        if is_annotation_edge:
            header += " [advised by: implicit annotation handler, not a direct call]"
            if also_called_directly:
                header += " [also called directly elsewhere in this graph]"
        elif is_dual_role:
            header += " [also an implicit annotation handler for annotated methods in this graph]"
        output.append(header)
        output.append(f"{prefix}SOURCE:")
        for line in wrap_external(node["source"]).split("\n"):
            output.append(f"{prefix}  {line}")
    else:
        output.append(f"{prefix}├─ {node['id']}")
        output.append(f"{prefix}│  Type: {node['type']}")
        if is_annotation_edge:
            output.append(
                f"{prefix}│  Relation: advised by (implicit annotation handler, not a direct call)"
            )
            if also_called_directly:
                output.append(
                    f"{prefix}│  Note: also called directly elsewhere in this graph"
                )
        elif is_dual_role:
            output.append(
                f"{prefix}│  Note: also an implicit annotation handler for annotated methods in this graph"
            )
        output.append(f"{prefix}│  Location: {node['file']}:{node['lines']}")
        output.append(f"{prefix}│  Source:")
        source_lines = node["source"].split("\n")
        # Wrap only the shown slice so the markers stay balanced; the truncation
        # note is our own text and stays outside them.
        for line in wrap_external("\n".join(source_lines[:10])).split("\n"):
            output.append(f"{prefix}│    {line}")
        if len(source_lines) > 10:
            output.append(f"{prefix}│    ... ({len(source_lines) - 10} more lines)")

    output.append("")
    for child in node.get("calls", []):
        _format_call_graph(child, output, indent + 1, raw, seen)
