"""Resolve a requested space scope to the ids the caller may READ.

Null scope = every readable space. No cap: global search chunks a large id set
into separate vector queries and merges, so any number of spaces is searchable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from . import repo_access
from .startup import require_authorization

if TYPE_CHECKING:
    from .context import AuthContext


def resolve_scope(ctx: "AuthContext", requested: Optional[list[str]]) -> list[str]:
    """Return the space ids the caller may READ.

    None → every readable space; a list → its readable subset. A read-denied
    space is dropped. With None, an expired token leaves the public spaces; with
    a list it propagates (the caller picked those spaces). A down provider fails
    either way (section 3).
    """
    if requested is None:
        return [s["space_id"] for s in ctx.visible_spaces()]
    readable: list[str] = []
    for sid in requested:
        record = ctx.space_status(sid)
        if record is None:
            continue
        if not require_authorization() or repo_access.can_read_bool(ctx, record):
            readable.append(sid)
    return readable
