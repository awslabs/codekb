"""Common utilities for MCP tools to reduce duplication."""

import logging
from functools import wraps
from typing import Callable, Optional, Tuple

from mcp.server.fastmcp import Context

from ...authz import hyperspace as hyperspace_authz
from ...authz import repo_access
from ...authz.errors import AuthzError, CredentialExpired, HyperspaceAccessDenied, ReadDenied
from ...authz.scope import resolve_scope
from ...authz.startup import require_authorization
from ...core.models import SPACE_SEP, split_space

logger = logging.getLogger(__name__)

SEPARATOR = SPACE_SEP

# Shown by a cross-space tool when the caller can read no spaces at all.
NO_ACCESS_MESSAGE = "You don't have access to any spaces to search."


def parse_component_input(raw: str) -> Tuple[str, str]:
    """Extract the space_id from a fully-qualified component input.

    The MCP layer is stateless, so every component reference must be
    space-qualified using the ``space_id::component_name`` format.

    Args:
        raw: Fully-qualified component string, e.g. "sdk-python-b374::event_loop.cycle".

    Returns:
        (space_id, raw) — the parsed space_id and the original qualified string,
        which downstream service calls use as the component identifier.

    Raises:
        ValueError: If the input is not space-qualified (missing the "::" prefix).
    """
    try:
        return split_space(raw)
    except ValueError:
        raise ValueError(
            f"Component '{raw}' must be space-qualified as 'space_id{SEPARATOR}component_name'. "
            "Pass the fully-qualified id (e.g. from get_component_from_description)."
        )


# ── authorization helpers ───────────────────────────────────────────────
# MCP has READ-only tools, so only READ gates apply. Each is a no-op when the
# switch is off, mirroring the REST gates. The caller's identity is re-derived
# from the request the FastMCP Context carries — the same path the REST side
# uses (verified JWT → UserIdentity → AuthContext).


def _auth_context(ctx: Context):
    """Build the caller's AuthContext from the MCP request."""
    from ...api.auth import current_auth_context

    return current_auth_context(ctx.request_context.request)


def gate_space_read(ctx: Context, space_id: str) -> None:
    """Require READ on one space. A missing space reads as denied. No-op when off."""
    if not require_authorization():
        return
    from ...service.codekb_service import CodeKBService

    actx = _auth_context(ctx)
    record = CodeKBService.instance().read_status(space_id)
    if record is None:
        raise ReadDenied()
    repo_access.can_read(actx, record)


def preload_space_statuses(ctx: Context, statuses: dict) -> None:
    """Hand already-fetched ``{space_id: status}`` to this request's auth context
    so the READ gates read them from memory instead of fetching from S3 again.
    No-op when authz is off."""
    if not require_authorization():
        return
    _auth_context(ctx).preload_statuses(statuses)


def gate_hyperspace_read(ctx: Context, space_ids: list) -> None:
    """Require READ on EVERY member — all-or-nothing, like the REST and agent gates.

    An MCP hyperspace search must refuse (not silently narrow) when any member is
    unreadable, since MCP is the only enforcement point. No-op when off.
    """
    if not require_authorization():
        return
    actx = _auth_context(ctx)
    members = [
        {**rec, "space_id": sid}
        for sid in space_ids
        if (rec := actx.space_status(sid)) is not None
    ]
    if not members:
        raise HyperspaceAccessDenied("This hyperspace has no accessible member spaces.")
    hyperspace_authz.check_read(actx, members)


def resolve_search_scope(ctx: Context, requested: Optional[list]) -> Optional[list]:
    """Scope a cross-space search to the caller's readable spaces.

    Off → request scope unchanged. Otherwise → the caller's readable subset
    (``None`` request = all readable).
    """
    if not require_authorization():
        return requested
    return resolve_scope(_auth_context(ctx), requested)


def filter_readable_spaces(ctx: Context, summaries: list) -> list:
    """Keep only the space summaries the caller can READ. All when off.

    An expired token hides only private spaces (public stay listed); a down
    provider propagates (fails the tool), consistent with the REST list.
    """
    if not require_authorization():
        return summaries
    actx = _auth_context(ctx)
    return [s for s in summaries if repo_access.can_list(actx, s)]


def _format_authz_error(exc: AuthzError) -> str:
    """Render an authz denial as a tool-error string, message first (§3).

    MCP users can't fix a token from the editor, so the expired message names the
    web page; a hyperspace denial names the inaccessible members.
    """
    from ...api.auth import git_creds_url

    message = str(exc)
    if isinstance(exc, CredentialExpired):
        return (
            f"{message} Reconnect your {exc.provider} token on the "
            f"Git credentials page: {git_creds_url()}"
        )
    if isinstance(exc, HyperspaceAccessDenied) and exc.inaccessible_space_ids:
        return f"{message} Inaccessible spaces: {', '.join(exc.inaccessible_space_ids)}"
    return message


def with_error_handling(func: Callable) -> Callable:
    """Decorator for consistent error handling across tools.

    An authorization denial is surfaced message-first (no "failed" prefix); any
    other error is logged and returned as a short failure string.
    """

    @wraps(func)
    async def wrapper(*args, **kwargs) -> str:
        try:
            return await func(*args, **kwargs)
        except AuthzError as e:
            logger.info(f"{func.__name__} authorization denied: {e.code}")
            return _format_authz_error(e)
        except Exception as e:
            logger.error(f"{func.__name__} failed: {e}", exc_info=True)
            return f"{func.__name__} failed: {e}"

    return wrapper
