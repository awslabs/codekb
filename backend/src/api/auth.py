"""JWT authentication for FastAPI.

When Cognito is configured (CODEKB_COGNITO_USER_POOL_ID set), tokens are
verified against the Cognito JWKS endpoint. Only when authentication is
explicitly disabled (CODEKB_REQUIRE_AUTHENTICATION=false, local dev) does it
fall back to unverified payload decoding with X-User-Identity header fallback.
"""

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import Request

from ..core.config import get_config

logger = logging.getLogger("codekb.api.auth")

# ── JWKS refresh rate-limiting ────────────────────────────────────────────
_JWKS_REFRESH_INTERVAL_SECONDS = 30  # minimum seconds between forced JWKS refreshes
_last_jwks_refresh_time: float = 0.0

# ── Auth requirement mode ──────────────────────────────────────────────────
# authn.require_authentication (env CODEKB_REQUIRE_AUTHENTICATION, or its
# backward-compatible alias CODEKB_REQUIRE_AUTH) controls fail-open vs
# fail-closed behavior. Resolved through the config env-mapping layer: the
# canonical name wins over the alias, and only the literal "false" disables it:
#   true (default)  → auth MUST be functional; if JWKS can't init, service refuses /api and /mcp
#   false           → explicit opt-in to no-auth mode (local dev ONLY)
_REQUIRE_AUTH = bool(get_config("authn.require_authentication", True))

# ── Cognito JWKS setup ───────────────────────────────────────────────────

_COGNITO_USER_POOL_ID = os.environ.get("CODEKB_COGNITO_USER_POOL_ID")
_COGNITO_REGION = os.environ.get(
    "CODEKB_COGNITO_REGION", os.environ.get("CODEKB_S3_REGION", "us-east-1")
)
_COGNITO_DOMAIN_URL = os.environ.get("CODEKB_COGNITO_DOMAIN_URL")
_COGNITO_MCP_CLIENT_ID = os.environ.get("CODEKB_COGNITO_MCP_CLIENT_ID")
_COGNITO_APP_CLIENT_IDS = [
    cid.strip()
    for cid in os.environ.get("CODEKB_COGNITO_APP_CLIENT_IDS", "").split(",")
    if cid.strip()
]
_PUBLIC_DOMAIN = os.environ.get("CODEKB_PUBLIC_DOMAIN")
_COGNITO_IDP_NAME = os.environ.get("CODEKB_COGNITO_IDP_NAME") or None

# Expected issuer URL for this user pool
_COGNITO_ISSUER = (
    f"https://cognito-idp.{_COGNITO_REGION}.amazonaws.com/{_COGNITO_USER_POOL_ID}"
    if _COGNITO_USER_POOL_ID
    else None
)

_jwks_client = None

if _COGNITO_USER_POOL_ID:
    try:
        from jwt import PyJWKClient

        _jwks_url = (
            f"https://cognito-idp.{_COGNITO_REGION}.amazonaws.com"
            f"/{_COGNITO_USER_POOL_ID}/.well-known/jwks.json"
        )
        _jwks_client = PyJWKClient(_jwks_url, cache_keys=True, timeout=10)
        logger.info(f"JWKS verification enabled: {_jwks_url}")
    except Exception as e:
        logger.warning(f"Failed to initialize JWKS client: {e}. Falling back to unverified mode.")
        _jwks_client = None

# ── Startup security warnings and fail-closed enforcement ──────────────────

# Without the app client IDs we can't check a token's audience, so any token
# signed by this user pool is accepted — including one issued for a different
# app client in the same pool. Treat it like a missing pool ID: refuse to serve
# rather than run with the check switched off.
_audience_unvalidatable = _jwks_client is not None and not _COGNITO_APP_CLIENT_IDS

# Determine whether auth is in a broken state that should block requests.
# This is True when auth is REQUIRED but couldn't initialize, or when a control
# it depends on is unconfigured.
_auth_misconfigured: bool = False

if _jwks_client and not _audience_unvalidatable:
    logger.info(
        "╔══════════════════════════════════════════════════════════════════╗\n"
        "║  AUTH MODE: ENFORCED — JWT signature verification active         ║\n"
        "╚══════════════════════════════════════════════════════════════════╝"
    )
elif _audience_unvalidatable and not _REQUIRE_AUTH:
    # Signatures are still verified; only the audience check is off.
    logger.warning(
        "SECURITY: CODEKB_COGNITO_APP_CLIENT_IDS is not set. "
        "Audience/client_id validation is DISABLED."
    )
elif _REQUIRE_AUTH:
    # Auth is required but can't be enforced correctly — FAIL CLOSED
    _auth_misconfigured = True
    logger.critical(
        "╔══════════════════════════════════════════════════════════════════╗\n"
        "║  FATAL: CODEKB_REQUIRE_AUTHENTICATION=true but authentication    ║\n"
        "║  cannot be enforced. CODEKB_COGNITO_USER_POOL_ID or              ║\n"
        "║  CODEKB_COGNITO_APP_CLIENT_IDS is missing, or JWKS init failed.  ║\n"
        "║                                                                  ║\n"
        "║  ALL /api/* and /mcp/* requests will be REJECTED with 503.       ║\n"
        "║                                                                  ║\n"
        "║  To run without auth (LOCAL DEV ONLY), explicitly set:           ║\n"
        "║    CODEKB_REQUIRE_AUTHENTICATION=false                           ║\n"
        "╚══════════════════════════════════════════════════════════════════╝"
    )
else:
    # Auth explicitly disabled — local dev mode
    logger.warning(
        "╔══════════════════════════════════════════════════════════════════╗\n"
        "║  ⚠ AUTH MODE: DISABLED (CODEKB_REQUIRE_AUTHENTICATION=false)     ║\n"
        "║  All endpoints are PUBLIC. Identity from unverified JWT or       ║\n"
        "║  X-User-Identity header. DO NOT USE IN PRODUCTION.               ║\n"
        "╚══════════════════════════════════════════════════════════════════╝"
    )


def is_auth_enforced() -> bool:
    """Return True when server-side JWT verification is active."""
    return _jwks_client is not None


def is_auth_required() -> bool:
    """True when authentication is on (requests must be authenticated).

    Controlled by CODEKB_REQUIRE_AUTHENTICATION (canonical) or its
    backward-compatible alias CODEKB_REQUIRE_AUTH.
    """
    return _REQUIRE_AUTH


def is_auth_misconfigured() -> bool:
    """Return True when auth is required but cannot be enforced correctly.

    Either CODEKB_COGNITO_USER_POOL_ID is missing / JWKS failed to initialize,
    or CODEKB_COGNITO_APP_CLIENT_IDS is missing (no audience validation).

    When True, all /api/* and /mcp/* requests should be rejected with 503.
    This prevents silent fail-open when an env var is missing in production.
    """
    return _auth_misconfigured


def is_mcp_path(path: str) -> bool:
    """True for the MCP mount: ``/mcp`` exactly as well as the ``/mcp/`` subtree.

    MCP's streamable-HTTP clients post to ``/mcp`` with no trailing slash, so a
    prefix check on ``/mcp/`` alone would miss it.
    """
    return path == "/mcp" or path.startswith("/mcp/")


def is_protected_path(path: str) -> bool:
    """True for paths that require authentication (``/api/*`` and MCP).

    The single definition shared by the fail-closed block and the auth
    enforcement middleware, so the two cannot drift apart.
    """
    return path.startswith("/api/") or is_mcp_path(path)


def is_mcp_oauth_configured() -> bool:
    """Return True when MCP OAuth metadata endpoints should be served."""
    return bool(_COGNITO_DOMAIN_URL and _COGNITO_MCP_CLIENT_ID and _jwks_client)


def get_cognito_config() -> dict:
    """Return Cognito configuration for OAuth metadata endpoints."""
    return {
        "domain_url": _COGNITO_DOMAIN_URL,
        "mcp_client_id": _COGNITO_MCP_CLIENT_ID,
        "region": _COGNITO_REGION,
        "user_pool_id": _COGNITO_USER_POOL_ID,
        "public_base_url": f"https://{_PUBLIC_DOMAIN}" if _PUBLIC_DOMAIN else None,
        "idp_name": _COGNITO_IDP_NAME,
    }


def git_creds_url() -> str:
    """Git credentials page URL for the expired-token message — absolute with a
    public domain, else the relative SPA path."""
    base = f"https://{_PUBLIC_DOMAIN}" if _PUBLIC_DOMAIN else ""
    return f"{base}/git-creds"


@dataclass
class UserIdentity:
    email: str
    sub: str | None = None  # immutable Cognito user ID — use for authorization
    given_name: str | None = None
    family_name: str | None = None

    @property
    def display_name(self) -> str:
        if self.given_name and self.family_name:
            return f"{self.given_name} {self.family_name}"
        return self.email


ANONYMOUS = UserIdentity(email="anonymous")


def _decode_jwt_payload(token: str) -> dict:
    """Base64-decode the JWT payload (middle segment) without verification."""
    try:
        payload_b64 = token.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception as e:
        logger.debug(f"Failed to decode JWT payload: {e}")
        return {}


def _extract_identity(claims: dict, verified: bool = False) -> Optional[UserIdentity]:
    """Extract UserIdentity from JWT claims dict.

    Args:
        claims: Decoded JWT payload.
        verified: True if claims came from a cryptographically verified token.
                  When True, enforces email_verified check for ID tokens.
    """
    if not claims:
        return None

    sub = claims.get("sub")  # immutable user identifier

    # ID token path — has email directly
    email = claims.get("email")
    if email:
        # Enforce email_verified only for native users. Federated users (external
        # OIDC IdP) are trusted via the corporate directory; Cognito sets the
        # 'identities' claim exclusively for them, so it's the reliable signal.
        is_federated = bool(claims.get("identities"))
        if verified and not is_federated and not claims.get("email_verified", False):
            logger.warning(f"JWT rejected: email is not verified (email_verified=false), sub={sub}")
            return None
        return UserIdentity(
            email=email,
            sub=sub,
            given_name=claims.get("given_name"),
            family_name=claims.get("family_name"),
        )

    # Access token path — no email claim. Use the username alias as the identity;
    # authorization keys on sub, not email.
    username = claims.get("username", "")
    if username:
        alias = username.split("_", 1)[-1] if "_" in username else username
        return UserIdentity(email=alias, sub=sub)

    return None


def verify_token(token: str) -> Optional[UserIdentity]:
    """Verify JWT signature via JWKS and return identity, or None on failure.

    Only called when Cognito is configured.
    If the signing key isn't found in cache, refreshes JWKS keys and retries once.
    """
    import jwt as pyjwt

    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
    except Exception:
        # Key not in cache — rate-limited refresh and retry
        global _last_jwks_refresh_time
        now = time.monotonic()
        if now - _last_jwks_refresh_time < _JWKS_REFRESH_INTERVAL_SECONDS:
            logger.warning(
                "JWKS refresh rate-limited — unknown kid in token (possible key-flooding attack)"
            )
            return None
        try:
            _last_jwks_refresh_time = now
            _jwks_client.fetch_data()
            signing_key = _jwks_client.get_signing_key_from_jwt(token)
            logger.info("JWKS keys refreshed successfully — new signing key resolved")
        except Exception as e:
            logger.warning(f"JWKS key lookup failed after refresh: {e}")
            return None

    # ── Assert signing key is an RSA public key (defense against algorithm confusion) ──
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

    if not isinstance(signing_key.key, RSAPublicKey):
        logger.warning(
            f"JWT rejected: signing key is {type(signing_key.key).__name__}, expected RSAPublicKey"
        )
        return None

    try:
        # Cognito access tokens put the client_id in the payload (not 'aud'),
        # while ID tokens use 'aud'. We disable PyJWT's built-in aud check and
        # validate the audience/client_id manually after decoding.
        claims = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=_COGNITO_ISSUER,
            options={
                "verify_aud": False,
                "verify_iss": True,
                "verify_exp": True,
                "verify_iat": True,
            },
        )

        # ── Validate token_use ────────────────────────────────────────────
        token_use = claims.get("token_use")
        if token_use not in ("id", "access"):
            logger.warning(f"JWT rejected: unexpected token_use '{token_use}'")
            return None

        # ── Validate audience / client_id ─────────────────────────────────
        if _COGNITO_APP_CLIENT_IDS:
            if token_use == "id":  # nosec B105
                aud = claims.get("aud")
                if aud not in _COGNITO_APP_CLIENT_IDS:
                    logger.warning(f"JWT rejected: ID token aud '{aud}' not in allowed client IDs")
                    return None
            else:  # access token
                client_id = claims.get("client_id")
                if client_id not in _COGNITO_APP_CLIENT_IDS:
                    logger.warning(
                        f"JWT rejected: access token client_id '{client_id}' not in allowed client IDs"
                    )
                    return None
        elif _REQUIRE_AUTH:
            # Unreachable via HTTP (startup fails closed first), but ensures no
            # caller can accept a token whose audience was never checked.
            logger.error("JWT rejected: CODEKB_COGNITO_APP_CLIENT_IDS is not configured")
            return None

        return _extract_identity(claims, verified=True)
    except pyjwt.ExpiredSignatureError:
        logger.debug("JWT expired")
        return None
    except pyjwt.InvalidIssuerError:
        logger.warning("JWT rejected: issuer mismatch")
        return None
    except pyjwt.InvalidTokenError as e:
        logger.debug(f"JWT verification failed: {e}")
        return None
    except Exception as e:
        logger.warning(f"Unexpected error verifying JWT: {e}")
        return None


def get_current_user(request: Request) -> UserIdentity | None:
    """Extract and verify user identity from the request.

    When Cognito is configured: verifies JWT signature → returns identity or
    None. Only cryptographically verified JWTs can establish identity.

    When not configured and authentication is explicitly disabled
    (CODEKB_REQUIRE_AUTHENTICATION=false, local dev): decodes the JWT payload
    without verification, or uses the X-User-Identity header as a convenience
    fallback.

    When not configured but authentication is required (misconfigured): returns
    None. An unverified token or header must never establish identity just
    because the verifier failed to initialize.

    Returns None when no identity can be established — the auth middleware
    rejects these requests when enforcement is active.
    """
    # Unverified identity is a local-dev convenience only. Without a verifier
    # and with auth required, nothing the caller sends can be trusted.
    unverified_allowed = not _jwks_client and not _REQUIRE_AUTH

    # Accept both "Bearer <token>" (RFC 6750, used by the SPA, MCP clients, and
    # the container) and a bare "<token>" for clients that send the raw JWT. Both
    # are fully verified below, so the fallback costs nothing.
    auth_header = request.headers.get("authorization", "").strip()
    token = ""  # nosec B105
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif auth_header.count(".") == 2:
        # A bare JWT (header.payload.signature) with no auth scheme.
        token = auth_header
    if token:
        if _jwks_client:
            # Auth enforced: verify signature.
            # This is the ONLY path that can establish identity.
            return verify_token(token)
        elif unverified_allowed:
            # Local dev: unverified decode
            claims = _decode_jwt_payload(token)
            user = _extract_identity(claims)
            if user:
                return user

    # X-User-Identity header fallback — only in explicit no-auth mode (local dev).
    # When Cognito is configured, or auth is required but could not initialize,
    # this header is never trusted; identity can only come from a
    # cryptographically verified JWT above.
    if unverified_allowed:
        user_identity = request.headers.get("x-user-identity", "")
        if user_identity:
            # No-auth mode has no Cognito sub; use the identity so git creds work.
            return UserIdentity(email=user_identity, sub=user_identity)

    return None


def require_current_user(request: Request) -> UserIdentity:
    """Like get_current_user, but raises HTTP 401 if identity cannot be established.

    Use this in route handlers as a defense-in-depth measure. Even though the
    auth middleware should catch unauthenticated requests, this prevents a null
    user from propagating if middleware is bypassed or misconfigured.

    In local dev mode (CODEKB_REQUIRE_AUTHENTICATION=false), falls back to a
    default "local-dev" identity so endpoints remain usable without a token.
    """
    from fastapi import HTTPException

    user = get_current_user(request)
    if user is None:
        if not _REQUIRE_AUTH:
            # Local dev: no token provided, use a default identity
            logger.debug("No credentials provided — using local-dev fallback identity")
            return UserIdentity(email="local-dev@localhost", sub="local-dev")
        logger.warning(
            f"Authentication required but no valid identity established "
            f"(path={request.url.path}, method={request.method})"
        )
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def current_auth_context(request: Request):
    """Build the caller's AuthContext, cached on ``request.state`` for this one
    request. So the gates in a request share one status memo (and identity work
    runs once). Each new request gets a fresh state; nothing carries over.
    """
    from ..authz.context import AuthContext

    # Only a real cached context short-circuits (a mock's .state can be truthy).
    state = getattr(request, "state", None)
    cached = getattr(state, "auth_context", None) if state is not None else None
    if isinstance(cached, AuthContext):
        return cached
    user = require_current_user(request)
    actx = AuthContext.from_identity(user)
    if state is not None:
        try:
            state.auth_context = actx
        except Exception:  # read-only/odd state object — fall back to per-call contexts
            pass
    return actx
