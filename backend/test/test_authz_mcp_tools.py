"""Phase 5 (§6.5): MCP tools enforce READ the same way REST does.

Tools are plain async functions, so we call them directly and patch the gate
seams in ``src.mcp.tools.common``. No MCP transport is spun up. The repo has no
pytest-asyncio, so coroutines are driven with ``asyncio.run``.
"""

import asyncio
import json
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from src.authz.errors import CredentialExpired, HyperspaceAccessDenied, ReadDenied
from src.mcp.tools import analysis_tools, query_tools, vector_tools


def _call(coro):
    return asyncio.run(coro)


def _actx():
    actx = MagicMock()
    actx.space_status.side_effect = lambda sid: {"space_id": sid, "visibility": "private"}
    return actx


def _env(*, svc=None, actx=None, extra=None):
    """Switch authz on and stub the identity + service seams in common."""
    svc = svc or MagicMock()
    actx = actx or _actx()
    stack = ExitStack()
    stack.enter_context(patch("src.mcp.tools.common.require_authorization", return_value=True))
    stack.enter_context(patch("src.mcp.tools.common._auth_context", return_value=actx))
    stack.enter_context(patch("src.service.codekb_service.CodeKBService.instance", return_value=svc))
    for target, kw in (extra or {}).items():
        stack.enter_context(patch(target, **kw))
    return stack, svc, actx


# ── single-space READ ────────────────────────────────────────────────────


def test_tool_denies_unreadable_space():
    svc = MagicMock()
    svc.read_status.return_value = {"space_id": "s1", "visibility": "private"}
    stack, svc, _ = _env(
        svc=svc, extra={"src.mcp.tools.common.repo_access.can_read": {"side_effect": ReadDenied()}}
    )
    with stack:
        out = _call(query_tools.find_component("s1::foo"))
    assert "do not have access" in out.lower()
    svc.find_component.assert_not_called()


def test_tool_allows_readable_space():
    svc = MagicMock()
    svc.read_status.return_value = {"space_id": "s1", "visibility": "private"}
    svc.find_component.return_value = {"name": "foo", "type": "function"}
    stack, svc, _ = _env(
        svc=svc, extra={"src.mcp.tools.common.repo_access.can_read": {"return_value": None}}
    )
    with stack:
        out = _call(query_tools.find_component("s1::foo"))
    assert "foo" in out
    svc.find_component.assert_called_once()


def test_tool_expired_token_names_the_page():
    svc = MagicMock()
    svc.read_status.return_value = {"space_id": "s1", "visibility": "private"}
    stack, svc, _ = _env(
        svc=svc,
        extra={
            "src.mcp.tools.common.repo_access.can_read": {
                "side_effect": CredentialExpired("github")
            }
        },
    )
    with stack:
        out = _call(query_tools.get_space_details("s1"))
    assert "expired" in out.lower()
    assert "/git-creds" in out


def test_switch_off_is_a_noop():
    svc = MagicMock()
    svc.find_component.return_value = {"name": "foo", "type": "function"}
    with (
        patch("src.mcp.tools.common.require_authorization", return_value=False),
        patch("src.service.codekb_service.CodeKBService.instance", return_value=svc),
    ):
        out = _call(query_tools.find_component("s1::foo"))
    assert "foo" in out
    svc.read_status.assert_not_called()  # gate short-circuited before any read


# ── list filtering ─────────────────────────────────────────────────────────


def test_list_spaces_filters_to_readable():
    svc = MagicMock()
    svc.list_spaces.return_value = [{"space_id": "s1"}, {"space_id": "s2"}]
    stack, svc, _ = _env(
        svc=svc,
        extra={
            "src.mcp.tools.common.repo_access.can_list": {
                "side_effect": lambda actx, s: s["space_id"] == "s1"
            }
        },
    )
    with stack:
        out = json.loads(_call(query_tools.list_spaces()))
    assert [s["space_id"] for s in out] == ["s1"]


def test_list_spaces_expired_token_keeps_public():
    svc = MagicMock()
    svc.list_spaces.return_value = [
        {"space_id": "pub", "visibility": "public"},
        {"space_id": "priv", "visibility": "private"},
    ]

    def gate(_actx, record):
        if record["visibility"] == "private":
            raise CredentialExpired("github")

    stack, svc, _ = _env(
        svc=svc,
        extra={
            "src.authz.repo_access.can_read": {"side_effect": gate},
            "src.authz.repo_access.require_authorization": {"return_value": True},
        },
    )
    with stack:
        out = json.loads(_call(query_tools.list_spaces()))
    assert [s["space_id"] for s in out] == ["pub"]


# ── hyperspace READ (all members) ──────────────────────────────────────────


def test_hyperspace_details_is_signed_in_not_gated():
    # Details are signed-in (parity with REST GET /hyperspaces/{id}); membership
    # metadata is returned without a per-member READ gate. Only content paths gate.
    svc = MagicMock()
    svc.get_hyperspace.return_value = {"hyperspace_id": "h1", "space_ids": ["s1", "s2"]}
    stack, svc, _ = _env(svc=svc)
    with stack:
        out = _call(query_tools.get_hyperspace_details("h1"))
    assert "h1" in out
    svc.get_hyperspace.assert_called_once_with("h1")


# ── cross-space search scope ────────────────────────────────────────────────


def test_search_explicit_list_refuses_when_incomplete():
    # An explicit space_ids list is all-or-nothing: an unreadable member refuses
    # the search and names it, matching the hyperspace path, REST, and the agent.
    svc = MagicMock()
    stack, svc, _ = _env(
        svc=svc, extra={"src.mcp.tools.common.resolve_scope": {"return_value": ["s1"]}}
    )
    with stack:
        out = _call(vector_tools.get_component_from_description("pay", space_ids=["s1", "s2"]))
    assert "s2" in out
    svc.global_semantic_search.assert_not_called()


def test_search_explicit_list_all_readable_searches_all():
    svc = MagicMock()
    svc.global_semantic_search.return_value = [
        {"key": "c1", "metadata": {"space_id": "s1", "description": "d", "type": "t"}}
    ]
    stack, svc, _ = _env(
        svc=svc, extra={"src.mcp.tools.common.resolve_scope": {"return_value": ["s1", "s2"]}}
    )
    with stack:
        out = _call(vector_tools.get_component_from_description("pay", space_ids=["s1", "s2"]))
    assert "c1" in out
    svc.global_semantic_search.assert_called_once_with(
        "pay", n_results=None, space_ids=["s1", "s2"]
    )


def test_search_empty_scope_returns_no_access():
    svc = MagicMock()
    stack, svc, _ = _env(
        svc=svc, extra={"src.mcp.tools.common.resolve_scope": {"return_value": []}}
    )
    with stack:
        out = _call(vector_tools.get_component_from_description("pay"))
    assert "access" in out.lower()
    svc.global_semantic_search.assert_not_called()


# ── hyperspace search is all-or-nothing (MCP is the only guard) ─────────────


def test_hyperspace_search_refuses_when_a_member_is_unreadable():
    # An unreadable member refuses the whole hyperspace search (no silent
    # narrowing), and the denial names the inaccessible members.
    svc = MagicMock()
    svc.get_hyperspace.return_value = {"hyperspace_id": "h1", "space_ids": ["s1", "s2"]}
    stack, svc, _ = _env(
        svc=svc,
        extra={
            "src.mcp.tools.common.hyperspace_authz.check_read": {
                "side_effect": HyperspaceAccessDenied("nope", inaccessible_space_ids=["s2"])
            }
        },
    )
    with stack:
        out = _call(vector_tools.get_component_from_description("pay", hyperspace_id="h1"))
    assert "s2" in out
    svc.global_semantic_search.assert_not_called()


def test_hyperspace_search_runs_when_all_members_readable():
    svc = MagicMock()
    svc.get_hyperspace.return_value = {"hyperspace_id": "h1", "space_ids": ["s1", "s2"]}
    svc.global_semantic_search.return_value = []
    stack, svc, _ = _env(
        svc=svc,
        extra={
            "src.mcp.tools.common.hyperspace_authz.check_read": {"return_value": None},
            "src.mcp.tools.common.resolve_scope": {"return_value": ["s1", "s2"]},
        },
    )
    with stack:
        _call(vector_tools.get_component_from_description("pay", hyperspace_id="h1"))
    svc.global_semantic_search.assert_called_once_with(
        "pay", n_results=None, space_ids=["s1", "s2"]
    )


# ── analysis tool (own try/except) still gates ─────────────────────────────


def test_load_knowledge_base_denied():
    svc = MagicMock()
    svc.read_status.return_value = {"space_id": "s1", "visibility": "private"}
    stack, svc, _ = _env(
        svc=svc, extra={"src.mcp.tools.common.repo_access.can_read": {"side_effect": ReadDenied()}}
    )
    with stack:
        out = _call(analysis_tools.load_knowledge_base("s1"))
    assert "do not have access" in out.lower()
    svc.get_space.assert_not_called()
