"""The gate: decide READ/WRITE on a space from the caller's git permission.

  private git   READ: can see the code    WRITE: maintain/admin
  public git    READ: any signed-in user  WRITE: creator / maintain-admin / admin
  zip           READ: uploader            WRITE: uploader
  unattributable             nobody (fail closed)

WRITE checks READ first, so a failed READ returns the READ error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from ..core.config import get_config
from ..core.logging_utils import StorageError, get_logger
from ..gitcreds.providers import (
    ProviderError,
    ProviderListingUnsupported,
    TokenInvalid,
    get_provider,
    list_accessible_repos,
    provider_for_host,
    saved_token_matches_repo,
)
from ..gitcreds.store import GitCredentialStore
from . import ladder
from .cache import MISS, build_key, get_cache
from .classify import effective_visibility, is_zip_space
from .errors import AuthorizationUnavailable, CredentialExpired, ReadDenied, WriteDenied
from .repo_key import parse_repo, repo_key
from .startup import require_authorization

if TYPE_CHECKING:
    from .context import AuthContext

logger = get_logger(__name__)


def _log_decision(
    gate: str, result: str, space: Optional[str] = None, reason: Optional[str] = None
) -> None:
    """Deny at INFO (audit signal); allow at DEBUG (fires per space on every list)."""
    log = logger.info if result == "deny" else logger.debug
    log(
        "authz.decision gate=%s space=%s result=%s reason=%s",
        gate,
        space or "-",
        result,
        reason or "-",
    )


def _log_level(provider: Optional[str], result: str, cache: str) -> None:
    """DEBUG detail for the git-permission path (diagnostics, not the decision)."""
    logger.debug("authz.repo_level provider=%s result=%s cache=%s", provider or "-", result, cache)


def _space_coords(record: dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """``(provider_id, host, repo_key)`` from stored fields or git_url. Any may be None."""
    git_url = record.get("git_url")
    provider_id = record.get("provider") or get_provider(git_url or "")
    key = record.get("repo_key") or repo_key(git_url)
    parsed = parse_repo(git_url)
    host = parsed[0] if parsed else None
    return provider_id, host, key


def is_space_owner(ctx: "AuthContext", record: dict[str, Any]) -> bool:
    """Owner by ``created_by_sub``, else legacy ``created_by`` email (case-insensitive)."""
    created_sub = record.get("created_by_sub")
    if created_sub:
        return bool(ctx.sub) and created_sub == ctx.sub
    created_by = record.get("created_by")
    return (
        bool(created_by)
        and bool(ctx.email)
        and str(created_by).strip().lower() == ctx.email.strip().lower()
    )


def _kms_configured() -> bool:
    """True when per-user tokens can be stored/read (KMS key is set)."""
    return bool(str(get_config("gitcreds.kms_key_arn", "") or "").strip())


def _map_level(provider_id: str, native_perm: Any) -> str:
    if provider_id == "github":
        return ladder.github_level(native_perm)
    if provider_id == "gitlab":
        return ladder.gitlab_level(native_perm)
    if provider_id == "bitbucket":
        return ladder.bitbucket_level(native_perm)
    return ladder.NONE


def repo_level(
    ctx: "AuthContext", provider_id: str, host: Optional[str], key: str, git_url: Optional[str]
) -> str:
    """The caller's level for one repo, via the per-user cache.

    Cache hit = no store read, no provider call. On a miss: flagged token →
    expired; no token → none (cached as an empty map); 401 → mark invalid +
    expired; provider error → unavailable. Expired/unavailable are never cached.
    """
    # No usable credential system → only public spaces are reachable; a private
    # repo gets no access (not a 503). provider_for_host returns None when the
    # provider is disabled OR the self-hosted host isn't configured — both
    # permanent config states. (A configured host that is merely down 503s later,
    # from the listing call.)
    if not _kms_configured() or provider_for_host(host or "") is None:
        _log_level(provider_id, "no_creds", "skip")
        return ladder.NONE

    cache = get_cache()
    cache_key = build_key(ctx.sub or "", provider_id, host or "")
    cached = cache.get(cache_key)
    if cached is not MISS:
        level = _map_level(provider_id, cached.get(key)) if key in cached else ladder.NONE
        _log_level(provider_id, level, "hit")
        return level

    # Already expired this request: a list would otherwise re-read the store per private space.
    if provider_id in ctx.expired_providers:
        _log_level(provider_id, "expired", "request-memo")
        raise CredentialExpired(provider_id)

    store = GitCredentialStore()
    # One read for invalid flag + host + token (was two), so a tokenless caller
    # doesn't pay two S3 round-trips per private space.
    try:
        invalid, saved_host, token = store.get_credential_state(ctx.sub or "", provider_id)
    except StorageError as e:
        raise AuthorizationUnavailable(provider=provider_id) from e
    if invalid:
        ctx.expired_providers.add(provider_id)
        _log_level(provider_id, "expired", "miss")
        raise CredentialExpired(provider_id)
    # Only use the token if it was saved for this repo's host — never send a
    # cloud token to a self-hosted host or vice versa.
    if not token or not saved_token_matches_repo(provider_id, saved_host, git_url or ""):
        # Negatively cache the empty result so a tokenless caller stops re-reading
        # the store per private space. Cleared on token save/delete (invalidate_user).
        cache.put(cache_key, {})
        _log_level(provider_id, "no_token", "skip")
        return ladder.NONE

    try:
        listed = list_accessible_repos(provider_id, token, host)
    except TokenInvalid as e:
        store.mark_invalid(ctx.sub or "", provider_id)
        ctx.expired_providers.add(provider_id)
        _log_level(provider_id, "token_invalid", "miss")
        raise CredentialExpired(provider_id) from e
    except ProviderListingUnsupported:
        # Permanent (this host can't list perms) → deny, don't 503. Negatively
        # cache so we don't re-attempt the unsupported listing per space.
        cache.put(cache_key, {})
        _log_level(provider_id, "listing_unsupported", "miss")
        return ladder.NONE
    except ProviderError as e:
        _log_level(provider_id, "unavailable", "miss")
        raise AuthorizationUnavailable(provider=provider_id) from e
    cache.put(cache_key, listed)

    level = _map_level(provider_id, listed.get(key)) if key in listed else ladder.NONE
    _log_level(provider_id, level, "miss")
    return level


def can_read(ctx: "AuthContext", record: dict[str, Any]) -> None:
    """Raise unless the caller may READ this space. No-op when the switch is off."""
    if not require_authorization():
        return
    space = record.get("space_id")
    if record.get("authz_unresolved"):
        # Legacy space with no status.json: provenance unknown, so fail closed
        # rather than let the no-git_url heuristic read it as a zip.
        _log_decision("read", "deny", space, "unresolved")
        raise ReadDenied()
    if is_zip_space(record):
        # An uploaded zip has no repo to check membership against, so it is owner-only:
        if is_space_owner(ctx, record):
            return _log_decision("read", "allow", space, "zip-owner")
        _log_decision("read", "deny", space, "zip-not-owner")
        raise ReadDenied()
    if effective_visibility(record) == "public":
        return _log_decision("read", "allow", space, "public")
    provider_id, host, key = _space_coords(record)
    if not provider_id or not key:
        _log_decision("read", "deny", space, "unattributable")
        raise ReadDenied()
    level = repo_level(ctx, provider_id, host, key, record.get("git_url"))
    if not ladder.can_read(level):
        _log_decision("read", "deny", space, f"level={level}")
        raise ReadDenied()
    _log_decision("read", "allow", space, f"level={level}")


def can_write(ctx: "AuthContext", record: dict[str, Any]) -> None:
    """Raise unless the caller may WRITE. Checks READ first. No-op when off."""
    if not require_authorization():
        return
    can_read(ctx, record)  # a failed READ returns the READ error
    space = record.get("space_id")
    if is_zip_space(record):
        if is_space_owner(ctx, record):
            return _log_decision("write", "allow", space, "owner")
        _log_decision("write", "deny", space, "zip-not-owner")
        raise WriteDenied()
    provider_id, host, key = _space_coords(record)
    if effective_visibility(record) == "public" and is_space_owner(ctx, record):
        return _log_decision("write", "allow", space, "public-creator")  # no provider call
    if not provider_id or not key:
        _log_decision("write", "deny", space, "unattributable")
        raise WriteDenied()
    level = repo_level(ctx, provider_id, host, key, record.get("git_url"))
    if not ladder.can_write(level):
        _log_decision("write", "deny", space, f"level={level}")
        raise WriteDenied()
    _log_decision("write", "allow", space, f"level={level}")


def can_read_bool(ctx: "AuthContext", record: dict[str, Any]) -> bool:
    """Non-raising READ check for list/scope filtering.

    A plain denial is False; an expired token or provider-down propagates so a
    multi-space caller can tell "skip" from "can't serve".
    """
    try:
        can_read(ctx, record)
        return True
    except ReadDenied:
        return False


def can_list(ctx: "AuthContext", record: dict[str, Any]) -> bool:
    """READ check for "all readable" listings: an expired token counts as no token.

    So public spaces stay listed when a token expires. A down provider still
    propagates; explicit picks keep can_read_bool's expired error (reconnect prompt).
    """
    try:
        return can_read_bool(ctx, record)
    except CredentialExpired as e:
        _log_decision("read", "deny", record.get("space_id"), f"expired:{e.provider}")
        return False
