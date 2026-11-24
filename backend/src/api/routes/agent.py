"""Agent chat REST routes (stateless).

POST   /agent/chat          — send a message (JSON response)
POST   /agent/chat/stream   — send a message (SSE streaming)

Conversation history is managed by the frontend and sent with each request.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Any, List, Optional
from starlette.requests import Request

from ...agent.codekb_agent import (
    run_agent_query,
    stream_agent_query,
)
from ...authz.errors import ReadDenied
from ...authz.scope import resolve_scope
from ...authz.startup import require_authorization
from ...service.codekb_service import SpaceNotFoundError
from ...core.logging_utils import get_logger, format_payload
from ...core.token_usage import pricing_url
from ..auth import current_auth_context, require_current_user

logger = get_logger(__name__)

router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# Shown when authorization is on and the caller can read no spaces at all. We
# don't run the model (and never let an empty scope fall through to "all spaces").
_NO_ACCESS_MESSAGE = "You don't have access to any spaces to search."


class AgentRequest(BaseModel):
    """Request body for agent endpoints."""

    message: str = Field(..., description="Natural language question for the agent")
    space_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "Space IDs to scope the agent to. Omit (or send null) to search every "
            "space — semantic search then runs unfiltered instead of enumerating "
            "every space id into a metadata filter."
        ),
    )
    mode: str = Field(default="chat", description="Agent mode: 'chat' or 'impact_analysis'")
    history: Optional[List[dict]] = Field(
        default=None,
        description="Prior conversation history (Bedrock message format). Omit for new conversation.",
    )


class AgentResponse(BaseModel):
    """Response from the agent."""

    space_ids: Optional[List[str]]
    message: str
    response: str
    history: List[Any]
    token_usage: Optional[dict] = None
    pricing_url: Optional[str] = None


def _no_access_history(body: AgentRequest) -> list:
    """Conversation history with the user turn and the no-access reply appended."""
    history = list(body.history or [])
    history.append({"role": "user", "content": body.message})
    history.append({"role": "assistant", "content": _NO_ACCESS_MESSAGE})
    return history


async def _resolve_agent_scope(request: Request, body: AgentRequest) -> Optional[List[str]]:
    """The agent's space scope — the ids to search, or None for all. No-op when off.

    An explicit id list is all-or-nothing: one unreadable space refuses the request
    (the caller asked for exactly those). ``None`` means "all readable", filtered
    not refused (an expired token leaves the public spaces). Runs off the loop; a
    down provider, or an expired token on an explicit list, raises AuthzError.
    """
    if not require_authorization():
        return body.space_ids
    ctx = current_auth_context(request)
    space_ids = await asyncio.to_thread(resolve_scope, ctx, body.space_ids)
    if body.space_ids is not None:
        inaccessible = sorted(set(body.space_ids) - set(space_ids))
        if inaccessible:
            raise ReadDenied(
                "You don't have access to all the selected spaces: " + ", ".join(inaccessible)
            )
    return space_ids


@router.post("/chat", response_model=AgentResponse)
async def agent_chat(body: AgentRequest, request: Request) -> dict:
    """Send a message to the CodeKB agent (full response)."""
    require_current_user(request)
    space_ids = await _resolve_agent_scope(request, body)
    # Authz on, nothing readable → answer without running the model (and never let
    # an empty scope fall through to "all spaces").
    if require_authorization() and not space_ids:
        return {
            "space_ids": [],
            "message": body.message,
            "response": _NO_ACCESS_MESSAGE,
            "history": _no_access_history(body),
            "token_usage": None,
            "pricing_url": pricing_url(),
        }
    logger.info(
        f"Agent query for {space_ids or '<all spaces>'} (mode={body.mode}, "
        f"history_len={len(body.history) if body.history else 0}, prompt_chars={len(body.message)})"
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"Agent query prompt: {format_payload(body.message)}")
    try:
        response_text, updated_history, token_usage = await asyncio.to_thread(
            run_agent_query,
            space_ids,
            body.message,
            mode=body.mode,
            history=body.history,
        )
        return {
            "space_ids": space_ids,
            "message": body.message,
            "response": response_text,
            "history": updated_history,
            "token_usage": token_usage,
            "pricing_url": pricing_url(),
        }
    except SpaceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Agent error for {space_ids or '<all spaces>'}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Agent request failed")


@router.post("/chat/stream")
async def agent_chat_stream(body: AgentRequest, request: Request):
    """Send a message to the CodeKB agent with SSE streaming.

    Returns a stream of Server-Sent Events:
      - event: tool_start  — agent is calling a tool (data: {tool, input})
      - event: tool_done   — tool finished (data: {toolUseId})
      - event: tool_result — tool output (data: {toolUseId, result})
      - event: reasoning   — incremental thinking text (data: {text})
      - event: text        — incremental text (data: {text})
      - event: done        — final response + updated history (data: {response, history})
    """
    require_current_user(request)
    # Resolve scope BEFORE the stream starts: once SSE is flowing we can't return
    # an HTTP error, so an AuthzError here must surface as a normal response.
    space_ids = await _resolve_agent_scope(request, body)
    if require_authorization() and not space_ids:
        return StreamingResponse(
            _no_access_stream(body), media_type="text/event-stream", headers=_SSE_HEADERS
        )
    logger.info(
        f"Agent stream for {space_ids or '<all spaces>'} (mode={body.mode}, "
        f"history_len={len(body.history) if body.history else 0}, prompt_chars={len(body.message)})"
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(f"Agent stream prompt: {format_payload(body.message)}")

    return StreamingResponse(
        stream_agent_query(
            space_ids,
            body.message,
            mode=body.mode,
            history=body.history,
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


async def _no_access_stream(body: AgentRequest):
    """A one-event SSE stream: the done event carrying the no-access reply."""
    payload = {"response": _NO_ACCESS_MESSAGE, "history": _no_access_history(body)}
    yield f"event: done\ndata: {json.dumps(payload)}\n\n"
