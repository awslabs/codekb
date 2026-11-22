"""Hyperspace management REST routes.

GET    /hyperspaces          — list all hyperspaces
POST   /hyperspaces          — create a hyperspace
GET    /hyperspaces/{id}     — get hyperspace details
DELETE /hyperspaces/{id}     — delete a hyperspace
"""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ...authz import hyperspace as hyperspace_authz
from ...authz import repo_access
from ...authz.errors import HyperspaceAccessDenied, ReadDenied, WriteDenied
from ...authz.startup import require_authorization
from ...service.codekb_service import (
    HyperspaceNotFoundError,
    SpaceNotFoundError,
    ServiceError,
    overview_generation_enabled,
)
from ..auth import current_auth_context, require_current_user
from ...service.codekb_service import CodeKBService

router = APIRouter()


def _gate_members(hyperspace_id: str, request: Request, *, write: bool) -> dict:
    """Return the config checked for READ (or WRITE) on every member space.

    Loads the hyperspace (404 if missing) and runs the all-or-nothing hyperspace
    gate: provider down → 503, else an expired token → 401, else the denied
    member ids are named in a 403. Sync — call via ``asyncio.to_thread``.
    """
    # Reuse the statuses get_hyperspace already fetched in parallel instead of
    # re-reading each one serially via ctx.space_status.
    status_sink: dict = {}
    cfg = CodeKBService.instance().get_hyperspace(hyperspace_id, status_sink=status_sink)  # raises HyperspaceNotFoundError
    if not require_authorization():
        return cfg
    ctx = current_auth_context(request)
    ctx.preload_statuses(status_sink)
    members = [
        {**rec, "space_id": sid}
        for sid in (cfg.get("space_ids") or [])
        if (rec := ctx.space_status(sid)) is not None
    ]
    if write:
        hyperspace_authz.check_write(ctx, members)
    else:
        hyperspace_authz.check_read(ctx, members)
    # A missing member can still have contributed private content to the
    # overview. Checking only the remaining members would widen access.
    # Check after the gates above to preserve provider/credential error precedence.
    if not members or len(members) != len(cfg.get("space_ids") or []):
        raise HyperspaceAccessDenied("This hyperspace has missing or no accessible member spaces.")
    return cfg


def _member_readable(ctx, sid: str) -> bool:
    """Whether the caller may READ one member space.

    Only a plain READ denial marks a member inaccessible; a transient failure
    (expired token, provider down) propagates instead, so it never becomes a
    permanent marker. Missing member (deleted) → readable here, reported via
    ``missing_space_ids``.
    """
    record = ctx.space_status(sid)
    if record is None:
        return True
    try:
        repo_access.can_read(ctx, {**record, "space_id": sid})
        return True
    except ReadDenied:
        return False


def _inaccessible_members(ctx, cfg: dict) -> list[str]:
    """Member space ids the caller can't READ (for a non-refusing marker)."""
    return [sid for sid in (cfg.get("space_ids") or []) if not _member_readable(ctx, sid)]


def _list_hyperspaces(ctx) -> list[dict]:
    """List hyperspaces, annotating each with the caller's inaccessible members."""
    # Reuse the member statuses the service already fetched in parallel, so the
    # READ check doesn't re-read every status.json from S3 serially (the
    # dominant cost of this endpoint under authz).
    status_sink: dict = {} if ctx is not None else None
    items = CodeKBService.instance().list_hyperspaces(status_sink=status_sink)
    if ctx is not None:
        ctx.preload_statuses(status_sink)
        # Decide READ once per unique member: a space shared by several
        # hyperspaces is evaluated a single time, not per hyperspace.
        unique_sids = {sid for hs in items for sid in (hs.get("space_ids") or [])}
        denied = {sid for sid in unique_sids if not _member_readable(ctx, sid)}
        for hs in items:
            hs["inaccessible_space_ids"] = [
                sid for sid in (hs.get("space_ids") or []) if sid in denied
            ]
    return items


def _get_hyperspace(hyperspace_id: str, ctx) -> dict:
    """Get a hyperspace (404 if missing), annotated with inaccessible members."""
    # Reuse the statuses the service already fetched (see _list_hyperspaces).
    status_sink: dict = {} if ctx is not None else None
    cfg = CodeKBService.instance().get_hyperspace(hyperspace_id, status_sink=status_sink)  # raises HyperspaceNotFoundError
    if ctx is not None:
        ctx.preload_statuses(status_sink)
        cfg["inaccessible_space_ids"] = _inaccessible_members(ctx, cfg)
    return cfg


def _require_owner(hyperspace_id: str, request: Request) -> None:
    """Owner-only gate for update / delete (404 if missing). No-op when off.

    Owner = created_by_sub, else legacy created_by email. No creator stored →
    nobody can manage it (fail closed). Call via ``asyncio.to_thread``.
    """
    # Raw config, not the enriched getter — skips the member S3 fan-out the
    # owner check doesn't need.
    cfg = CodeKBService.instance().read_hyperspace_config(hyperspace_id)
    if cfg is None:
        raise HyperspaceNotFoundError(hyperspace_id)
    if not require_authorization():
        return
    ctx = current_auth_context(request)
    if repo_access.is_space_owner(ctx, cfg):
        return
    raise WriteDenied()


class CreateHyperspaceRequest(BaseModel):
    name: str = Field(..., description="Human-readable name for the hyperspace")
    space_ids: list[str] = Field(..., description="List of space IDs (minimum 1)")


class UpdateHyperspaceRequest(BaseModel):
    # Hyperspace names cannot be edited (parity with spaces, which have no
    # rename operation). Any ``name`` supplied by a client is silently ignored.
    space_ids: list[str] | None = Field(
        None,
        description=(
            "Replacement list of space IDs (minimum 1). Omit to leave membership "
            "unchanged. Providing this field fully replaces the current members."
        ),
    )


@router.get("")
async def list_hyperspaces(request: Request) -> list[dict]:
    """List all hyperspaces (each annotated with the caller's inaccessible members)."""
    require_current_user(request)
    ctx = current_auth_context(request) if require_authorization() else None
    return await asyncio.to_thread(_list_hyperspaces, ctx)


@router.post("", status_code=201)
async def create_hyperspace(body: CreateHyperspaceRequest, request: Request) -> dict:
    """Create a new hyperspace."""
    user = require_current_user(request)
    try:
        return CodeKBService.instance().create_hyperspace(
            body.name, body.space_ids, created_by=user.email, created_by_sub=user.sub
        )
    except SpaceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{hyperspace_id}")
async def get_hyperspace(hyperspace_id: str, request: Request) -> dict:
    """Get hyperspace details with space summaries (+ the caller's inaccessible members)."""
    require_current_user(request)
    ctx = current_auth_context(request) if require_authorization() else None
    try:
        return await asyncio.to_thread(_get_hyperspace, hyperspace_id, ctx)
    except HyperspaceNotFoundError:
        raise HTTPException(status_code=404, detail=f"Hyperspace not found: {hyperspace_id}")


@router.put("/{hyperspace_id}")
async def update_hyperspace(
    hyperspace_id: str, body: UpdateHyperspaceRequest, request: Request
) -> dict:
    """Update a hyperspace's member spaces.

    Providing ``space_ids`` fully replaces the existing membership — omitted
    spaces are removed, new spaces are added. Every space in the replacement
    list must exist and be READY. The hyperspace name cannot be changed.
    """
    user = require_current_user(request)
    try:
        await asyncio.to_thread(_require_owner, hyperspace_id, request)
        return await asyncio.to_thread(
            CodeKBService.instance().update_hyperspace,
            hyperspace_id,
            body.space_ids,
            user.email,
        )
    except HyperspaceNotFoundError:
        raise HTTPException(status_code=404, detail=f"Hyperspace not found: {hyperspace_id}")
    except SpaceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{hyperspace_id}")
async def delete_hyperspace(hyperspace_id: str, request: Request) -> dict:
    """Delete a hyperspace."""
    require_current_user(request)
    try:
        await asyncio.to_thread(_require_owner, hyperspace_id, request)
        return await asyncio.to_thread(CodeKBService.instance().delete_hyperspace, hyperspace_id)
    except HyperspaceNotFoundError:
        raise HTTPException(status_code=404, detail=f"Hyperspace not found: {hyperspace_id}")


@router.post("/{hyperspace_id}/generate-overview", status_code=202)
async def generate_hyperspace_overview(hyperspace_id: str, request: Request) -> dict:
    """Trigger Hyperspace Overview generation.

    First ensures every READY member space has an up-to-date Space Overview,
    (re)generating any that are missing or stale (older than the latest code
    refresh), then synthesizes a system-level overview (System Overview + Space
    Interconnections) from them. This can incur significant Bedrock cost and
    take considerable time for large hyperspaces — the UI confirms before
    triggering. Returns immediately; poll GET /api/hyperspaces/{id} for progress
    via the overview_status field.
    """
    require_current_user(request)
    if not overview_generation_enabled():
        raise HTTPException(
            status_code=403, detail="Overview generation is disabled on this instance."
        )
    try:
        # Generate-overview is a WRITE: require WRITE on every member.
        await asyncio.to_thread(_gate_members, hyperspace_id, request, write=True)
        return await asyncio.to_thread(
            CodeKBService.instance().start_hyperspace_overview_generation, hyperspace_id
        )
    except HyperspaceNotFoundError:
        raise HTTPException(status_code=404, detail=f"Hyperspace not found: {hyperspace_id}")
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.get("/{hyperspace_id}/overview/{doc_key}")
async def get_hyperspace_overview_document(
    hyperspace_id: str, doc_key: str, request: Request
) -> PlainTextResponse:
    """Return a Hyperspace Overview markdown document.

    Valid doc_key values: system_overview, space_interconnections.
    """
    require_current_user(request)
    try:
        # Reading the overview exposes member content: require READ on every member.
        cfg = await asyncio.to_thread(_gate_members, hyperspace_id, request, write=False)
        content = await asyncio.to_thread(
            CodeKBService.instance().get_hyperspace_overview_document,
            hyperspace_id,
            doc_key,
            config=cfg,
        )
        return PlainTextResponse(content, media_type="text/markdown")
    except HyperspaceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Overview document not found: {doc_key} for hyperspace {hyperspace_id}",
        )
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
