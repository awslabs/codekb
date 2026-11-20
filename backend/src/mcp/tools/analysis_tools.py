"""Analysis and loading tools for MCP server — delegates to CodeKBService."""

import asyncio
import logging

from mcp.server.fastmcp import Context

from ...service.codekb_service import CodeKBService
from .common import gate_space_read, with_error_handling

logger = logging.getLogger(__name__)


@with_error_handling
async def load_knowledge_base(space_id: str, ctx: Context = None) -> str:
    """Verify a previously saved knowledge base and report its summary.

    Confirms the space exists (on disk or S3) and returns its component/statistics
    summary.

    Args:
        space_id: Unique identifier for the space (used to locate the saved knowledge base)
    """
    # READ gate first, before the broad try below swallows it into a generic message.
    await asyncio.to_thread(gate_space_read, ctx, space_id)
    try:
        codekb_service = CodeKBService.instance()

        # Verify the space loads successfully
        space_info = codekb_service.get_space(space_id)

        component_count = space_info.get("component_count", 0)
        stats = space_info.get("statistics", {})
        response = "Knowledge base loaded!\n\n"
        response += f"Space: {space_id}\n"
        response += f"Components: {component_count}\n"
        response += f"Files: {stats.get('files_analyzed', 0)}\n"

        languages = list(stats.get("components_by_language", {}).keys())
        if languages:
            response += f"Languages: {', '.join(languages)}\n"

        response += f"Call graph entries: {stats.get('call_graph_entries', 0)}\n"
        logger.info(f"Loaded knowledge base for space '{space_id}': {component_count} components")
        return response
    except Exception as e:
        logger.error(f"Failed to load knowledge base '{space_id}': {e}", exc_info=True)
        return f"Failed to load knowledge base: {str(e)}. Analyze the space via the REST API first."
