"""FastAPI application for CodeKB REST API.

Local dev (API only):  uvicorn codekb.api.app:app --reload --port 8000
Local dev (UI + API):  cd web && npm run dev  (proxies /api → localhost:8000)
AWS:                   ECS Fargate running the same uvicorn command
"""

import logging
import os
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse as _urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.semconv.attributes.http_attributes import (
    HTTP_REQUEST_METHOD,
    HTTP_RESPONSE_STATUS_CODE,
    HTTP_ROUTE,
)
from opentelemetry.semconv.attributes.url_attributes import URL_PATH

from .routes import (
    spaces,
    analysis,
    query,
    metrics,
    agent,
    search,
    hyperspaces,
    config as config_routes,
    git_credentials,
)
from ..mcp.server import get_mcp_app, get_mcp_session_manager
from ..service.codekb_service import CodeKBService
from ..service.errors import PipelineCapacityError
from ..service.pipeline_pool import pipeline_pool
from ..service.source_provider import sweep_source_cache_scratch
from ..core.logging_utils import (
    setup_logging,
    format_payload,
)
from ..core.config import load_config, get_config
from ..core.bedrock_guardrail import validate_guardrail_config
from ..core.llm_startup_check import probe_llm_settings, validate_llm_settings
from ..gitcreds.providers import validate_provider_config
from ..authz.errors import (
    AuthorizationUnavailable,
    AuthzError,
    CredentialExpired,
    HyperspaceAccessDenied,
)
from .auth import (
    get_current_user,
    git_creds_url,
    is_auth_enforced,
    is_auth_misconfigured,
    is_mcp_oauth_configured,
    is_mcp_path,
    is_protected_path,
    get_cognito_config,
)

# Initialize config and logging
load_config()
setup_logging()
logger = logging.getLogger("codekb.api")

# Reject an unusable git-provider list here, while the process is still starting.
validate_provider_config()

# Refuse to boot on a half-configured guardrail; warn if it's disabled entirely.
validate_guardrail_config()

# Refuse to boot on LLM settings CodeKB can tell are invalid (no model, a bad
# max_tokens). Whether the model accepts them is probed in lifespan.
_LLM_SETTINGS = validate_llm_settings()

# Refuse an unsafe authorization config. No-op when the switch is off.
from ..authz.startup import validate_authz_startup  # noqa: E402

validate_authz_startup()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build the shared CodeKBService while the process is still single-threaded,
    # so the first concurrent requests can't race its lazy construction (and any
    # config error surfaces here at startup rather than on the first request).
    CodeKBService.instance()

    # Start the pipeline pool here too, so a bad CODEKB_PIPELINE_POOL_SIZE fails
    # startup rather than every job-starting request.
    pipeline_pool()

    # Ask Bedrock whether the models accept the configured settings. Raises
    # (failing startup) only when Bedrock rejects them; any other Bedrock
    # problem is a warning, so an outage can't keep the API from starting.
    probe_llm_settings(_LLM_SETTINGS)

    # Reclaim leftover disk scratch. Runs before the first request.
    try:
        sweep_source_cache_scratch()
    except Exception as e:
        logger.warning(f"Startup source-cache sweep failed: {e}", exc_info=True)

    session_manager = get_mcp_session_manager()
    # The flush is in finally so buffered records still reach the exporter when
    # shutdown raises — those are the records a crash needs most.
    from ..core.observability import shutdown_logging_provider

    try:
        async with session_manager.run():
            logger.info("MCP session manager started")
            yield
        logger.info("MCP session manager stopped")
    finally:
        shutdown_logging_provider()


app = FastAPI(
    title="CodeKB API",
    description="REST API for CodeKB space management (Cloudscape Web UI backend)",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all for unhandled exceptions — return JSON instead of HTML 500."""
    logger.error(
        f"Unhandled exception on {request.method} {request.url.path}: {exc}", exc_info=True
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


@app.exception_handler(AuthzError)
async def authz_exception_handler(request: Request, exc: AuthzError):
    """Map an authorization failure to its error code + HTTP status."""
    body: dict = {"error": exc.code, "message": str(exc)}
    headers: dict = {}
    if isinstance(exc, CredentialExpired):
        body["provider"] = exc.provider
        body["update_url"] = git_creds_url()
    elif isinstance(exc, HyperspaceAccessDenied):
        body["inaccessible_space_ids"] = exc.inaccessible_space_ids
    elif isinstance(exc, AuthorizationUnavailable):
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(status_code=exc.http_status, content=body, headers=headers)


@app.exception_handler(PipelineCapacityError)
async def pipeline_capacity_exception_handler(request: Request, exc: PipelineCapacityError):
    """Every pipeline slot on this process is busy: 429, retry later."""
    return JSONResponse(
        status_code=429,
        # "detail" is what the frontend's ApiError shows. "error" is a stable code,
        # as in the AuthzError bodies, so automation can tell this 429 apart from
        # one sent by a proxy or WAF.
        content={"error": "pipeline_capacity", "detail": str(exc)},
        headers={"Retry-After": str(exc.retry_after)},
    )


def _route_template(request: Request) -> str | None:
    """Matched route template. Absent for 404s and for the /mcp mount."""
    return getattr(request.scope.get("route"), "path_format", None)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every request with method, path, status, and timing.

    At DEBUG the full request and response bodies are dumped; at any other level
    a short preview keeps the log readable.
    """
    dump_bodies = logger.isEnabledFor(logging.DEBUG)
    start = time.time()
    body = await request.body()

    # Paths whose request/response bodies carry credentials (OAuth tokens, auth
    # codes). Never log their bodies, whatever the level.
    sensitive_body = request.url.path.startswith("/oauth2/")

    if dump_bodies:
        # Headers are safe to dump at DEBUG, but redact obvious auth material.
        safe_headers = {
            k: ("<redacted>" if k.lower() in {"authorization", "cookie", "x-api-key"} else v)
            for k, v in request.headers.items()
        }
        logger.debug(
            "→ %s %s\n  query=%s\n  headers=%s\n  body=%s",
            request.method,
            request.url.path,
            dict(request.query_params),
            safe_headers,
            "<redacted>" if sensitive_body else (format_payload(body) if body else "(empty)"),
        )
    else:
        logger.debug(
            "→ %s %s  body=%s",
            request.method,
            request.url.path,
            "<redacted>"
            if sensitive_body
            else (body.decode(errors="replace")[:500] if body else "(empty)"),
        )

    # Exported as queryable fields by the OTel handler. Built before call_next so
    # the failure path can report them too. Names are semconv 0.65b0.
    log_attributes: dict[str, str | int | float] = {
        HTTP_REQUEST_METHOD: request.method,
        URL_PATH: request.url.path,
    }

    try:
        response = await call_next(request)
    except Exception as exc:
        elapsed = (time.time() - start) * 1000
        log_attributes[HTTP_RESPONSE_STATUS_CODE] = 500
        log_attributes[ERROR_TYPE] = type(exc).__name__
        log_attributes["codekb.http.server.duration_ms"] = round(elapsed, 1)
        if route := _route_template(request):
            log_attributes[HTTP_ROUTE] = route
        # ServerErrorMiddleware is outside this middleware, so failed requests would
        # otherwise be the only ones absent from the access log. The traceback comes
        # from unhandled_exception_handler, so no exc_info here.
        logger.error(
            "\u2190 %s %s \u2192 500  (%.0fms)",
            request.method,
            request.url.path,
            elapsed,
            extra=log_attributes,
        )
        raise

    elapsed = (time.time() - start) * 1000
    log_attributes[HTTP_RESPONSE_STATUS_CODE] = response.status_code
    # Not a semconv attribute — semconv defines duration only as a metric.
    log_attributes["codekb.http.server.duration_ms"] = round(elapsed, 1)
    if route := _route_template(request):
        log_attributes[HTTP_ROUTE] = route

    # Streaming responses pass through untouched. Draining body_iterator to log the
    # body would withhold the whole turn — the SSE agent chat and the MCP transport
    # mounted at /mcp deliver nothing until their generator finishes.
    is_streaming = "text/event-stream" in response.headers.get(
        "content-type", ""
    ).lower() or request.url.path.startswith("/mcp")

    # At DEBUG we buffer the response body to log it. This adds a small
    # latency cost, so it's opt-in only.
    if dump_bodies and not is_streaming:
        response_bytes = b""
        async for chunk in response.body_iterator:
            response_bytes += chunk
        logger.debug(
            "← %s %s → %d  (%.0fms)\n  response=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
            "<redacted>"
            if sensitive_body
            else (format_payload(response_bytes) if response_bytes else "(empty)"),
            extra=log_attributes,
        )
        # Rebuild the response so FastAPI can deliver it to the client.
        from starlette.responses import Response as StarletteResponse

        new_response = StarletteResponse(
            content=response_bytes,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
        return new_response

    logger.info(
        "← %s %s → %d  (%.0fms)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed,
        extra=log_attributes,
    )
    return response


# ── Fail-closed middleware — blocks all protected requests when auth is misconfigured ──
# This fires when CODEKB_REQUIRE_AUTH=true (default) but Cognito env vars are missing
# or JWKS initialization failed. Prevents silent fail-open in production.
if is_auth_misconfigured():

    @app.middleware("http")
    async def block_misconfigured_auth(request: Request, call_next):
        path = request.url.path
        # Same protected-path test as enforce_auth — including /mcp exactly,
        # where MCP's streamable-HTTP clients post with no trailing slash.
        if is_protected_path(path):
            # Allow health check through so load balancers can detect the problem
            if path == "/api/health":
                return await call_next(request)
            logger.warning(f"Request blocked (auth misconfigured): {request.method} {path}")
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "Service unavailable: authentication is required but could not be initialized. "
                },
            )
        return await call_next(request)

    logger.critical("Auth misconfigured — all /api/* and /mcp/* requests will return 503")


# Auth enforcement middleware — active when Cognito is configured.
# Rejects unauthenticated requests to /api/* and /mcp/* with 401.
# Identity is established ONLY via verified JWT (Bearer token).
if is_auth_enforced():
    _mcp_oauth = is_mcp_oauth_configured()
    _cognito_cfg = get_cognito_config() if _mcp_oauth else None

    @app.middleware("http")
    async def enforce_auth(request: Request, call_next):
        path = request.url.path
        # MCP's streamable-HTTP transport posts to /mcp exactly (no trailing
        # slash), so guard the exact path as well as the /mcp/ subtree — matching
        # only "/mcp/" would let an unauthenticated POST /mcp fall through to the
        # SPA branch below and skip enforcement entirely.
        mcp_request = is_mcp_path(path)

        # Skip auth for health check, OAuth metadata/endpoints, static assets, SPA routes, and OPTIONS
        if (
            request.method == "OPTIONS"
            or path == "/api/health"
            or path.startswith("/.well-known/")
            or path.startswith("/oauth2/")
            or not is_protected_path(path)
        ):
            return await call_next(request)

        # Check for valid authentication (verified JWT only)
        user = get_current_user(request)
        if user is not None:
            return await call_next(request)

        # No valid token — return 401 with WWW-Authenticate for MCP OAuth discovery
        if mcp_request and _mcp_oauth:
            # Use configured public domain for the resource_metadata URL
            base_url = _safe_base_url(request, _cognito_cfg["public_base_url"])
            resource_metadata = f"{base_url}/.well-known/oauth-protected-resource"
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "message": "Authentication required"},
                headers={
                    "WWW-Authenticate": (
                        f'Bearer realm="CodeKB MCP", resource_metadata="{resource_metadata}"'
                    )
                },
            )

        return JSONResponse(status_code=401, content={"detail": "Authentication required"})

    logger.info("Auth enforcement middleware enabled")


# ── Security headers middleware — infra-independent, applies to every response ──
# The container serves the SPA document itself, so these headers protect the HTML
# no matter how the service is fronted (localhost, a customer reverse proxy, an
# ALB, or CloudFront). Values are config-driven (security.headers.*) so a
# deployment can tune or disable them via CODEKB_SECURITY_* env vars without a
# rebuild. Registered last so it runs outermost — headers are added even to auth
# rejections (401/503) produced by the middleware above.
_sec_headers_cfg = get_config("security.headers", {}) or {}
if _sec_headers_cfg.get("enabled", True):
    _csp_value = (_sec_headers_cfg.get("content_security_policy") or "").strip()

    # The SPA uploads a source zip by PUTting straight at a presigned S3 URL,
    # which is cross-origin — a bare `connect-src 'self'` blocks it outright. The
    # bucket differs per deployment, so derive the origin from the same
    # storage.s3 config that mints the presigned URLs rather than baking a domain
    # into the policy template. Grants that one origin, not all of S3.
    _s3_cfg = get_config("storage.s3", {}) or {}
    _s3_bucket = _s3_cfg.get("bucket_name", "")
    _s3_region = _s3_cfg.get("region", "")
    if _csp_value and _s3_bucket and _s3_region and "connect-src " in _csp_value:
        _s3_origin = f"https://{_s3_bucket}.s3.{_s3_region}.amazonaws.com"
        _csp_value = _csp_value.replace("connect-src ", f"connect-src {_s3_origin} ", 1)

    # When the hosted deployment uses Cognito, the SPA exchanges its authorization
    # code at the Cognito hosted-UI /oauth2/token endpoint — a cross-origin fetch.
    # Without that origin in connect-src the browser's CSP blocks the exchange with
    # "Refused to connect", the fetch throws "Failed to fetch", and sign-in hangs
    # on "Signing in…". Derived from the same authn.cognito config the backend uses
    # for its own OAuth calls, so it's exactly the origin the SPA talks to.
    _cognito_domain_url = (get_config("authn.cognito.domain_url", "") or "").strip().rstrip("/")
    if _csp_value and _cognito_domain_url and "connect-src " in _csp_value:
        _csp_value = _csp_value.replace("connect-src ", f"connect-src {_cognito_domain_url} ", 1)

    _csp_header_name = (
        "Content-Security-Policy-Report-Only"
        if _sec_headers_cfg.get("csp_report_only", False)
        else "Content-Security-Policy"
    )
    _hsts_value = (_sec_headers_cfg.get("hsts") or "").strip()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        # Always-safe headers. setdefault() so a fronting proxy that sets a
        # stricter value wins instead of colliding.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        # HSTS only takes effect over HTTPS; opt-in via config so we don't
        # impose it on a plain-HTTP or shared-domain deployment.
        if _hsts_value:
            response.headers.setdefault("Strict-Transport-Security", _hsts_value)
        # Full CSP is the document's XSS backstop — scope it to HTML so JSON and
        # streaming (MCP) responses aren't needlessly decorated.
        if _csp_value and response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault(_csp_header_name, _csp_value)
        return response

    logger.info(
        "Security headers middleware enabled (csp=%s, hsts=%s)",
        "report-only"
        if _sec_headers_cfg.get("csp_report_only", False)
        else ("on" if _csp_value else "off"),
        "on" if _hsts_value else "off",
    )


# Register API route modules
app.include_router(spaces.router, prefix="/api/spaces", tags=["spaces"])
app.include_router(analysis.router, prefix="/api/spaces", tags=["analysis"])
app.include_router(query.router, prefix="/api/spaces", tags=["query"])
app.include_router(metrics.router, prefix="/api/metrics", tags=["metrics"])
app.include_router(search.router, prefix="/api/search", tags=["search"])
app.include_router(agent.router, prefix="/api/agent", tags=["agent"])
app.include_router(hyperspaces.router, prefix="/api/hyperspaces", tags=["hyperspaces"])
app.include_router(config_routes.router, prefix="/api/config", tags=["config"])
app.include_router(
    git_credentials.router, prefix="/api/user-profile/gitcreds", tags=["gitcreds"]
)

# Mount MCP server (streamable HTTP transport) at /mcp — protected by auth middleware above
app.mount("/mcp", get_mcp_app())


# ── OAuth proxy endpoints (MCP spec compliance) ─────────────────────────────
#
# MCP clients expect the authorization server to support DCR (RFC 7591).
# Cognito doesn't, so we act as the authorization server ourselves and
# proxy authorize/token requests to Cognito, injecting the pre-registered
# MCP client_id.

# Only loopback callbacks are permitted.
#
# TODO: if a remote (non-loopback) MCP client is ever supported, add a host
# allowlist here (e.g. from CODEKB_OAUTH_ALLOWED_REDIRECT_HOSTS) rather than
# relaxing this check unconditionally.

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_redirect_allowed(redirect_uri: str) -> bool:
    """Check whether a redirect_uri targets a permitted host.

    Only loopback destinations are allowed:
    - Loopback over http or https is permitted (local MCP clients).
    - Everything else is rejected.
    """
    if not redirect_uri:
        return False
    parsed = _urlparse(redirect_uri)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()

    if not host:
        return False
    if scheme not in ("http", "https"):
        return False

    return host in _LOOPBACK_HOSTS


def _trusted_request_hosts() -> frozenset[str]:
    """Loopback hosts plus any configured via CODEKB_TRUSTED_HOSTS."""
    extra = os.environ.get("CODEKB_TRUSTED_HOSTS", "")
    hosts = {h.strip().lower() for h in re.split(r"[,\s]+", extra) if h.strip()}
    return frozenset(_LOOPBACK_HOSTS | hosts)


def _safe_base_url(request: Request, public_base: str | None) -> str:
    """Resolve the base URL from the configured public base or an allowlisted
    Host, never from an arbitrary Host header."""
    if public_base:
        return public_base
    parsed = _urlparse(f"//{(request.headers.get('host', '') or '').strip()}")
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        return "https://localhost"
    for trusted in _trusted_request_hosts():
        if trusted == hostname:
            host_part = f"[{trusted}]" if ":" in trusted else trusted
            scheme = "http" if trusted in _LOOPBACK_HOSTS else "https"
            base = f"{scheme}://{host_part}"
            if port is not None:
                base = base + ":" + str(port)
            return base
    return "https://localhost"


# Cap on the OAuth pending-redirect map so it can't grow without bound
_PENDING_REDIRECT_MAX_ENTRIES = 10_000


if is_mcp_oauth_configured():
    import httpx
    from urllib.parse import urlencode, parse_qs

    _oauth_cfg = get_cognito_config()
    _cognito_domain = _oauth_cfg["domain_url"]
    _mcp_client_id = _oauth_cfg["mcp_client_id"]
    _public_base = _oauth_cfg["public_base_url"]
    _cognito_region = _oauth_cfg["region"]
    _cognito_pool_id = _oauth_cfg["user_pool_id"]
    _idp_name = _oauth_cfg["idp_name"]

    # In-memory store: state → original redirect_uri from MCP client.
    # When the authorize proxy receives a request, it saves the MCP client's
    # redirect_uri keyed by the OAuth state parameter, rewrites redirect_uri
    # to our own /oauth2/callback, and forwards to Cognito. When Cognito
    # redirects back to /oauth2/callback, we look up the original redirect_uri
    # and relay the auth code there. This avoids the redirect_mismatch problem
    # since Cognito only ever sees our single known callback URL.
    _pending_redirects: "OrderedDict[str, str]" = OrderedDict()

    @app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
    async def oauth_protected_resource(request: Request):
        """RFC 9728 Protected Resource Metadata — points to ourselves as the auth server."""
        base_url = _safe_base_url(request, _public_base)
        return JSONResponse(
            {
                "resource": f"{base_url}/mcp/mcp",
                "authorization_servers": [base_url],
                "scopes_supported": ["openid", "email", "profile"],
                "bearer_methods_supported": ["header"],
            }
        )

    @app.get("/.well-known/oauth-authorization-server", include_in_schema=False)
    async def oauth_authorization_server(request: Request):
        """OAuth 2.0 Authorization Server Metadata (RFC 8414)."""
        base_url = _safe_base_url(request, _public_base)
        return JSONResponse(
            {
                "issuer": base_url,
                "authorization_endpoint": f"{base_url}/oauth2/authorize",
                "token_endpoint": f"{base_url}/oauth2/token",
                "registration_endpoint": f"{base_url}/oauth2/register",
                "jwks_uri": f"https://cognito-idp.{_cognito_region}.amazonaws.com/{_cognito_pool_id}/.well-known/jwks.json",
                "scopes_supported": ["openid", "email", "profile"],
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            }
        )

    @app.post("/oauth2/register", include_in_schema=False)
    async def oauth2_register(request: Request):
        """Dynamic Client Registration (RFC 7591) — returns the pre-registered MCP client_id."""
        body = await request.json()
        return JSONResponse(
            {
                "client_id": _mcp_client_id,
                "client_name": body.get("client_name", "mcp-client"),
                "redirect_uris": body.get("redirect_uris", []),
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }
        )

    @app.get("/oauth2/authorize", include_in_schema=False)
    async def oauth2_authorize(request: Request):
        """Proxy authorize to Cognito — rewrites redirect_uri to our callback relay."""
        params = dict(request.query_params)
        original_redirect = params.get("redirect_uri", "")
        state = params.get("state", "")
        logger.warning(
            f"MCP OAuth authorize — redirect_uri={original_redirect}, state={state[:16]}..."
        )

        # Validate redirect_uri before accepting it
        if not _is_redirect_allowed(original_redirect):
            logger.error(
                f"MCP OAuth authorize — rejected redirect_uri={original_redirect} "
                "(not a loopback callback)"
            )
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_redirect_uri",
                    "message": "The redirect_uri is not permitted. "
                    "Only loopback (localhost) callback addresses are accepted.",
                },
            )

        # Save the MCP client's original redirect_uri so /oauth2/callback can relay.
        # Evict oldest entries past the cap so the map can't grow without bound.
        if state and original_redirect:
            _pending_redirects[state] = original_redirect
            _pending_redirects.move_to_end(state)
            while len(_pending_redirects) > _PENDING_REDIRECT_MAX_ENTRIES:
                _pending_redirects.popitem(last=False)

        # Rewrite redirect_uri to our own callback endpoint
        base_url = _safe_base_url(request, _public_base)
        params["redirect_uri"] = f"{base_url}/oauth2/callback"
        params["client_id"] = _mcp_client_id
        if _idp_name:
            params["identity_provider"] = _idp_name

        cognito_url = f"{_cognito_domain}/oauth2/authorize"
        qs = urlencode(params)
        target = f"{cognito_url}?{qs}"
        logger.warning(
            f"MCP OAuth authorize — redirecting to Cognito (rewritten redirect_uri={params['redirect_uri']})"
        )
        return Response(status_code=302, headers={"Location": target})

    @app.get("/oauth2/callback", include_in_schema=False)
    async def oauth2_callback(request: Request):
        """Callback relay — Cognito redirects here, we relay auth code to MCP client."""
        params = dict(request.query_params)
        state = params.get("state", "")
        code = params.get("code", "")
        error = params.get("error", "")

        logger.warning(
            f"MCP OAuth callback — state={state[:16]}..., code={'present' if code else 'MISSING'}, error={error or 'none'}"
        )

        # Look up the original redirect_uri
        original_redirect = _pending_redirects.pop(state, None)
        if not original_redirect:
            logger.error(f"MCP OAuth callback — no pending redirect for state={state[:16]}...")
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_state",
                    "message": "No pending authorization for this state",
                },
            )

        # Relay all query params (code, state, error, etc.) to the MCP client's redirect_uri
        relay_qs = urlencode(params)
        separator = "&" if "?" in original_redirect else "?"
        relay_url = f"{original_redirect}{separator}{relay_qs}"
        logger.warning(f"MCP OAuth callback — relaying to {original_redirect}")
        return Response(status_code=302, headers={"Location": relay_url})

    @app.post("/oauth2/token", include_in_schema=False)
    async def oauth2_token(request: Request):
        """Proxy token exchange to Cognito, injecting the correct client_id and redirect_uri."""
        body = await request.body()
        parsed = parse_qs(body.decode(), keep_blank_values=True)
        params = {k: v[0] for k, v in parsed.items()}
        logger.warning(
            f"MCP OAuth token — grant_type={params.get('grant_type', 'unknown')}, "
            f"params={list(params.keys())}"
        )

        # Replace client_id with our MCP app client
        params["client_id"] = _mcp_client_id

        # Rewrite redirect_uri to match what we told Cognito during authorize
        # (MCP client sends its localhost redirect_uri, but Cognito expects ours)
        base_url = _public_base or "https://localhost"
        params["redirect_uri"] = f"{base_url}/oauth2/callback"

        form_body = urlencode(params)
        logger.warning(
            f"MCP OAuth token — proxying to Cognito (redirect_uri={params['redirect_uri']})"
        )

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_cognito_domain}/oauth2/token",
                content=form_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        logger.warning(f"MCP OAuth token — Cognito response: {resp.status_code}")
        if resp.status_code != 200:
            # Log only the OAuth error code, never the raw body (it can echo
            # back submitted tokens/codes on some IdPs).
            error_code = "unknown"
            try:
                error_code = resp.json().get("error", "unknown")
            except Exception:
                pass
            logger.warning(f"MCP OAuth token — Cognito error: {error_code}")
        return Response(
            status_code=resp.status_code,
            content=resp.content,
            headers={"Content-Type": resp.headers.get("content-type", "application/json")},
        )

    logger.info("MCP OAuth proxy endpoints enabled")


@app.get("/api/health", tags=["health"])
async def health() -> dict:
    """Health check (JSON)."""
    return {"service": "codekb-api", "version": "1.0.0", "status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


# Serve built React app (web/dist) if it exists.
# In local dev, use `npm run dev` in web/ instead (Vite proxies /api to here).
_web_dist = Path(__file__).resolve().parent.parent / "static" / "dist"
if _web_dist.is_dir():
    _index_html = _web_dist / "index.html"

    # SPA entry point
    @app.get("/", include_in_schema=False)
    async def spa_root():
        return FileResponse(str(_index_html))

    # Static assets (JS, CSS, images) — must be registered before the catch-all
    app.mount("/assets", StaticFiles(directory=str(_web_dist / "assets")), name="static")

    # Root-level assets from the frontend's public/ folder.
    _diagrams_dir = _web_dist / "diagrams"
    if _diagrams_dir.is_dir():
        app.mount("/diagrams", StaticFiles(directory=str(_diagrams_dir)), name="diagrams")

    # First path segment of every client-side route in frontend/src/App.tsx.
    # The catch-all serves the SPA shell only for these; any other path gets a
    # real 404 instead of index.html, so unauthenticated probes (/openapi.json,
    # /actuator, …) can't receive a 200 HTML response and be mistaken for an
    # exposed service. Keep in sync with the React Router routes.
    _SPA_ROUTE_PREFIXES = frozenset(
        {
            "",  # "/"
            "spaces",
            "create",
            "search",
            "settings",
            "git-creds",
            "agent",
            "hyperspaces",
            "docs",
            "auth",  # /auth/callback
        }
    )

    # SPA catch-all — must be registered LAST so it only matches paths no earlier
    # route or mount claimed. Serves index.html for known client-side routes so
    # React Router owns deep links and refreshes; every other path returns a real
    # 404 (JSON), never the SPA shell. GET-only: other methods keep 404ing.
    #
    # This replaces CloudFront's 404->index.html rewrite: with the container
    # serving the SPA directly, a deep link like /spaces/foo/overview would
    # otherwise 404. Unknown paths (including backend prefixes like api/, mcp,
    # oauth2/, .well-known/) fall through to a real 404 rather than being masked
    # by the SPA shell.
    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_catch_all(full_path: str):
        if full_path.split("/", 1)[0] in _SPA_ROUTE_PREFIXES:
            return FileResponse(str(_index_html))
        return JSONResponse(status_code=404, content={"detail": "Not found"})


class _RootPathStripMiddleware:
    """Strip a fixed leading path segment that a fronting proxy prepends.

    The hosted deployment fronts the container with CloudFront at the origin
    root, so the backend is served the post-origin path ("/api/…", "/mcp/…") with
    no prefix and CODEKB_ROOT_PATH is left unset — this middleware is a
    pass-through there. It exists for a deployment that instead fronts the
    container under a fixed path prefix (e.g. a customer reverse proxy forwarding
    "/codekb/api/…"): set CODEKB_ROOT_PATH to that prefix and FastAPI keeps
    serving at "/" without coupling every route (and the SPA static mounts) to it.

    Wrapped OUTSIDE the FastAPI app so it runs before routing and before the auth
    / SPA-catch-all middlewares, which then see the already-stripped path. A no-op
    when CODEKB_ROOT_PATH is unset — only requests actually carrying the prefix
    are rewritten.
    """

    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = "/" + prefix.strip("/") if prefix.strip("/") else ""

    async def __call__(self, scope, receive, send):
        if self.prefix and scope.get("type") == "http":
            path = scope.get("path", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                scope = dict(scope)
                scope["path"] = path[len(self.prefix) :] or "/"
                scope["root_path"] = scope.get("root_path", "") + self.prefix
                raw = scope.get("raw_path")
                if isinstance(raw, (bytes, bytearray)):
                    # raw_path excludes the query string; strip the same prefix.
                    pbytes = self.prefix.encode()
                    if raw.startswith(pbytes):
                        scope["raw_path"] = raw[len(pbytes) :] or b"/"
        await self.app(scope, receive, send)


# ASGI entrypoint. The FastAPI instance above stays `app` for route/middleware
# registration and tests; uvicorn serves this wrapped object. When
# CODEKB_ROOT_PATH is empty the wrapper delegates straight through.
app = _RootPathStripMiddleware(app, os.environ.get("CODEKB_ROOT_PATH", ""))


def main() -> None:
    """Entry point for `codekb-api` script."""
    import uvicorn

    # Loopback by default: this dev entry point runs with reload on the host.
    # The container image starts uvicorn itself (see the Dockerfile), where the
    # published port is the boundary. Set CODEKB_BIND_HOST to bind elsewhere.
    host = os.environ.get("CODEKB_BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("CODEKB_BIND_PORT", "8000"))
    uvicorn.run("src.api.app:app", host=host, port=port, reload=True)
