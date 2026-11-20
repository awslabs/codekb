#!/usr/bin/env python3
"""MCP Server for Code Knowledge Base analysis.

Exposed over streamable HTTP, mounted inside the FastAPI app at ``/mcp``. Logging
and configuration are initialised once by the host app, not here.
"""

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from ..service.codekb_service import CodeKBService
from .tools.analysis_tools import load_knowledge_base
from .tools.query_tools import (
    find_component,
    search_components,
    list_components,
    show_component,
    get_stats,
    list_spaces,
    list_hyperspaces,
    get_space_details,
    get_hyperspace_details,
)
from .tools.flow_tools import trace_flow, show_dependencies, get_call_graph_with_source
from .tools.vector_tools import get_component_from_description

# Server-level usage guidance delivered to every MCP client (Kiro, Claude Code,
# Amazon Q, etc.) in the `initialize` handshake. Encodes the brownfield-change
# workflow CodeKB is built for: localize the exact edit sites, then map the full
# ripple impact — within and across repositories — at high precision and recall.
SERVER_INSTRUCTIONS = """\
CodeKB is a code knowledge base for understanding existing codebases. Each analyzed
repository is a "space"; related repositories are grouped into a "hyperspace". Every
component id is space-qualified as `space_id::component_name` — pass ids returned by
one tool directly into the next.

USE CODEKB FOR BROWNFIELD WORK. When fixing a defect or making a change in an existing
codebase, prefer these tools over blind file search: they localize the exact code to
change and reveal what else is affected. Blind grep misses dynamic/indirect usages and
cross-repo coupling; CodeKB is built to catch them.

GOLDEN RULES
- Start from meaning, not keywords: `get_component_from_description` is the entry point.
- Combine signals: semantic search gives RECALL (finds implicit/dynamic usages); the
  static call graph gives PRECISION (confirms real edges). Use both — never one alone.
- For impact analysis, call `show_dependencies` with raw_output=true. The default
  (human-readable) output omits the INCOMING "dependents" — the callers you need for
  ripple analysis. raw_output=true returns both directions.
- Verify before you act: read a candidate's source (`show_component` /
  `get_call_graph_with_source`) and confirm it matches the change intent before
  accepting it as an edit site. This is the precision gate.
- Check freshness: impact edges come from indexed analysis. If a space is not READY
  (see `list_spaces` / `get_space_details`), warn that results may be incomplete.

WORKFLOW FOR A BROWNFIELD CHANGE OR DEFECT FIX

Phase 0 — Scope. `list_spaces` / `list_hyperspaces`. If the change could cross repos,
`get_hyperspace_details` to enumerate member spaces, and pass `hyperspace_id` to
searches so recall spans every repo.

Phase 1 — Localize (recall first, then precision).
  1. `get_component_from_description` with a RICH natural-language description of the
     desired behavior or the defect (describe behavior, not identifiers). Search the
     whole hyperspace; keep results generous.
  2. Narrow with `find_component` (exact) or `search_components` (wildcard).
  3. `show_component` or `get_call_graph_with_source` to read source and CONFIRM each
     candidate truly matches the intent. Discard false positives. Output: the exact
     file:line edit sites.

Phase 2 — Ripple / impact (recall-critical).
  1. For each confirmed edit site, `show_dependencies` (raw_output=true) and walk the
     INCOMING "dependents" side. Recurse transitively (breadth-first) to build the
     blast radius; use `trace_flow` / `get_call_graph_with_source` to see how change
     propagates.
  2. CROSS-REPO: dependents are computed per-space, so a single call does NOT cross
     repository boundaries. For each other space in the hyperspace, (a) re-run
     `get_component_from_description` for usages that are not static call edges — API
     contracts, dynamic dispatch, events, config keys, serialized names — and
     (b) `show_dependencies` on any matches. This semantic pass is where cross-repo
     recall is won.

Phase 3 — Report. List the exact file:line locations to change, then a ranked impact
list — each affected component with the dependency path that justifies it, grouped by
repository. Call out anything that looks affected but could not be confirmed.
"""

# Initialize FastMCP server — disable DNS rebinding protection since the server
# sits behind an ALB/CloudFront with a dynamic hostname.
mcp = FastMCP(
    "aws-codekb",
    instructions=SERVER_INSTRUCTIONS,
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# Eagerly build the shared singleton so config errors surface at startup.
CodeKBService.instance()

# Register all tools
mcp.tool()(load_knowledge_base)
mcp.tool()(find_component)
mcp.tool()(search_components)
mcp.tool()(list_components)
mcp.tool()(show_component)
mcp.tool()(trace_flow)
mcp.tool()(show_dependencies)
mcp.tool()(get_call_graph_with_source)
mcp.tool()(get_stats)
mcp.tool()(list_spaces)
mcp.tool()(list_hyperspaces)
mcp.tool()(get_space_details)
mcp.tool()(get_hyperspace_details)
mcp.tool()(get_component_from_description)


def get_mcp_app():
    """Return a Starlette/ASGI app for streamable HTTP transport (mounted in FastAPI)."""
    return mcp.streamable_http_app()


def get_mcp_session_manager():
    """Return the MCP session manager (must be started via its lifespan)."""
    if mcp._session_manager is None:
        mcp.streamable_http_app()
    return mcp.session_manager
