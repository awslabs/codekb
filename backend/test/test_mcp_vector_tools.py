"""Tests for MCP vector_tools."""

import asyncio
from unittest.mock import MagicMock, patch

from src.mcp.tools.vector_tools import get_component_from_description


def test_empty_hyperspace_returns_descriptive_message():
    mock_service = MagicMock()
    mock_service.get_hyperspace.return_value = {"hyperspace_id": "empty-hs", "space_ids": []}

    with (
        patch("src.mcp.tools.vector_tools.CodeKBService.instance", return_value=mock_service),
        patch("src.mcp.tools.common.require_authorization", return_value=False),
    ):
        result = asyncio.run(get_component_from_description(query="auth", hyperspace_id="empty-hs"))

    assert result == "Hyperspace 'empty-hs' has no member spaces."
    mock_service.global_semantic_search.assert_not_called()
