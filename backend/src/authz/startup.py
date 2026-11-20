"""Authorization config accessors and startup checks."""

from __future__ import annotations

from ..core.config import get_config, get_positive_int
from ..core.logging_utils import get_logger
from ..gitcreds.providers import enabled_provider_ids

logger = get_logger(__name__)


def require_authorization() -> bool:
    """True when space authorization is switched on."""
    value = get_config("authz.require_authorization", False)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _auth_required() -> bool:
    """Whether authentication is on, from the auth layer's single reader."""
    from ..api.auth import is_auth_required

    return is_auth_required()


def validate_authz_startup() -> None:
    """Log the authz config at startup and warn on gaps. No-op when off."""
    if not require_authorization():
        logger.info("Space authorization: DISABLED")
        return

    # Authorization acts on a caller identity. With auth off, every caller is the
    # same local-dev identity, so the gate can't tell users apart — handy for
    # local testing, never safe on a shared instance.
    if not _auth_required():
        logger.warning(
            "Authorization ON with CODEKB_REQUIRE_AUTHENTICATION=false — all callers share "
            "the local-dev identity. Local testing only; do NOT use on a shared instance."
        )

    # Validate the cache-sizing keys now so a bad override fails fast at startup,
    # not as a per-request 500 later.
    get_positive_int("authz.repo_cache_ttl_hours")
    get_positive_int("authz.repo_cache_maxsize")

    providers = enabled_provider_ids()
    has_kms = bool(str(get_config("gitcreds.kms_key_arn", "") or "").strip())
    logger.info(
        "Space authorization: ENABLED (providers=%s, kms=%s)",
        ",".join(providers) or "none",
        "set" if has_kms else "unset",
    )

    # Providers and KMS only matter for private repos and git-permission writes.
    # A public/internal-only deployment needs neither, so warn rather than refuse.
    if not providers:
        logger.warning(
            "Authorization ON with no git providers — only public/internal and zip "
            "spaces are reachable; private repos can't be authorized."
        )
    if not has_kms:
        logger.warning(
            "Authorization ON without CODEKB_GIT_CREDENTIALS_KMS_KEY_ARN — per-user "
            "tokens can't be stored; private reads and git-maintainer writes won't work."
        )
