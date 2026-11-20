"""Route authorization gates. Sync deps (run in the threadpool); no-op when off."""

from __future__ import annotations

from fastapi import Request

from ..authz import repo_access
from ..authz.errors import ReadDenied, RepoReadDenied
from ..authz.repo_key import repo_key
from ..authz.startup import require_authorization
from ..gitcreds.providers import get_provider
from ..service.codekb_service import (
    CodeKBService,
    lookup_repo_visibility,
    validate_git_url,
)
from .auth import current_auth_context


def gate_read(space_id: str, request: Request) -> None:
    """Require READ. A missing space reads as denied (never confirms it exists)."""
    if not require_authorization():
        return
    ctx = current_auth_context(request)
    record = CodeKBService.instance().read_status(space_id)
    if record is None:
        raise ReadDenied()
    repo_access.can_read(ctx, record)


def gate_write(space_id: str, request: Request) -> None:
    """Require WRITE (checks READ first)."""
    if not require_authorization():
        return
    ctx = current_auth_context(request)
    record = CodeKBService.instance().read_status(space_id)
    if record is None:
        raise ReadDenied()
    repo_access.can_write(ctx, record)


def gate_repo_read(request: Request, git_url: str) -> str | None:
    """Create path: require READ on the repo. Classifies visibility once and
    returns it for the caller to reuse. None when off."""
    if not require_authorization():
        return None
    # SSRF guard: enforce the host allowlist before the probe calls out. This gate
    # runs before start_analysis, so it's the only check in front of the probe.
    git_url = validate_git_url(git_url)
    ctx = current_auth_context(request)
    visibility = lookup_repo_visibility(git_url, ctx.sub)
    record = {
        "git_url": git_url,
        "visibility": visibility,
        "provider": get_provider(git_url),
        "repo_key": repo_key(git_url),
    }
    try:
        repo_access.can_read(ctx, record)
    except ReadDenied as e:
        raise RepoReadDenied() from e
    return visibility
