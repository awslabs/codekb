"""Analysis pipeline REST routes.

POST   /spaces/{id}/refresh              — trigger space refresh
GET    /spaces/{id}/status               — get analysis pipeline status
GET    /spaces/{id}/knowledge-base       — get raw knowledge base JSON
GET    /spaces/{id}/overview/{doc_key}   — get a Space Overview markdown document
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ...service.codekb_service import (
    CodeKBService,
    ServiceError,
    SpaceNotFoundError,
    overview_generation_enabled,
)
from ..auth import get_current_user, require_current_user
from ..gates import gate_read, gate_write

router = APIRouter()


def _extract_actor(request: Request) -> str | None:
    """Extract the actor identity (email) from the request, or None if unauthenticated."""
    user = get_current_user(request)
    if user is None:
        return None
    return user.email


class PipelineStep(BaseModel):
    """A single step in the analysis pipeline."""

    step: str
    status: str  # in_progress | completed | failed
    started_at: str | None = None
    updated_at: str | None = None


class RefreshPipelineRun(BaseModel):
    """One space-refresh run: its start time and the steps it executed."""

    started_at: str | None = None
    steps: list[PipelineStep] = []


class SpaceOverviewDocEntry(BaseModel):
    """A single Space Overview document entry in the catalog."""

    key: str
    title: str
    s3_path: str
    generated_at: str | None = None
    stale: bool | None = None


class RefreshResultPayload(BaseModel):
    """Payload for the refresh_result field in AnalysisStatus."""

    files_added: int = 0
    files_modified: int = 0
    files_removed: int = 0
    files_unchanged: int = 0
    components_added: int = 0
    components_removed: int = 0
    components_reprocessed: int = 0
    vectors_upserted: int = 0
    vectors_deleted: int = 0
    duration_s: float = 0.0


class AnalysisStatus(BaseModel):
    """Response for GET /spaces/{id}/status."""

    space_id: str
    space_name: str | None = None
    # SUBMITTED | ANALYZING | SUMMARIZING | REFRESHING | READY | FAILED | REFRESH_FAILED | NOT_FOUND
    status: str
    component_count: int | None = None
    error: str | None = None
    current_step: str | None = None
    pipeline_steps: list[PipelineStep] = []
    refresh_pipeline_runs: list[RefreshPipelineRun] = []
    git_url: str | None = None
    branch: str | None = None
    git_commit_hash: str | None = None
    created_by: str | None = None
    started_at: str | None = None
    created_at: str | None = None
    summarize_total: int | None = None
    summarize_done: int | None = None
    summarize_percent: float | None = None
    # Written only by older refresh code; the next completed refresh clears it.
    refresh_warning: str | None = None
    space_overview_docs: list[SpaceOverviewDocEntry] = []
    overview_status: str | None = None
    overview_total: int | None = None
    overview_done: int | None = None
    overview_percent: float | None = None
    business_context: dict | None = None
    omit_low_business_relevance: bool | None = None
    statistics: dict | None = None
    # Per-operation Bedrock token consumption (e.g. {"space_creation": {...}}).
    # Cost is not computed here — pricing_url points to AWS Bedrock pricing.
    token_usage: dict | None = None
    pricing_url: str | None = None
    # Space-level Bedrock model identity ({"model_id": ..., "region": ...}).
    bedrock_model: dict | None = None
    refresh_result: RefreshResultPayload | None = None
    refresh_errors: list[str] | None = None


@router.post("/{space_id}/refresh", status_code=202)
async def refresh_space(
    space_id: str, request: Request, _authz: None = Depends(gate_write)
) -> dict:
    """Trigger a Space Refresh for an existing git-sourced Space."""
    user = require_current_user(request)
    codekb_service = CodeKBService.instance()
    try:
        return codekb_service.refresh_space(space_id=space_id, actor=user.email, actor_sub=user.sub)
    except SpaceNotFoundError:
        raise HTTPException(status_code=404, detail=f"Space not found: {space_id}")
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.get("/{space_id}/status", response_model=AnalysisStatus)
async def get_analysis_status(
    space_id: str, request: Request, _authz: None = Depends(gate_read)
) -> dict:
    """Get the current analysis pipeline status for a space."""
    require_current_user(request)
    codekb_service = CodeKBService.instance()
    return codekb_service.get_analysis_status(space_id)


@router.get("/{space_id}/knowledge-base")
async def get_knowledge_base(
    space_id: str, request: Request, _authz: None = Depends(gate_read)
) -> dict:
    """Return the raw knowledge_base.json for a space."""
    require_current_user(request)
    codekb_service = CodeKBService.instance()
    try:
        return codekb_service.get_knowledge_base_raw(space_id)
    except SpaceNotFoundError:
        raise HTTPException(status_code=404, detail=f"Knowledge base not found: {space_id}")


@router.get("/{space_id}/overview/{doc_key}")
async def get_overview_document(
    space_id: str, doc_key: str, request: Request, _authz: None = Depends(gate_read)
) -> PlainTextResponse:
    """Return a Space Overview markdown document.

    Valid doc_key values: business_overview, application_architecture,
    technology_stack, deployment_architecture.
    """
    require_current_user(request)
    codekb_service = CodeKBService.instance()
    try:
        content = codekb_service.get_space_overview_document(space_id, doc_key)
        return PlainTextResponse(content, media_type="text/markdown")
    except SpaceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Overview document not found: {doc_key} for space {space_id}",
        )


@router.post("/{space_id}/generate-overview", status_code=202)
async def generate_overview(
    space_id: str, request: Request, _authz: None = Depends(gate_write)
) -> dict:
    """Trigger Space Overview generation for an existing READY space."""
    require_current_user(request)
    if not overview_generation_enabled():
        raise HTTPException(
            status_code=403, detail="Overview generation is disabled on this instance."
        )
    codekb_service = CodeKBService.instance()
    try:
        return codekb_service.start_overview_generation(space_id)
    except SpaceNotFoundError:
        raise HTTPException(status_code=404, detail=f"Space not found: {space_id}")
    except ServiceError as e:
        raise HTTPException(status_code=409, detail=str(e))
