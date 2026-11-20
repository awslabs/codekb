"""Git-provider adapters behind one neutral interface.

Each provider class holds only its own facts — cloud host, self-hosted config
key, git auth-header scheme. Cloud API bases are module constants; identity and
visibility endpoints are built in ``validate_token`` / ``get_visibility``.

A provider is resolved by id (gitcreds API) or by git host (clone path).
``get_provider`` / ``get_visibility`` answer public-vs-private: the three cloud
hosts are always recognized, plus one optional self-hosted host per provider
read from its config key. ``internal`` counts as ``public`` (same-org audience).
"""

from __future__ import annotations

import base64
from typing import Any, NamedTuple, Optional, Protocol, runtime_checkable
from urllib.parse import quote, urljoin, urlparse

import httpx

from ..core.config import env_var_for, get_config
from ..core.logging_utils import ConfigurationError, get_logger

logger = get_logger(__name__)

# The env var that selects providers, named in every configuration error so the
# operator knows what to edit.
_CONFIG_VAR = "CODEKB_GIT_CREDENTIALS_PROVIDERS"

# Value that turns credentials off: public repositories only.
_NO_PROVIDERS = "none"

# Supported cloud provider ids.
GITHUB, GITLAB, BITBUCKET = "github", "gitlab", "bitbucket"

# Cloud API bases per provider (the self-hosted base comes from config instead).
GITHUB_API = "https://api.github.com"
GITLAB_API = "https://gitlab.com"
BITBUCKET_API = "https://api.bitbucket.org/2.0"

# Visibility levels recorded as "public"; everything else is "private".
_PUBLIC_LEVELS = frozenset({"public", "internal"})

# Page size for listing accessible repos. 100 is the max for all three providers
# (GitHub/GitLab per_page, Bitbucket pagelen), so one constant covers them all.
_REPOS_PER_PAGE = 100
# Max pages fetched per authz check. Pages are fetched one by one, so this caps the
# API calls per check: 50 pages × 100 = 5000 repos — more than anyone really has, so
# the full list is fetched in practice.
_MAX_PROVIDER_REPO_PAGES = 50


class _SelfHosted(NamedTuple):
    """A self-hosted instance: ``host`` matches repo URLs, ``base_url`` reaches its API."""

    host: str
    base_url: str


def _parse_self_hosted(raw: Optional[str], env_var: str) -> Optional[_SelfHosted]:
    """Parse a self-hosted URL (``None`` if unset); bad scheme/host/port raises ``ConfigurationError``."""
    raw = (raw or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ConfigurationError(
            f"{env_var} must be a full URL starting with http:// or https:// "
            f"(e.g. https://github.example.com or https://github.example.com:8443); got {raw!r}."
        )
    try:
        port = parsed.port
    except ValueError as e:
        raise ConfigurationError(
            f"{env_var} has an invalid port in {raw!r}: {e}. A port must be between 1 and 65535."
        ) from e
    host = (parsed.hostname or "").lower()
    if not host:
        raise ConfigurationError(
            f"{env_var} is missing a host in {raw!r}. "
            f"Use http(s)://host[:port], e.g. https://github.example.com:8443."
        )
    base_url = f"{scheme}://{host}" + (f":{port}" if port is not None else "")
    return _SelfHosted(host=host, base_url=base_url)


def _self_hosted_of(provider: "GitProvider") -> Optional[_SelfHosted]:
    """Parsed self-hosted instance for a provider, or ``None`` when unconfigured."""
    key = provider._SELF_HOSTED_KEY  # type: ignore[attr-defined]
    return _parse_self_hosted(get_config(key, ""), env_var_for(key) or key)


def _bare_host(value: Optional[str]) -> str:
    """Hostname from a bare host or a full URL, lowercased (``""`` if empty)."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    return urlparse(v if "://" in v else "//" + v).hostname or v


def _same_origin(base: str, target: str) -> bool:
    """True if ``target`` (possibly relative to ``base``) is the same scheme, host
    and port as ``base`` — used to follow a redirect without ever sending the
    token to a different origin."""
    a, b = urlparse(base), urlparse(urljoin(base, target))
    return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port)


def _instance_root_for(provider: "GitProvider", host: Optional[str]) -> Optional[str]:
    """The base URL to call for this ``host`` when saving a token.

    Returns ``None`` for the cloud host (the caller then uses the cloud API
    constant), or the configured self-hosted base URL. Any other host raises
    ``ProviderError`` so a token is never sent somewhere unexpected.
    """
    h = (host or "").strip().lower()
    if not h or h == provider._HOST:  # type: ignore[attr-defined]
        return None
    sh = _self_hosted_of(provider)
    if sh and sh.host == h:
        return sh.base_url
    key = provider._SELF_HOSTED_KEY  # type: ignore[attr-defined]
    raise ProviderError(f"{h} is not configured for {provider.id}; set {env_var_for(key) or key}.")


class ProviderError(Exception):
    """A provider-side failure (network, unexpected status, unsupported provider)."""


class TokenInvalid(ProviderError):
    """The supplied token was rejected by the provider (401/403)."""


class VisibilityError(ProviderError):
    """Visibility can't be checked for a client-fixable reason — an unsupported
    host, a non-repo URL, or a rejected saved token. The create path maps it to
    HTTP 400. (A provider non-answer raises plain ``ProviderError`` -> 503.)
    """


class ProviderListingUnsupported(ProviderError):
    """This host can't list permissions (e.g. Bitbucket Server/DC). Permanent,
    so the authz gate denies rather than 503s."""


def _http_timeout() -> float:
    """Seconds per provider call, read per call so later config applies."""
    return float(get_config("gitcreds.http_timeout", 10.0))


def _basic_auth_header(username: str, token: str) -> str:
    """Build an HTTP Basic ``Authorization`` header value.

    Git-over-HTTPS auth for every supported provider is Basic with a
    provider-specific pseudo-user and the token as the password.
    """
    raw = f"{username}:{token}".encode()
    return "Authorization: Basic " + base64.b64encode(raw).decode()


def _identity(body: Any, provider: str, *fields: str) -> str:
    """Read the username from ``body``, trying each name in ``fields`` in order.

    Raises ``ProviderError`` if none are found, listing only the keys (not values,
    which are user data).
    """
    if not isinstance(body, dict):
        raise ProviderError(
            f"{provider} returned {type(body).__name__} where an object was expected"
        )
    for field in fields:
        value = body.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    wanted = " or ".join(repr(f) for f in fields)
    raise ProviderError(f"{provider} response carries no {wanted}; keys received: {sorted(body)}")


def _list_get(url: str, headers: dict, params: dict, label: str) -> httpx.Response:
    """One GET on the permission-listing path. 401 and a non-rate-limit 403 are
    TokenInvalid (reconnect); a rate-limit 403, 429, 5xx or network error is a
    transient ProviderError."""
    try:
        r = httpx.get(url, headers=headers, params=params, timeout=_http_timeout())
    except httpx.HTTPError as e:
        raise ProviderError(f"Could not reach {label}: {e}") from e
    if r.status_code == 401:
        raise TokenInvalid("Invalid or expired token")
    if r.status_code == 429:
        raise ProviderError(f"{label} rate limit exceeded; please retry later")
    if r.status_code == 403:
        # GitHub returns 403 for primary rate limits (ratelimit headers set) — that's
        # transient. Any other 403 is a permanent rejection (SSO/SAML not authorized,
        # token missing a scope), so surface it as a reconnect, not a stuck 503.
        if r.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in r.headers:
            raise ProviderError(f"{label} rate limit exceeded; please retry later")
        raise TokenInvalid(f"{label} rejected the token (HTTP 403)")
    if r.status_code >= 400:
        raise ProviderError(f"{label} returned {r.status_code}")
    return r


@runtime_checkable
class GitProvider(Protocol):
    """The neutral contract every provider adapter implements."""

    id: str

    def match(self, host: str) -> bool:
        """True if this provider owns ``host`` (e.g. github.com)."""
        ...

    def validate_token(self, token: str, host: Optional[str] = None) -> dict:
        """Check the token is valid and return ``{"username": ...}``.

        Raises ``TokenInvalid`` if the provider rejects the token,
        ``ProviderError`` for any other failure.
        """
        ...

    def git_auth_header(self, token: str) -> str:
        """The ``http.extraHeader`` value injected into git clone/fetch."""
        ...

    def visibility_request(
        self, segments: list[str], is_cloud: bool, base: str, token: Optional[str]
    ) -> Optional[tuple[str, dict]]:
        """Build the ``(url, headers)`` to look up a repo, or ``None`` if
        ``segments`` isn't a valid repo path. ``base`` is the self-hosted URL,
        used only when not ``is_cloud``."""
        ...

    def reads_public(self, body: dict, is_cloud: bool) -> bool:
        """Return ``True`` if a 200 response body means the repo is public."""
        ...

    def list_accessible_repos(self, token: str, host: Optional[str] = None) -> dict:
        """``repo_key`` → native permission for every repo the token can reach.
        Only 401 is ``TokenInvalid``; other failures are ``ProviderError``."""
        ...


class GitHubProvider:
    """github.com. Fully supported."""

    id = GITHUB
    _HOST = "github.com"
    _SELF_HOSTED_KEY = "gitcreds.github_custom_host"

    def match(self, host: str) -> bool:
        host = host.strip().lower()
        if host == self._HOST:
            return True
        configured = _self_hosted_of(self)
        return configured is not None and host == configured.host

    def validate_token(self, token: str, host: Optional[str] = None) -> dict:
        root = _instance_root_for(self, host)
        api = f"{root}/api/v3" if root else GITHUB_API  # Enterprise REST lives at /api/v3
        try:
            r = httpx.get(
                f"{api}/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=_http_timeout(),
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"Could not reach GitHub: {e}") from e
        if r.status_code in (401, 403):
            if self._is_rate_limited(r.status_code, r.headers):
                raise ProviderError("GitHub rate limit exceeded; please retry later")
            raise TokenInvalid("Invalid or expired token")
        if r.status_code >= 400:
            raise ProviderError(f"GitHub returned {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise ProviderError("GitHub returned a non-JSON response") from e
        return {"username": _identity(body, "GitHub", "login")}

    def git_auth_header(self, token: str) -> str:
        # GitHub accepts the token as the password with any non-empty username;
        # x-access-token is the documented convention for token auth.
        return _basic_auth_header("x-access-token", token)

    def visibility_request(self, segments, is_cloud, base, token):
        if len(segments) != 2:  # owner/repo
            return None
        api = GITHUB_API if is_cloud else f"{base}/api/v3"  # Enterprise REST at /api/v3
        url = f"{api}/repos/{quote(segments[0], safe='')}/{quote(segments[1], safe='')}"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return url, headers

    def reads_public(self, body, is_cloud):
        value = body.get("visibility")  # str, or legacy bool `private`
        if isinstance(value, str):
            return value.strip().lower() in _PUBLIC_LEVELS
        return body.get("private") is False

    def _is_rate_limited(self, status, headers) -> bool:
        # GitHub reuses 403 for rate limits; the headers tell it apart from a bad token.
        return status == 403 and (
            headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers
        )

    def list_accessible_repos(self, token: str, host: Optional[str] = None) -> dict[str, Any]:
        root = _instance_root_for(self, host)
        api_base = f"{root}/api/v3" if root else GITHUB_API  # Enterprise REST at /api/v3
        key_host = (host or self._HOST).lower()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        repos: dict[str, Any] = {}
        page = 1
        for _ in range(_MAX_PROVIDER_REPO_PAGES):
            r = _list_get(
                f"{api_base}/user/repos",
                headers,
                {
                    "per_page": _REPOS_PER_PAGE,
                    "affiliation": "owner,collaborator,organization_member",
                    "page": page,
                },
                "GitHub",
            )
            items = r.json()
            if not isinstance(items, list) or not items:
                break
            for repo in items:
                full = (repo or {}).get("full_name")
                if full:
                    repos[f"{key_host}/{full}".lower()] = (repo or {}).get("permissions")
            if len(items) < _REPOS_PER_PAGE:
                break
            page += 1
        return repos


class GitLabProvider:
    """gitlab.com and self-hosted GitLab.

    Enabled via ``gitcreds.providers`` once verified in the target
    environment; not on by default.
    """

    id = GITLAB
    _HOST = "gitlab.com"
    _SELF_HOSTED_KEY = "gitcreds.gitlab_custom_host"
    # Reporter: lowest level that can read code. The listing filters on this, so
    # every returned project is readable at least at this tier.
    _MIN_ACCESS_LEVEL = 15

    def match(self, host: str) -> bool:
        host = host.strip().lower()
        if host == self._HOST:
            return True
        configured = _self_hosted_of(self)
        return configured is not None and host == configured.host

    def validate_token(self, token: str, host: Optional[str] = None) -> dict:
        base = _instance_root_for(self, host) or GITLAB_API
        try:
            r = httpx.get(
                f"{base}/api/v4/user",
                headers={"PRIVATE-TOKEN": token},
                timeout=_http_timeout(),
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"Could not reach GitLab: {e}") from e
        if r.status_code in (401, 403):
            raise TokenInvalid("Invalid or expired token")
        if r.status_code >= 400:
            raise ProviderError(f"GitLab returned {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise ProviderError("GitLab returned a non-JSON response") from e
        return {"username": _identity(body, "GitLab", "username")}

    def git_auth_header(self, token: str) -> str:
        return _basic_auth_header("oauth2", token)

    def visibility_request(self, segments, is_cloud, base, token):
        if len(segments) < 2:  # group[/subgroup...]/project
            return None
        api = GITLAB_API if is_cloud else base
        url = f"{api}/api/v4/projects/{quote('/'.join(segments), safe='')}"
        return url, ({"PRIVATE-TOKEN": token} if token else {})

    def reads_public(self, body, is_cloud):
        return str(body.get("visibility")).strip().lower() in _PUBLIC_LEVELS

    def list_accessible_repos(self, token: str, host: Optional[str] = None) -> dict[str, Any]:
        api_base = _instance_root_for(self, host) or GITLAB_API
        key_host = (host or self._HOST).lower()
        headers = {"PRIVATE-TOKEN": token}
        repos: dict[str, Any] = {}
        page = 1
        for _ in range(_MAX_PROVIDER_REPO_PAGES):
            r = _list_get(
                f"{api_base}/api/v4/projects",
                headers,
                {
                    "membership": "true",
                    "min_access_level": self._MIN_ACCESS_LEVEL,
                    "per_page": _REPOS_PER_PAGE,
                    "page": page,
                },
                "GitLab",
            )
            items = r.json()
            if not isinstance(items, list) or not items:
                break
            for proj in items:
                path = (proj or {}).get("path_with_namespace")
                if not path:
                    continue
                perms = (proj or {}).get("permissions") or {}
                project_access = (perms.get("project_access") or {}).get("access_level") or 0
                group_access = (perms.get("group_access") or {}).get("access_level") or 0
                level = max(int(project_access), int(group_access))
                # Both fields are null when access is inherited (ancestor group or
                # project sharing); the membership filter still guarantees the floor,
                # so use it rather than reading 0.
                repos[f"{key_host}/{path}".lower()] = level or self._MIN_ACCESS_LEVEL
            if len(items) < _REPOS_PER_PAGE:
                break
            page += 1
        return repos


class BitbucketProvider:
    """Bitbucket Cloud and self-hosted Server/Data Center.

    Targets access tokens over HTTPS (``x-token-auth`` pseudo-user). Cloud
    identity comes from ``/2.0/user``; Server/DC has none, so the user is read
    from the ``X-AUSERNAME`` header. Enabled via ``gitcreds.providers``; off by
    default.
    """

    id = BITBUCKET
    _HOST = "bitbucket.org"
    _SELF_HOSTED_KEY = "gitcreds.bitbucket_custom_host"

    def match(self, host: str) -> bool:
        host = host.strip().lower()
        if host == self._HOST:
            return True
        configured = _self_hosted_of(self)
        return configured is not None and host == configured.host

    def validate_token(self, token: str, host: Optional[str] = None) -> dict:
        root = _instance_root_for(self, host)
        if root:
            return self._validate_server(token, root)
        try:
            r = httpx.get(
                f"{BITBUCKET_API}/user",
                headers={"Authorization": f"Bearer {token}"},
                timeout=_http_timeout(),
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"Could not reach Bitbucket: {e}") from e
        if r.status_code in (401, 403):
            raise TokenInvalid("Invalid or expired token")
        if r.status_code >= 400:
            raise ProviderError(f"Bitbucket returned {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise ProviderError("Bitbucket returned a non-JSON response") from e
        # Bitbucket's identity field varies by account; prefer username, fall
        # back to nickname.
        return {"username": _identity(body, "Bitbucket", "username", "nickname")}

    def _validate_server(self, token: str, root: str) -> dict:
        """Validate against Bitbucket Server/DC.

        No official current-user endpoint, so the token is checked via the
        caller's inbox count (auth-only, 401 on a bad token); the username comes
        from the non-official ``X-AUSERNAME`` header if present, else blank for the
        save route to fill.
        """
        try:
            r = httpx.get(
                f"{root}/rest/api/1.0/inbox/pull-requests/count",
                headers={"Authorization": f"Bearer {token}"},
                timeout=_http_timeout(),
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"Could not reach Bitbucket: {e}") from e
        if r.status_code in (401, 403):
            raise TokenInvalid("Invalid or expired token")
        if r.status_code >= 400:
            raise ProviderError(f"Bitbucket returned {r.status_code}")
        username = (r.headers.get("X-AUSERNAME") or "").strip()
        if username.lower() == "anonymous":
            username = ""
        return {"username": username}

    def git_auth_header(self, token: str) -> str:
        return _basic_auth_header("x-token-auth", token)

    def visibility_request(self, segments, is_cloud, base, token):
        # Server/DC clone paths carry an "/scm" context prefix (/scm/PROJECT/repo
        # or /scm/~user/repo); drop it to get project/repo.
        if not is_cloud and segments and segments[0] == "scm":
            segments = segments[1:]
        if len(segments) != 2:
            return None
        if is_cloud:
            url = f"{BITBUCKET_API}/repositories/{quote(segments[0], safe='')}/{quote(segments[1], safe='')}"
        else:  # Server / Data Center: projectKey/repoSlug under REST 1.0
            url = (
                f"{base}/rest/api/1.0/projects/"
                f"{quote(segments[0], safe='')}/repos/{quote(segments[1], safe='')}"
            )
        return url, ({"Authorization": f"Bearer {token}"} if token else {})

    def reads_public(self, body, is_cloud):
        if is_cloud:
            return body.get("is_private") is False
        return body.get("public") is True  # Server/DC: anonymous read allowed

    def list_accessible_repos(self, token: str, host: Optional[str] = None) -> dict[str, Any]:
        if _instance_root_for(self, host):  # Server/DC listing not supported
            raise ProviderListingUnsupported(
                "Bitbucket Server/Data Center permission listing is not supported."
            )
        key_host = (host or self._HOST).lower()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        repos: dict[str, Any] = {}
        url: Optional[str] = f"{BITBUCKET_API}/user/permissions/repositories"
        params: dict = {"pagelen": _REPOS_PER_PAGE}
        for _ in range(_MAX_PROVIDER_REPO_PAGES):
            if url is None:
                break
            r = _list_get(url, headers, params, "Bitbucket")
            body = r.json() if callable(getattr(r, "json", None)) else {}
            for value in (body or {}).get("values", []) or []:
                full = ((value or {}).get("repository") or {}).get("full_name")
                if full:
                    repos[f"{key_host}/{full}".lower()] = (value or {}).get("permission")
            url = (body or {}).get("next")  # Cloud paginates with an absolute next URL
            params = {}
        return repos


# All implemented adapters by id; which are *active* is decided by config
# (``gitcreds.providers``), so a provider can exist in code before it is enabled.
_ALL_GIT_PROVIDERS: dict[str, GitProvider] = {
    p.id: p for p in (GitHubProvider(), GitLabProvider(), BitbucketProvider())
}


def enabled_provider_ids() -> list[str]:
    """Enabled provider ids in config order, from ``gitcreds.providers``
    (``CODEKB_GIT_CREDENTIALS_PROVIDERS``), comma/space-separated. ``NONE`` yields
    an empty list (public repos only).

    Raises ``ConfigurationError`` on an empty value, ``NONE`` mixed with a
    provider, or an unknown provider; run at startup by ``validate_provider_config``.
    """
    raw = get_config("gitcreds.providers", "github") or ""
    tokens = [t.lower() for t in raw.replace(",", " ").split()]
    supported = ", ".join(sorted(_ALL_GIT_PROVIDERS))
    choices = f"a combination of {supported}, or NONE for public repositories only"

    if not tokens:
        raise ConfigurationError(
            f"{_CONFIG_VAR} is empty. Set it to {choices} "
            f"in the environment (.env / ECS task definition) and restart."
        )
    if _NO_PROVIDERS in tokens:
        if len(tokens) > 1:
            named = ", ".join(repr(t) for t in tokens if t != _NO_PROVIDERS)
            raise ConfigurationError(
                f"{_CONFIG_VAR} combines NONE with {named}. Set it to {choices} "
                f"in the environment (.env / ECS task definition) and restart."
            )
        return []
    unknown = [t for t in tokens if t not in _ALL_GIT_PROVIDERS]
    if unknown:
        raise ConfigurationError(
            f"{_CONFIG_VAR} names unknown provider(s) {', '.join(repr(u) for u in unknown)}. "
            f"Set it to {choices} in the environment (.env / ECS task definition) and restart."
        )

    ids: list[str] = []
    for token in tokens:
        if token not in ids:
            ids.append(token)
    return ids


def validate_provider_config() -> None:
    """Fail startup on an unusable provider list or a malformed self-hosted host
    URL, so a typo stops the service instead of surfacing per request.
    """
    ids = enabled_provider_ids()
    for provider in _ALL_GIT_PROVIDERS.values():
        _self_hosted_of(provider)  # raises on a malformed self-hosted host URL
    logger.info(
        "Git credential providers enabled: %s",
        ", ".join(ids) if ids else "none (public repositories only)",
    )


def provider_by_id(provider_id: str) -> GitProvider:
    """Resolve an implemented provider by id, or raise ``ProviderError``."""
    pid = (provider_id or "").strip().lower()
    if pid not in _ALL_GIT_PROVIDERS:
        raise ProviderError(f"Unknown provider: {provider_id!r}")
    return _ALL_GIT_PROVIDERS[pid]


def provider_for_host(host: str) -> Optional[GitProvider]:
    """Resolve an enabled provider that owns ``host``, or ``None``."""
    for pid in enabled_provider_ids():
        provider = _ALL_GIT_PROVIDERS[pid]
        if provider.match(host):
            return provider
    return None


def configured_self_hosted_host(provider_id: str) -> Optional[str]:
    """The self-hosted host configured for a provider (its ``*_CUSTOM_HOST``), or
    ``None`` for cloud-only. Lets the save path take the host from operator config
    instead of asking the user for it."""
    provider = _ALL_GIT_PROVIDERS.get((provider_id or "").strip().lower())
    if provider is None:
        return None
    sh = _self_hosted_of(provider)
    return sh.host if sh else None


def get_provider(git_url: str) -> Optional[str]:
    """Provider id for an https/ssh ``git_url`` (cloud or configured self-hosted
    host), else ``None``. Independent of ``gitcreds.providers`` — that governs
    saved credentials, and visibility works unauthenticated for public repos.
    """
    parsed = urlparse((git_url or "").strip())
    if parsed.scheme.lower() not in ("https", "ssh"):
        return None
    host = (parsed.hostname or "").lower()
    for pid, provider in _ALL_GIT_PROVIDERS.items():
        if provider.match(host):
            return pid
    return None


def saved_token_matches_repo(provider_id: str, saved_host: Optional[str], git_url: str) -> bool:
    """Whether a saved token should be sent when checking ``git_url``.

    The token belongs to the custom host it was saved under, or the provider's
    cloud host if none was saved. Return ``True`` only when the repo is on that
    same host, so a token is never sent to a host it was not issued for. Both
    sides are reduced to a bare hostname before comparing.
    """
    provider = _ALL_GIT_PROVIDERS.get(provider_id)
    if provider is None:
        return False
    repo_host = _bare_host(urlparse((git_url or "").strip()).hostname)
    token_host = _bare_host(saved_host) or provider._HOST  # type: ignore[attr-defined]
    return bool(repo_host) and repo_host == token_host


def get_visibility(git_url: str, token: Optional[str] = None) -> str:
    """Return ``"public"`` or ``"private"`` for a repo (cloud or self-hosted).

    Returns a verdict only when sure: 200 (``internal`` is public) or 404 ->
    ``"private"``. Otherwise it fails instead of guessing: an unsupported host,
    non-repo URL, or rejected token raises ``VisibilityError`` (client can fix);
    a provider non-answer (network error, 5xx, rate limit) raises
    ``ProviderError``. Follows one same-origin redirect (e.g. a renamed repo),
    never cross-origin, so the token is never replayed elsewhere.
    """
    pid = get_provider(git_url)
    if pid is None:
        raise VisibilityError("This repository's host is not supported for analysis.")
    provider = _ALL_GIT_PROVIDERS[pid]

    parsed = urlparse((git_url or "").strip())
    host = (parsed.hostname or "").lower()
    is_cloud = host == provider._HOST  # type: ignore[attr-defined]
    # Self-hosted: use the operator-configured scheme/host/port.
    self_hosted = None if is_cloud else _self_hosted_of(provider)
    sh_base = self_hosted.base_url if self_hosted else f"https://{host}"

    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")].rstrip("/")
    segments = [s for s in path.split("/") if s]

    request = provider.visibility_request(segments, is_cloud, sh_base, token)
    if request is None:
        raise VisibilityError("This does not look like a repository URL.")
    url, headers = request

    try:
        # follow_redirects=False so a token can't be replayed to another origin.
        r = httpx.get(url, headers=headers, timeout=_http_timeout(), follow_redirects=False)
        # A renamed/transferred repo answers 301/308 to a new location on the same
        # host; follow one same-origin hop (keeps the no-cross-origin-token rule).
        if r.status_code in (301, 302, 307, 308):
            location = r.headers.get("location", "")
            if location and _same_origin(url, location):
                r = httpx.get(
                    urljoin(url, location),
                    headers=headers,
                    timeout=_http_timeout(),
                    follow_redirects=False,
                )
    except httpx.HTTPError as e:
        raise ProviderError(f"Could not reach {pid} to check repo visibility: {e}") from e
    status = r.status_code

    if status == 200:
        try:
            body = r.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ProviderError(f"{pid} returned an unreadable visibility response")
        return "public" if provider.reads_public(body, is_cloud) else "private"

    # A private repo answers 404 on every provider when the caller can't see it,
    # so 404 is a definite "not publicly visible".
    if status == 404:
        return "private"

    # GitHub reuses 403 for rate limits (ratelimit headers) — that's a non-answer,
    # not a bad token.
    rate_limited = (
        pid == GITHUB
        and status == 403
        and (r.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in r.headers)
    )
    # With a saved token, a non-rate-limit 401/403 means the token was rejected.
    if status in (401, 403) and token and not rate_limited:
        raise VisibilityError(
            f"Your saved {pid} token was rejected. Update it on the Git credentials page."
        )
    # Anything else — anonymous 401/403, a rate limit, 5xx — is no usable answer.
    # Fail the create rather than storing a guessed visibility.
    raise ProviderError(f"{pid} could not confirm repo visibility (HTTP {status})")


def list_accessible_repos(
    provider_id: str, token: str, host: Optional[str] = None
) -> dict[str, Any]:
    """``repo_key`` → native permission for every repo the token can reach.

    Thin dispatcher to the provider adapter. ``repo_key`` is ``host/full_name``
    lowercased (matches authz.repo_key). 401 → TokenInvalid; transient failure →
    ProviderError; can't-list host (Bitbucket Server/DC) → ProviderListingUnsupported.
    """
    return provider_by_id(provider_id).list_accessible_repos(token, host)
