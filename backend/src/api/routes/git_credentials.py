"""Per-user git credential management.

Endpoints under /api/user-profile/gitcreds. Every request is scoped to the
caller's own immutable Cognito ``sub`` — a user can only see, change, or delete
their own credentials. Tokens are write-only from the client: sent once on
``PUT`` and never returned.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...gitcreds.providers import (
    GitProvider,
    ProviderError,
    TokenInvalid,
    configured_self_hosted_host,
    enabled_provider_ids,
    provider_by_id,
)
from ...gitcreds.store import GitCredentialStore
from ...authz.cache import invalidate_user
from ...core.logging_utils import StorageError, get_logger
from ..auth import require_current_user

logger = get_logger(__name__)

router = APIRouter()


class GitCredentialRequest(BaseModel):
    username: str = Field(..., description="The account username on the provider")
    token: str = Field(..., description="Personal access token (write-only; never returned)")
    # No host field: the self-hosted host comes from the provider's *_CUSTOM_HOST
    # config, not the client — so the UI needn't ask and clients can't inject one.


def _require_sub(request: Request) -> str:
    """The caller's verified Cognito sub — the only key credentials are stored under."""
    user = require_current_user(request)
    if not user.sub:
        # Enforced auth always carries sub; a missing one means auth is not
        # actually enforced, which must never reach the token store.
        raise HTTPException(status_code=403, detail="A verified user identity is required.")
    return user.sub


def _get_store() -> GitCredentialStore:
    return GitCredentialStore()


def _enabled_adapter(provider: str) -> GitProvider:
    """Resolve a provider that is enabled on this deployment, else HTTP 400.

    Keeps the API in step with the UI, which only lists enabled providers.
    """
    if (provider or "").strip().lower() not in enabled_provider_ids():
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported provider '{provider}'. Enabled: {', '.join(enabled_provider_ids())}",
        )
    return provider_by_id(provider)


@router.get("")
async def list_git_credentials(request: Request) -> list[dict]:
    """List the caller's saved providers (never a token)."""
    sub = _require_sub(request)
    try:
        return _get_store().list_providers(sub)
    except StorageError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.put("/{provider}")
async def save_git_credential(provider: str, body: GitCredentialRequest, request: Request) -> dict:
    """Validate a token against the provider, then encrypt-and-store it."""
    sub = _require_sub(request)
    adapter = _enabled_adapter(provider)
    # Host comes from operator config (*_CUSTOM_HOST), not the client: the custom
    # host if one is set for this provider, else None (cloud).
    host = configured_self_hosted_host(adapter.id)

    try:
        identity = adapter.validate_token(body.token, host=host)
    except TokenInvalid:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    except ProviderError as e:
        # Provider unreachable / unexpected status — not the user's fault.
        raise HTTPException(status_code=502, detail=str(e))

    username = body.username or identity.get("username", "")
    try:
        _get_store().put(
            sub=sub,
            provider=adapter.id,
            username=username,
            token=body.token,
            host=host,
        )
    except StorageError as e:
        raise HTTPException(status_code=503, detail=str(e))

    invalidate_user(sub, adapter.id)  # a new token clears cached permissions

    return {"provider": adapter.id, "username": username, "status": "connected"}


@router.post("/{provider}/test")
async def test_git_credential(provider: str, request: Request) -> dict:
    """Re-validate the stored token for a saved provider."""
    sub = _require_sub(request)
    adapter = _enabled_adapter(provider)

    store = _get_store()
    try:
        token = store.get_token(sub, adapter.id)
    except StorageError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not token:
        raise HTTPException(status_code=404, detail=f"No {adapter.id} credential to test")

    host = configured_self_hosted_host(adapter.id)
    try:
        identity = adapter.validate_token(token, host=host)
    except TokenInvalid:
        raise HTTPException(status_code=400, detail="Stored token is invalid or expired — reconnect")
    except ProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {"provider": adapter.id, "username": identity.get("username", ""), "status": "ok"}


@router.delete("/{provider}")
async def delete_git_credential(provider: str, request: Request) -> dict:
    """Remove the caller's credential for a provider."""
    sub = _require_sub(request)
    pid = (provider or "").strip().lower()
    try:
        removed = _get_store().delete(sub, pid)
    except StorageError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not removed:
        raise HTTPException(status_code=404, detail=f"No {pid} credential to delete")

    invalidate_user(sub, pid)  # clear cached permissions

    return {"provider": pid, "status": "deleted"}
