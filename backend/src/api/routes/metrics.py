"""Global metrics REST route.

GET /metrics — aggregate metrics across all spaces and the vector store
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from ..auth import current_auth_context, require_current_user
from ...authz.errors import AuthzError
from ...authz.startup import require_authorization
from ...service.codekb_service import CodeKBService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
async def get_global_metrics(request: Request) -> dict:
    """Return aggregate metrics over the spaces the caller can read.

    With authorization on, metrics are scoped to the caller's visible spaces so
    totals never leak across tenants. With it off, metrics cover all spaces.
    """
    require_current_user(request)
    codekb_service = CodeKBService.instance()
    try:
        if require_authorization():
            ctx = current_auth_context(request)
            # Off the event loop: filtering reads each space and may make provider calls.
            spaces = await asyncio.to_thread(ctx.visible_spaces)
            return await asyncio.to_thread(codekb_service.get_global_metrics, spaces)
        return await asyncio.to_thread(codekb_service.get_global_metrics)
    except AuthzError:
        # Let the app-level AuthzError handler map these to their real status,
        raise
    except Exception as e:
        logger.error(f"Failed to get global metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get metrics")
