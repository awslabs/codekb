"""Space management REST routes.

GET    /spaces          — list all spaces
POST   /spaces          — create space (+ trigger analysis)
GET    /spaces/{id}     — get space details
DELETE /spaces/{id}     — delete a space
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...service.codekb_service import (
    SpaceBusyError,
    SpaceNotFoundError,
    ServiceError,
    zip_upload_enabled,
)
from ...core.logging_utils import get_logger
from ...authz.startup import require_authorization
from ...service.codekb_service import CodeKBService
from ..auth import current_auth_context, require_current_user
from ..gates import gate_read, gate_repo_read, gate_write

router = APIRouter()
logger = get_logger(__name__)

# Strong references to fire-and-forget background tasks. The event loop only
# holds a weak reference to a task, so without this a scheduled task can be
# garbage-collected mid-execution. Tasks remove themselves on completion.
_background_tasks: set[asyncio.Task] = set()


# ── request/response models ─────────────────────────────────────────────


class CreateSpaceRequest(BaseModel):
    """Request body for POST /spaces."""

    git_url: str = Field(..., description="Git repository URL to analyze")
    space_name: str | None = Field(
        default=None,
        description="Optional human-readable name for the space.",
    )
    branch: str | None = Field(
        default=None,
        description="Git branch to analyze. Defaults to the repository's default branch.",
    )
    summarize: bool = Field(default=False, description="Run AI summarization after analysis")
    omit_low_business_relevance: bool = Field(
        default=False,
        description="Skip low business relevance components during AI summarization",
    )
    overview_language: str | None = Field(
        default=None,
        description=(
            "Optional lowercase language subtag (e.g. 'en', 'ja') for generated "
            "Space Overview documents. Set once at creation; not validated here."
        ),
    )
    analysis_language: str | None = Field(
        default=None,
        description=(
            "Optional lowercase language subtag (e.g. 'en', 'ja') for generated "
            "analysis descriptions. Set once at creation; not validated here."
        ),
    )


class UploadUrlRequest(BaseModel):
    """Request body for POST /spaces/upload-url."""

    space_name: str = Field(..., description="Human-readable name for the space")
    summarize: bool = Field(default=False, description="Run AI summarization after analysis")
    omit_low_business_relevance: bool = Field(
        default=False,
        description="Skip low business relevance components during AI summarization",
    )


class StartUploadAnalysisRequest(BaseModel):
    """Request body for POST /spaces/upload."""

    space_id: str = Field(..., description="Space ID returned by upload-url endpoint")
    space_name: str = Field(..., description="Human-readable name for the space")
    summarize: bool = Field(default=False, description="Run AI summarization after analysis")
    omit_low_business_relevance: bool = Field(
        default=False,
        description="Skip low business relevance components during AI summarization",
    )
    overview_language: str | None = Field(
        default=None,
        description=(
            "Optional lowercase language subtag (e.g. 'en', 'ja') for generated "
            "Space Overview documents. Set once at creation; not validated here."
        ),
    )
    analysis_language: str | None = Field(
        default=None,
        description=(
            "Optional lowercase language subtag (e.g. 'en', 'ja') for generated "
            "analysis descriptions. Set once at creation; not validated here."
        ),
    )


class SpaceSummary(BaseModel):
    """Summary of a space in list responses."""

    space_id: str
    status: str = "UNKNOWN"
    component_count: int | None = None
    vector_count: int | None = None
    source_directory: str | None = None
    git_url: str | None = None
    source_type: str | None = None
    space_name: str | None = None
    language_count: int | None = None
    languages: list[str] | None = None
    total_lines: int | None = None
    git_commit_hash: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    omit_low_business_relevance: bool | None = None
    # "public" | "private" for git spaces; None for zip/legacy spaces.
    visibility: str | None = None
    error: str | None = None


class SpaceDetail(BaseModel):
    """Full space details."""

    space_id: str
    source_directory: str
    component_count: int
    created_at: str | None = None
    created_by: str | None = None
    overview_language: str | None = None
    analysis_language: str | None = None
    version: str
    statistics: dict
    vector_count: int = 0
    git_url: str = ""
    git_commit_hash: str | None = None
    exclude_patterns: list[str] = []
    # "public" | "private" for git spaces; None for zip/legacy spaces.
    visibility: str | None = None


# ── routes ───────────────────────────────────────────────────────────────


@router.get("", response_model=list[SpaceSummary])
async def list_spaces(request: Request) -> list[dict]:
    """List spaces the caller can read (all of them when the switch is off)."""
    require_current_user(request)
    if require_authorization():
        ctx = current_auth_context(request)
        # Off the event loop: filtering reads each space and may make provider calls.
        return await asyncio.to_thread(ctx.visible_spaces)
    return await asyncio.to_thread(CodeKBService.instance().list_spaces)


@router.get("/git-branches", response_model=dict)
async def list_git_branches(git_url: str, request: Request) -> dict:
    """List branches and tags for a remote git repository.

    Used by the create-space form to populate a branch dropdown so users
    don't have to type branch names by hand.
    """
    user = require_current_user(request)
    codekb_service = CodeKBService.instance()
    try:
        # Blocking ls-remote: keep it off the event loop so a slow remote
        # degrades this request rather than every endpoint.
        return await asyncio.to_thread(codekb_service.list_remote_branches, git_url, actor_sub=user.sub)
    except ServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("", response_model=dict, status_code=201)
async def create_space(body: CreateSpaceRequest, request: Request) -> dict:
    """Create a new space and trigger analysis."""
    user = require_current_user(request)
    codekb_service = CodeKBService.instance()
    try:
        # Off the event loop (both make blocking provider + S3 calls). The gate
        # classifies visibility and passes it on, so the repo is probed once.
        visibility = await asyncio.to_thread(gate_repo_read, request, body.git_url)
        result = await asyncio.to_thread(
            codekb_service.start_analysis,
            git_url=body.git_url,
            branch=body.branch,
            summarize=body.summarize,
            omit_low_business_relevance=body.omit_low_business_relevance,
            space_name=body.space_name,
            created_by=user.email,
            actor_sub=user.sub,
            visibility=visibility,
            overview_language=body.overview_language,
            analysis_language=body.analysis_language,
        )
        return result
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/upload-url", response_model=dict, status_code=200)
async def generate_upload_url(body: UploadUrlRequest, request: Request) -> dict:
    """Generate a presigned S3 URL for uploading a source zip."""
    user = require_current_user(request)
    if not zip_upload_enabled():
        raise HTTPException(status_code=403, detail="Zip upload is disabled on this instance.")
    codekb_service = CodeKBService.instance()
    try:
        return codekb_service.generate_upload_url(
            space_name=body.space_name,
            summarize=body.summarize,
            omit_low_business_relevance=body.omit_low_business_relevance,
            created_by=user.email,
            actor_sub=user.sub,
        )
    except ServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/upload", response_model=dict, status_code=201)
async def start_upload_analysis(body: StartUploadAnalysisRequest, request: Request) -> dict:
    """Start analysis of an uploaded zip file."""
    user = require_current_user(request)
    if not zip_upload_enabled():
        raise HTTPException(status_code=403, detail="Zip upload is disabled on this instance.")
    codekb_service = CodeKBService.instance()
    try:
        result = codekb_service.start_upload_analysis(
            space_id=body.space_id,
            space_name=body.space_name,
            summarize=body.summarize,
            omit_low_business_relevance=body.omit_low_business_relevance,
            created_by=user.email,
            actor_sub=user.sub,
            overview_language=body.overview_language,
            analysis_language=body.analysis_language,
        )
        return result
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.get("/{space_id}", response_model=SpaceDetail)
async def get_space(space_id: str, request: Request, _authz: None = Depends(gate_read)) -> dict:
    """Get space details and statistics."""
    require_current_user(request)
    codekb_service = CodeKBService.instance()
    try:
        return codekb_service.get_space(space_id)
    except SpaceNotFoundError:
        raise HTTPException(status_code=404, detail=f"Space not found: {space_id}")


@router.delete("/{space_id}", status_code=202)
async def delete_space(space_id: str, request: Request, _authz: None = Depends(gate_write)) -> dict:
    """Delete a space and its knowledge base asynchronously.

    Returns 202 Accepted immediately and runs deletion in the background.
    Poll GET /api/spaces/{id}/status to track progress.
    """
    require_current_user(request)
    codekb_service = CodeKBService.instance()

    # Mark space as DELETING atomically (raises 404 if space doesn't exist,
    # 409 if the space is being indexed and must not be deleted mid-flight)
    try:
        codekb_service.mark_space_deleting(space_id)
    except SpaceNotFoundError:
        raise HTTPException(status_code=404, detail=f"Space not found: {space_id}")
    except SpaceBusyError:
        raise HTTPException(
            status_code=409,
            detail=f"Space '{space_id}' is being indexed; retry once indexing finishes.",
        )

    # Schedule background deletion with exception logging
    async def _delete_with_logging():
        try:
            await codekb_service.delete_space(space_id)
        except Exception as e:
            logger.error(f"Background deletion failed for '{space_id}': {e}", exc_info=True)

    task = asyncio.create_task(_delete_with_logging())

    # Keep a strong reference so the task isn't garbage-collected mid-execution.
    _background_tasks.add(task)

    # Belt-and-suspenders: attach done-callback to catch unexpected exceptions
    # that escape the try/except (e.g., bugs in logger or task creation edge cases)
    def _on_task_done(t: asyncio.Task):
        _background_tasks.discard(t)
        if not t.cancelled() and (exc := t.exception()):
            logger.error(f"Unhandled exception in deletion task for '{space_id}': {exc}", exc_info=exc)

    task.add_done_callback(_on_task_done)

    return {
        "space_id": space_id,
        "status": "DELETING",
        "message": f"Deletion in progress. Poll GET /api/spaces/{space_id}/status.",
    }
