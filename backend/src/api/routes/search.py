"""Global semantic search REST route.

GET /search?q=<query>&limit=10&spaces=space-a,space-b
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request

from ...authz.scope import resolve_scope
from ...authz.startup import require_authorization
from ...service.codekb_service import CodeKBService
from ..auth import current_auth_context, require_current_user
from ..display import attach_search_display_names

logger = logging.getLogger(__name__)

router = APIRouter()


def _enrich_with_display_names(svc: CodeKBService, results: list[dict]) -> list[dict]:
    """Attach the UI-only ``display_name`` to global-search results in place.

    Results span spaces, so catalog metadata (for accurate Terraform / Dart
    labels) is gathered per distinct ``metadata.space_id``. A space that cannot
    be loaded falls back to id-only derivation for its results. UI-only: this
    runs in the REST route, never in the shared service.
    """
    meta_by_id: dict[str, dict] = {}
    space_ids = {
        (r.get("metadata") or {}).get("space_id")
        for r in results
        if (r.get("metadata") or {}).get("space_id")
    }
    for sid in space_ids:
        try:
            for component in svc.list_components(sid):
                meta_by_id[component["id"]] = component
        except Exception:
            # Missing/unreadable space → its results use id-only derivation.
            logger.debug("display_name enrichment skipped catalog for space %s", sid)
    return attach_search_display_names(results, meta_by_id)


@router.get("")
async def global_semantic_search(
    request: Request,
    q: str = Query(..., description="Natural language search query"),
    limit: int | None = Query(
        None,
        ge=1,
        le=50,
        description="Max results (defaults to storage.s3_vectors.default_top_k from config)",
    ),
    spaces: str | None = Query(None, description="Comma-separated space IDs to scope search"),
) -> list[dict]:
    """Semantic search across component descriptions, optionally scoped to specific spaces.

    With authorization on, the search is restricted to spaces the caller can READ:
    the requested scope (or all spaces, when none is given) is narrowed to the
    readable set before any vector query runs. A denied space is dropped. An
    expired token narrows "all spaces" to the public ones but fails an explicit
    scope; a down provider fails the request via the AuthzError handler.
    """
    user = require_current_user(request)
    requested = [s.strip() for s in spaces.split(",") if s.strip()] if spaces else None
    svc = CodeKBService.instance()

    if require_authorization():
        ctx = current_auth_context(request)
        # Resolution reads each space and may call providers — keep it off the loop.
        # AuthzError (expired token / provider down) propagates to the handler.
        space_ids = await asyncio.to_thread(resolve_scope, ctx, requested)
        logger.info(
            f"GET /search q={q!r}, limit={limit}, readable={len(space_ids)}, user={user.sub}"
        )
        if not space_ids:
            return []  # nothing the caller can read → nothing to search
        try:
            results = await asyncio.to_thread(svc.global_semantic_search, q, limit, space_ids)
        except Exception as e:
            logger.error(f"Global search failed for q='{q}': {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Search failed")
        return await asyncio.to_thread(_enrich_with_display_names, svc, results)

    # Authorization off — unchanged behaviour (plain list, scope as requested).
    logger.info(f"GET /search q={q!r}, limit={limit}, spaces={requested}, user={user.sub}")
    try:
        results = await asyncio.to_thread(
            svc.global_semantic_search, q, n_results=limit, space_ids=requested
        )
        logger.info(f"GET /search returned {len(results)} result(s) for q={q!r}")
        return await asyncio.to_thread(_enrich_with_display_names, svc, results)
    except Exception as e:
        logger.error(f"Global search failed for q='{q}', spaces={requested}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Search failed")
