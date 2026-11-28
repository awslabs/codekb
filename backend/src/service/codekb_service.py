"""Stateless service layer wrapping all CodeKB operations.

Used by:
- FastAPI REST routes (uvicorn on ECS Fargate behind CloudFront, or local uvicorn)
- MCP server tools (same container, served at /mcp, or local stdio)
- CLI commands
"""

from __future__ import annotations

import ipaddress
import json
import ntpath
import os
import posixpath
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tarfile
import threading
import time
import zipfile
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import unquote, urlparse

from botocore.exceptions import ClientError

from ..analysis.orchestrator import AnalysisOrchestrator, AnalysisContext
from ..analysis.git_ops import redact_git_url, run_git
from ..persistence.knowledge_base_manager import KnowledgeBaseManager
from ..core.config import ConfigError, get_config, require_config
from ..core.file_utils import LEGACY_DEFAULT_EXCLUDE_PATTERNS, discard_directory
from ..core.logging_utils import ConfigurationError, get_logger
from ..core.token_usage import operation_usage
from ..gitcreds.providers import ProviderError, VisibilityError, get_provider, get_visibility
from ..gitcreds.resolver import resolve_repo_credential
from ..authz.classify import effective_visibility
from ..authz.repo_key import repo_key
from .errors import (
    HyperspaceNotFoundError,
    OverviewInProgressError,
    ServiceError,
    SourceNotAvailableError,
    SpaceBusyError,
    SpaceNotFoundError,
)
from .pipeline_pool import PipelineSlot, pipeline_pool

# The local source cache — its root path, its scratch-directory conventions, and
# the startup sweep — lives in source_provider, which owns that cache end to end.
# These names are re-exported from this module because refresh_orchestrator and
# api/app already import them from here.
from .source_provider import (
    SOURCE_CACHE_ROOT,
    WORK_DIR_ANALYSIS,
    WORK_DIR_REFRESH,
    SourceCodeProvider,
    sweep_source_cache_scratch,
)

logger = get_logger(__name__)


# Steps that belong exclusively to the space-refresh pipeline. Used to route
# status updates into ``refresh_pipeline_runs`` so the original analysis
# ``pipeline_steps`` remain visible after a refresh. "clone" is intentionally
# excluded because it is shared with the analysis pipeline; refresh clone
# updates are routed by their REFRESHING status instead.
# Chat, all-spaces scope and hyperspaces use these; a failed refresh keeps the last indexed snapshot.
QUERYABLE_STATUSES = frozenset({"READY", "REFRESH_FAILED"})

_REFRESH_ONLY_STEPS = frozenset(
    {
        "remediate_depth",
        "refresh_diff",
        "parse_changed",
        "merge",
        "rebuild_call_graph",
        "summarize_changed",
        "stage_kb",
        "vector_deletes",
        "commit_kb",
        "invalidate_caches",
        "mark_overviews_stale",
    }
)


# URL transports accepted for standard Git remotes.
ALLOWED_GIT_URL_SCHEMES = frozenset({"https", "ssh"})

# What ipaddress.ip_address() returns — the concrete union, so the IP-category
# properties (is_private, is_loopback, …) are visible to the type checker.
_IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# NAT64 Well-Known Prefix (RFC 6052 §2.1).
_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")

# Env var holding the REQUIRED comma/whitespace-separated Git host allowlist.
_ALLOWED_GIT_HOSTS_ENV = "CODEKB_ALLOWED_GIT_HOSTS"

# Hardened environment passed to every Git subprocess.
_GIT_SUBPROCESS_ENV: dict[str, str] = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "GIT_ALLOW_PROTOCOL": "https:ssh",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "http.followRedirects",
    "GIT_CONFIG_VALUE_0": "false",
}


def _allowed_git_hosts() -> frozenset[str]:
    """Parse the required host allowlist from the environment.

    Empty when unset. validate_git_url treats an empty allowlist as fail-closed
    (reject every host), not as "no restriction". Hosts are lowercased; commas
    and whitespace both act as separators.
    """
    raw = os.environ.get(_ALLOWED_GIT_HOSTS_ENV, "")
    tokens = raw.replace(",", " ").split()
    return frozenset(t.strip().lower() for t in tokens if t.strip())


def allowed_git_hosts() -> list[str]:
    """Public accessor: the effective Git host allowlist as a sorted list.

    Empty when fail-closed (no host permitted, so repository-by-URL analysis is
    disabled). Mirrors exactly what validate_git_url enforces, so callers such as
    the read-only Settings view show the same allowlist the validator applies.
    """
    return sorted(_allowed_git_hosts())


def zip_upload_enabled() -> bool:
    """Whether the zip-upload source is enabled for this deployment."""
    return bool(require_config("features.zip_upload_enabled"))


def git_url_credentials_allowed() -> bool:
    """Whether a git URL may embed credentials (https://user:token@host/...)."""
    return bool(require_config("features.allow_credentials_in_git_url"))


def overview_generation_enabled() -> bool:
    """Whether on-demand overview generation (Space + Hyperspace) is enabled."""
    return bool(require_config("features.overview_generation_enabled"))


def git_private_addresses_allowed() -> bool:
    """Whether a Git host may resolve to a private or loopback address.

    Off by default. The post-resolution SSRF guard (see
    ``_assert_git_host_resolves_safely``) rejects any host that resolves onto a
    private (RFC1918 / unique-local) or loopback address. A deployment whose only
    Git server is self-hosted on a private network AND reached by hostname sets
    ``CODEKB_ALLOW_PRIVATE_GIT_ADDRESSES=true`` to permit it. Link-local /
    metadata, multicast, reserved and unspecified addresses are ALWAYS rejected,
    even with this flag on.
    """
    return bool(require_config("features.allow_private_git_addresses"))


def _host_in_allowlist(hostname: str, allowlist: frozenset[str]) -> bool:
    """True if hostname equals, or is a dotted subdomain of, an allowlisted host."""
    host = hostname.lower()
    return any(host == allowed or host.endswith("." + allowed) for allowed in allowlist)


def _as_ip_address(host: str) -> _IpAddress | None:
    """Parse ``host`` as an IP literal, or return None if it is a name.

    The host has already had any ``[...]`` IPv6 brackets stripped by the caller.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _blocked_address_category(ip: _IpAddress, *, allow_private: bool) -> str | None:
    """Name the disallowed category ``ip`` falls into, or None if it is allowed.

    Always blocked (never a legitimate remote Git host, and the opt-in does NOT
    override these):
      - link-local — 169.254.0.0/16 and IPv6 fe80::/10; the cloud instance
        metadata endpoint 169.254.169.254 lives here, so this is the primary
        SSRF target,
      - multicast, reserved, and the unspecified address (0.0.0.0 / ::).

    Blocked unless ``allow_private`` is set:
      - loopback (127.0.0.0/8, ::1),
      - private — RFC1918 (10/8, 172.16/12, 192.168/16) and IPv6 unique-local.

    IPv6 wrappers around an IPv4 target (IPv4-mapped, 6to4, Teredo, and the
    NAT64 Well-Known Prefix 64:ff9b::/96) are unwrapped first so an internal v4
    address cannot be smuggled through them. NAT64 network-specific prefixes
    (RFC 6052 §2.2 / RFC 8215) carry no marker ``ipaddress`` can recognise and
    are not unwrapped.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    sixtofour = getattr(ip, "sixtofour", None)
    if sixtofour is not None:
        ip = sixtofour
    teredo = getattr(ip, "teredo", None)
    if teredo is not None:
        # teredo is (server, client); the client is the tunnelled destination.
        ip = teredo[1]
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64_WELL_KNOWN_PREFIX:
        # The embedded IPv4 target is the low 32 bits of the NAT64 address.
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)

    # Order matters: Python's ``ipaddress`` reports some addresses in several
    # categories at once (``::1`` is both loopback and reserved; ``240.0.0.0/4``
    # is both private and reserved). Classify the specific, intended meaning
    # first — loopback/unspecified before the catch-all reserved, and reserved
    # before the very broad ``is_private`` — so the opt-in gates exactly the
    # loopback and RFC1918/ULA ranges and nothing else.
    if ip.is_link_local:
        return "link-local/metadata"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_loopback:
        return None if allow_private else "loopback"
    if ip.is_reserved:
        return "reserved"
    if ip.is_private:
        return None if allow_private else "private"
    return None


def _resolve_host_addresses(hostname: str) -> list[_IpAddress]:
    """Resolve ``hostname`` to the full set of A/AAAA addresses git would use.

    Fail-closed: a name that cannot be resolved raises ServiceError rather than
    being waved through. Returns de-duplicated, zone-stripped addresses.
    """
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ServiceError(f"git_url host '{hostname}' could not be resolved ({exc}).")

    addresses: list[_IpAddress] = []
    seen: set[str] = set()
    for _family, _type, _proto, _canon, sockaddr in infos:
        raw = str(sockaddr[0]).split("%", 1)[0]  # drop any IPv6 %zone suffix
        if raw in seen:
            continue
        seen.add(raw)
        parsed = _as_ip_address(raw)
        if parsed is not None:
            addresses.append(parsed)

    if not addresses:
        raise ServiceError(f"git_url host '{hostname}' could not be resolved.")
    return addresses


def _assert_git_host_resolves_safely(hostname: str, allowlist: frozenset[str]) -> None:
    """Post-resolution SSRF guard: reject a host that lands on an internal address.

    The host allowlist matches on name only, so an allowlisted name — or a
    dotted subdomain of one, or any name whose DNS answer an attacker can
    influence — can still resolve to a private, loopback or cloud-metadata
    address. This resolves the host and rejects those before git is invoked.

    An IP literal that the operator placed on the allowlist by its exact value
    names one unambiguous target with no DNS indirection to rebind, so it is
    trusted for the private/loopback gate — that is how a self-hosted Git server
    on a private VPC address is allowed. It is NOT a skip of the always-blocked
    gates: link-local/metadata, multicast, reserved and unspecified addresses
    stay rejected even when the literal is on the allowlist, matching the
    documented guarantee. Every other host (including an IP literal reached only
    via a subdomain-style allowlist match) is resolved and classified.

    Raises ServiceError if any resolved address is in a disallowed range.
    """
    allow_private = git_private_addresses_allowed()

    literal = _as_ip_address(hostname)
    literal_on_allowlist = literal is not None and hostname.lower() in allowlist

    candidates = [literal] if literal is not None else _resolve_host_addresses(hostname)
    for ip in candidates:
        # An exact-match IP literal on the allowlist trusts the private/loopback
        # opt-in only; the always-blocked categories (link-local/metadata,
        # multicast, reserved, unspecified) still apply so a literal like
        # 169.254.169.254 can never be waved through.
        category = _blocked_address_category(
            ip, allow_private=allow_private or literal_on_allowlist
        )
        if category is None:
            continue
        hint = (
            " Set CODEKB_ALLOW_PRIVATE_GIT_ADDRESSES=true only if this host is a "
            "trusted Git server on your private network."
            if category in ("private", "loopback")
            else ""
        )
        raise ServiceError(
            f"git_url host '{hostname}' resolves to a disallowed {category} "
            f"address ({ip}); refusing to clone from it.{hint}"
        )


def validate_git_url(git_url: str) -> str:
    """Validate a git URL against the SSRF controls, returning the trimmed URL.

    Layered controls, in order:
    1. Scheme allowlist (https/ssh) — blocks ext:: RCE, file:// reads, plaintext.
    2. Host allowlist (CODEKB_ALLOWED_GIT_HOSTS) — FAIL-CLOSED and REQUIRED. With
       no allowlist configured, every host is rejected. Otherwise the host must
       match an entry exactly or be a dotted subdomain.
    3. Post-resolution address guard — the host is resolved and the URL is
       rejected if it lands on a private, loopback or link-local/metadata
       address. The name-only allowlist cannot see this: an allowlisted name (or
       a subdomain of one) can still point at an internal address via DNS. See
       _assert_git_host_resolves_safely and CODEKB_ALLOW_PRIVATE_GIT_ADDRESSES.
    4. Embedded credentials (userinfo) — rejected unless
       CODEKB_ALLOW_CREDENTIALS_IN_GIT_URL is enabled.

    Raises ServiceError on any violation.
    """
    candidate = (git_url or "").strip()
    if not candidate:
        raise ServiceError("git_url is required.")

    parts = urlparse(candidate)
    scheme = parts.scheme.lower()

    if not scheme:
        raise ServiceError("git_url must start with https:// or ssh://. ")

    if scheme not in ALLOWED_GIT_URL_SCHEMES:
        raise ServiceError(
            f"git_url scheme '{scheme}' is not supported. "
            f"Use {' or '.join(sorted(ALLOWED_GIT_URL_SCHEMES))}."
        )

    if not parts.netloc:
        raise ServiceError("git_url must include a host.")

    hostname = (parts.hostname or "").strip("[]")
    if not hostname:
        raise ServiceError("git_url must include a host.")

    # Host allowlist — FAIL-CLOSED. An explicit allowlist is required; with none
    # configured no host is permitted. This is the authoritative SSRF control.
    allowlist = _allowed_git_hosts()
    if not allowlist:
        raise ServiceError(
            "No Git hosts are permitted on this instance. Set "
            f"{_ALLOWED_GIT_HOSTS_ENV} to the Git host(s) you allow "
            "(comma or space separated), e.g. 'github.com'."
        )
    if not _host_in_allowlist(hostname, allowlist):
        raise ServiceError(
            f"git_url host '{hostname}' is not in the allowed Git hosts list "
            f"({_ALLOWED_GIT_HOSTS_ENV})."
        )

    # Post-resolution SSRF guard. The allowlist matched a name; make sure that
    # name does not resolve onto an internal, loopback or metadata address.
    _assert_git_host_resolves_safely(hostname, allowlist)

    # A password is always a secret, whatever the scheme. A bare username is only
    # a secret over http(s); the "git" in ssh://git@host is a user name, not one.
    if (
        parts.password or (scheme in ("http", "https") and parts.username)
    ) and not git_url_credentials_allowed():
        raise ServiceError(
            "Remove the username or token from the git_url. Enter the plain clone "
            "URL and save a git credential on the Git credentials page instead. "
            "(An administrator can allow URL-embedded credentials by setting "
            "CODEKB_ALLOW_CREDENTIALS_IN_GIT_URL=true.)"
        )

    return candidate


def validate_git_host(host: str) -> str:
    """Validate a bare Git host against the same fail-closed allowlist as
    validate_git_url, returning it lowercased.

    For the connection paths that take a self-hosted host directly (not a full
    URL): blocks pointing token validation at internal/metadata addresses, via
    both the name allowlist and the same post-resolution address guard as
    validate_git_url.
    Raises ServiceError on any violation.
    """
    hostname = (host or "").strip().strip("[]").lower()
    if not hostname:
        raise ServiceError("A host is required.")
    allowlist = _allowed_git_hosts()
    if not allowlist:
        raise ServiceError(
            "No Git hosts are permitted on this instance. Set "
            f"{_ALLOWED_GIT_HOSTS_ENV} to the Git host(s) you allow "
            "(comma or space separated), e.g. 'github.com'."
        )
    if not _host_in_allowlist(hostname, allowlist):
        raise ServiceError(
            f"Host '{hostname}' is not in the allowed Git hosts list ({_ALLOWED_GIT_HOSTS_ENV})."
        )
    _assert_git_host_resolves_safely(hostname, allowlist)
    return hostname


def generate_space_id(git_url: str) -> str:
    """Derive a human-readable space ID from a git URL with a random suffix.

    Examples:
        https://github.com/org/my-repo.git  -> my-repo-a3f2
        git@github.com:org/My_Cool_App.git  -> my-cool-app-9b1e
    """
    # Extract repo name from URL
    parsed = urlparse(git_url)
    path = parsed.path if parsed.path else git_url
    # Handle SSH-style URLs (git@host:org/repo.git)
    if ":" in path and not path.startswith("/"):
        path = path.split(":")[-1]
    repo_name = Path(path).stem  # strips .git

    return _slugify_to_space_id(repo_name)


def generate_space_id_from_name(name: str) -> str:
    """Derive a human-readable space ID from a user-supplied name.

    Examples:
        My Cool Project  -> my-cool-project-a3f2
        payment-service  -> payment-service-9b1e
    """
    return _slugify_to_space_id(name)


def _slugify_to_space_id(raw: str) -> str:
    """Slugify a raw string into a space ID with a random suffix."""
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    slug = slug[:54]  # leave room for dash + 8-char suffix (max 63 total)
    suffix = secrets.token_hex(4)  # 8 hex chars (32 bits)
    return f"{slug}-{suffix}"


def resolve_git_auth(git_url: str, actor_sub: str | None) -> tuple[str | None, str | None]:
    """Resolve ``(host, http.extraHeader value)`` for cloning ``git_url`` as ``actor_sub``.

    Returns ``(host, None)`` when there is no usable credential — the caller then
    clones credential-free, exactly as before. Never raises: a missing/expired
    credential or a KMS error degrades to no-auth, and a private clone then fails
    loud downstream with the normal access error.
    """
    host = urlparse(git_url).hostname if git_url else None
    if not host or not actor_sub:
        return host, None
    try:
        provider, token = resolve_repo_credential(actor_sub, git_url)
        if provider is None or not token:
            return host, None
        return host, provider.git_auth_header(token)
    except Exception as e:  # never let auth resolution break the clone path
        logger.warning(f"Git auth resolution failed for host '{host}': {e}")
        return host, None


def _embedded_url_token(git_url: str) -> str | None:
    """Token from a clone URL's userinfo for the visibility probe: the password
    of ``user:token@host``, or a lone ``token@host``. ``None`` if absent;
    percent-decoded."""
    try:
        parsed = urlparse((git_url or "").strip())
    except Exception:
        return None
    raw = parsed.password or parsed.username
    return unquote(raw) if raw else None


def lookup_repo_visibility(git_url: str, actor_sub: str | None) -> str:
    """``"public"`` or ``"private"`` for ``git_url``.

    Uses the caller's saved token when there is one so private repos answer
    "private" rather than an anonymous 404. Fails closed: when visibility can't
    be determined the create fails rather than storing a guess — a rejected token
    is a 400, a provider non-answer (unreachable, 5xx, rate limit) is a 503.
    """
    token: str | None = None
    try:
        _, token = resolve_repo_credential(actor_sub, git_url)
    except Exception as e:
        logger.warning(f"Saved-token lookup failed; checking repo visibility without it: {e}")
        token = None
    # No saved token (e.g. no credentials KMS key): fall back to one embedded in
    # the clone URL, allowed only when CODEKB_ALLOW_CREDENTIALS_IN_GIT_URL is on.
    if not token and git_url_credentials_allowed():
        token = _embedded_url_token(git_url)
    try:
        return get_visibility(git_url, token)
    except VisibilityError as e:
        # The caller's saved token was rejected — actionable, so surface it.
        raise ServiceError(str(e), status_code=400) from e
    except ProviderError as e:
        # Provider gave no usable answer; fail rather than store a guessed value.
        raise ServiceError(str(e), status_code=503) from e
    except ConfigurationError as e:
        # A malformed custom-host config (normally rejected at startup) — surface
        # it as a service error rather than letting it escape as a 500.
        raise ServiceError(f"Git provider is misconfigured: {e}", status_code=503) from e


def _git_env(auth_host: str | None = None, auth_header: str | None = None) -> dict[str, str]:
    """Git subprocess env, optionally injecting a host-scoped auth header.

    The base env (``_GIT_SUBPROCESS_ENV``) is credential-free and hardened. When
    a token is available for the repo's host, add a SECOND git config pair binding
    an ``http.extraHeader`` to that exact host: the credential is sent only to
    that host, is never written to ``.git/config``, and never appears in argv.
    """
    if not (auth_host and auth_header):
        # No credential to inject — reuse the hardened singleton unchanged.
        return _GIT_SUBPROCESS_ENV
    env = dict(_GIT_SUBPROCESS_ENV)
    env["GIT_CONFIG_COUNT"] = "2"
    env["GIT_CONFIG_KEY_1"] = f"http.https://{auth_host}/.extraheader"
    env["GIT_CONFIG_VALUE_1"] = auth_header
    return env


def build_git_env(git_url: str, actor_sub: str | None) -> dict[str, str]:
    """Convenience: resolve the caller's token for ``git_url`` and build the env."""
    host, header = resolve_git_auth(git_url, actor_sub)
    return _git_env(host, header)


def _is_git_dir_member(name: str) -> bool:
    """True if a tar member path is the ``.git`` directory or lives inside it."""
    return ".git" in Path(name).parts


def safe_extract_zip(zf: zipfile.ZipFile, dest: Path, max_extract_bytes: int) -> None:
    """Extract a zip archive into ``dest``, rejecting members that escape it.

    A substring check for ``".."`` is not sufficient: it misses drive-letter
    and UNC prefixes, and it says nothing about link members whose *target*
    points outside the destination. This validates the resolved path of every
    member instead:

    - members whose unix file type is anything other than a regular file or a
      directory (symlink, hardlink, device, fifo, socket) are rejected
    - absolute paths, including Windows drive and UNC forms, are rejected
    - ``realpath(dest / name)`` must land on ``realpath(dest)`` or below it

    All members are validated before anything is written, so an unsafe archive
    leaves no partial extraction behind. Because link members are rejected,
    extraction cannot introduce a symlink that would invalidate the resolved
    paths computed here.

    Raises:
        ValueError: if any member is unsafe, or if the archive's total
            uncompressed size exceeds ``max_extract_bytes`` (a non-positive
            value disables that check). The message is suitable for surfacing
            to the uploader.
    """
    dest.mkdir(parents=True, exist_ok=True)
    dest_root = os.path.realpath(dest)
    prefix = dest_root.rstrip(os.sep) + os.sep

    total_uncompressed = 0
    for info in zf.infolist():
        name = info.filename
        total_uncompressed += info.file_size

        # The unix mode sits in the high 16 bits of external_attr and is only
        # populated by unix-built archives. When the file-type bits are absent
        # (Windows/DOS archives) there is nothing to check, so skip the test
        # rather than reject a legitimate upload.
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if file_type and file_type not in (stat.S_IFREG, stat.S_IFDIR):
            raise ValueError(f"unsupported member type in zip: {name}")

        # Absolute paths: posix (/etc/...), Windows drive-relative (C:foo),
        # drive-absolute (C:\foo) and UNC (\\server\share\foo). ntpath covers
        # the Windows forms without misreading a colon elsewhere in the name.
        if (
            posixpath.isabs(name)
            or ntpath.isabs(name)
            or ntpath.splitdrive(name)[0]
            or name.startswith("\\")
        ):
            raise ValueError(f"absolute path in zip: {name}")

        resolved = os.path.realpath(os.path.join(dest_root, name))
        if resolved != dest_root and not resolved.startswith(prefix):
            raise ValueError(f"path traversal in zip: {name}")

    # Reject archives whose decompressed size would exhaust the task's
    # ephemeral storage (zip bomb): the compressed size does not bound
    # expansion. A non-positive ``max_extract_bytes`` disables the check.
    if max_extract_bytes > 0 and total_uncompressed > max_extract_bytes:
        raise ValueError(
            f"zip expands to {total_uncompressed} bytes, exceeding the "
            f"{max_extract_bytes}-byte extraction limit"
        )

    zf.extractall(dest)


class CodeKBService:
    """Service layer for CodeKB — a process-wide singleton.

    Use ``CodeKBService.instance()`` to obtain the one shared instance. Direct
    construction (``CodeKBService()``) is rejected, emulating a private constructor.
    """

    # A source zip is rejected when its decompressed size exceeds this multiple
    # of the compressed-upload cap -- an effective compression-ratio ceiling
    # (zip-bomb guard). Tied to the upload cap so the two cannot drift apart.
    _MAX_DECOMPRESSION_RATIO = 10

    # The one shared instance, created lazily by instance().
    _instance: "CodeKBService | None" = None
    _constructing: bool = False
    _instance_lock: "threading.Lock" = threading.Lock()

    def __new__(cls, *args: object, **kwargs: object) -> "CodeKBService":
        if not cls._constructing:
            raise RuntimeError(
                "CodeKBService is a singleton; call CodeKBService.instance() "
                "instead of constructing it directly."
            )
        return super().__new__(cls)

    @classmethod
    def instance(cls) -> "CodeKBService":
        """Return the shared singleton, building it on first call.

        Thread-safe via double-checked locking: the lock is taken only while the
        instance is still unbuilt, so once it exists every call is a lock-free
        attribute read. The ``_constructing`` flag is flipped inside the lock so
        the direct-construction guard in ``__new__`` is never briefly open to
        another thread.
        """
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._constructing = True
                    try:
                        cls._instance = cls()
                    finally:
                        cls._constructing = False
        return cls._instance

    def __init__(self, kb_manager: KnowledgeBaseManager | None = None) -> None:
        self.kb_manager = kb_manager or KnowledgeBaseManager()
        self.orchestrator = AnalysisOrchestrator(self.kb_manager)
        # LRU cache of space contexts, each tagged with its KB version.
        self._context_cache: OrderedDict[str, AnalysisContext] = OrderedDict()
        # Guards the cache bookkeeping only.
        self._context_cache_lock = threading.Lock()
        self._max_cached_spaces = int(get_config("service.max_cached_spaces"))
        if self._max_cached_spaces < 1:
            raise ConfigError(
                f"service.max_cached_spaces must be >= 1, got {self._max_cached_spaces}"
            )
        # Version-check TTL in seconds (config is hours; 0 disables).
        self._refresh_check_ttl_s = (
            float(require_config("source_cache.refresh_check_ttl_hours")) * 3600.0
        )
        # version_of lets the provider check if its on-disk tree is behind this ETag.
        self.source_provider = SourceCodeProvider(
            self.kb_manager, version_of=self._get_current_etag
        )

    # ── helpers ──────────────────────────────────────────────────────────

    def _ensure_context_cached(self, space_id: str) -> None:
        """Load a space's context into memory if missing, so reading its source
        also keeps its KB + call graph resident.

        Best-effort — any error is swallowed so warming never fails the read (no KB
        yet, or an S3 hiccup). A real context load elsewhere still raises.
        """
        try:
            self._load_context(space_id)
        except SpaceNotFoundError:
            pass
        except Exception as e:
            logger.debug(f"Could not warm context for '{space_id}': {e}")

    def _load_context(self, space_id: str) -> AnalysisContext:
        """Load space context (KB + catalog + flow analyzer), cached per space."""
        # Drop the cached context if the KB changed in S3 (TTL-gated).
        self._reconcile_version(space_id)
        with self._context_cache_lock:
            cached = self._context_cache.get(space_id)
            if cached is not None:
                self._context_cache.move_to_end(space_id)  # now most recently used
                return cached

        # Loaded outside the lock on purpose: load_space reads S3 and can take
        # seconds. Holding the lock across it would make one slow space block cache
        # lookups for every other space.
        try:
            ctx = self.orchestrator.load_space(space_id)
        except FileNotFoundError:
            raise SpaceNotFoundError(space_id)
        if ctx is None:
            raise SpaceNotFoundError(space_id)

        # Tag with the version it was built from (the provider compares it to the
        # on-disk tree to decide re-hydration).
        ctx.etag = self._read_kb_etag(space_id)
        ctx.checked_at = time.monotonic()

        with self._context_cache_lock:
            # The lock was released during the load, so another thread may have
            # cached this same space meanwhile. Prefer the entry already there, so
            # callers share one object instead of accumulating duplicate copies.
            existing = self._context_cache.get(space_id)
            if existing is not None:
                self._context_cache.move_to_end(space_id)
                return existing
            self._context_cache[space_id] = ctx

            while len(self._context_cache) > self._max_cached_spaces:
                evicted_id, _ = self._context_cache.popitem(last=False)
                logger.debug(
                    f"Evicted coldest space context '{evicted_id}' "
                    f"(cache at limit {self._max_cached_spaces})"
                )
        return ctx

    def _reconcile_version(self, space_id: str) -> None:
        """Drop the cached context if knowledge_base.json changed in S3.

        One HEAD per space per TTL. Never raises.
        """
        if self._refresh_check_ttl_s <= 0:
            return
        now = time.monotonic()
        with self._context_cache_lock:
            ctx = self._context_cache.get(space_id)
            if ctx is None:
                return  # nothing cached → the load sets the baseline
            if (now - ctx.checked_at) < self._refresh_check_ttl_s:
                return  # within TTL
            recorded = ctx.etag

        latest = self._read_kb_etag(space_id)  # HEAD outside the lock
        with self._context_cache_lock:
            ctx = self._context_cache.get(space_id)
            if ctx is None or latest is None:
                return  # gone, or HEAD failed (rare) → retry next call
            if recorded is not None and recorded != latest:
                logger.info(f"Space '{space_id}' knowledge base changed; dropping context")
                # Pop inline, not invalidate_context: we already hold the (non-reentrant) lock.
                self._context_cache.pop(space_id, None)
            else:
                ctx.etag = latest
                ctx.checked_at = now

    def _read_kb_etag(self, space_id: str) -> str | None:
        """Current S3 ETag of a space's knowledge_base.json, via HEAD. None on a miss."""
        try:
            bucket = self.kb_manager._get_s3_bucket()
            key = self.kb_manager._get_s3_key(space_id)
            resp = self.kb_manager.s3_client.head_object(Bucket=bucket, Key=key)
            etag = resp.get("ETag")
            return etag if isinstance(etag, str) else None
        except Exception as e:
            logger.debug(f"Could not read knowledge base ETag for '{space_id}': {e}")
            return None

    def _get_current_etag(self, space_id: str) -> str | None:
        """The ETag of the cached context, for the provider's disk-staleness check."""
        with self._context_cache_lock:
            ctx = self._context_cache.get(space_id)
            return ctx.etag if ctx else None

    def invalidate_context(self, space_id: str) -> None:
        """Remove a space from the context cache."""
        with self._context_cache_lock:
            self._context_cache.pop(space_id, None)

    # ── source code management ──────────────────────────────────────────

    def _source_local_path(self, space_id: str) -> Path:
        """Local path of a space's cached source tree."""
        return self.source_provider.cache_path(space_id)

    def _analysis_work_path(self, space_id: str) -> Path:
        """Local path of the analysis pipeline's working tree for a space.

        Distinct from the cache path on purpose. A working tree is scratch owned by a
        running pipeline, so it must never be evicted; the ``-analysis`` suffix puts
        it outside the eviction namespace, mirroring the refresh pipeline's
        ``-refresh`` clone.
        """
        return self.source_provider.work_path(space_id, WORK_DIR_ANALYSIS)

    def _source_s3_key(self, space_id: str) -> str:
        """S3 key for the archived source tarball."""
        return self.source_provider.source_s3_key(space_id)

    def _upload_source_archive(self, space_id: str, source_dir: Path) -> None:
        """Tar the cloned source and upload to S3."""
        archive_path = source_dir.parent / f"{space_id}-source.tar.gz"
        try:
            with tarfile.open(archive_path, "w:gz") as tar:
                # Exclude .git: it carries the remote config (and, historically,
                # any injected credential) and is dead weight for analysis.
                tar.add(
                    source_dir,
                    arcname=".",
                    filter=lambda ti: None if _is_git_dir_member(ti.name) else ti,
                )
            bucket = self.kb_manager._get_s3_bucket()
            key = self._source_s3_key(space_id)
            archive_bytes = archive_path.read_bytes()
            logger.debug(
                f"s3.put_object: s3://{bucket}/{key} ({len(archive_bytes):,} bytes, source tarball)"
            )
            self.kb_manager.s3_client.put_object(
                Bucket=bucket,
                Key=key,
                Body=archive_bytes,
            )
            logger.info(
                f"Uploaded source archive to s3://{bucket}/{key} ({len(archive_bytes):,} bytes)"
            )
        finally:
            archive_path.unlink(missing_ok=True)

    def read_source(self, space_id: str, rel_path: str) -> str:
        """Read one file from a space's source.

        Front door: warms the context cache, then reads via the provider (hydrates
        from S3 on a miss, retries once if the tree is evicted mid-read).

        Args:
            space_id: Space to read from.
            rel_path: Path relative to the source root.

        Returns:
            File contents, with undecodable bytes replaced.

        Raises:
            ValueError: If rel_path escapes the source root.
            FileNotFoundError: If the file does not exist in the repository.
            SourceNotAvailableError: If the archive cannot be fetched from S3.
        """
        self._ensure_context_cached(space_id)
        return self.source_provider.read_text(space_id, rel_path)

    def list_source(self, space_id: str, rel_path: str = "") -> list[tuple[str, bool]]:
        """List the visible children of a directory in a space's source.

        Front door: warms the context cache, then lists via the provider. See
        ``read_source`` for details.

        Args:
            space_id: Space to list from.
            rel_path: Directory relative to the source root. Empty for the root.

        Returns:
            ``(path relative to the source root, is a directory)`` pairs, sorted by
            name.

        Raises:
            ValueError: If rel_path escapes the source root.
            NotADirectoryError: If the path is not a directory in the repository.
            SourceNotAvailableError: If the archive cannot be fetched from S3.
        """
        self._ensure_context_cached(space_id)
        return self.source_provider.list_dir(space_id, rel_path)

    def ensure_source_local(self, space_id: str) -> Path:
        """Ensure a space's source is local and return its root.

        Front door for callers that need the root path to walk themselves: warms
        the context cache, then hydrates from S3 on a miss.

        Prefer ``read_source`` / ``list_source`` for a single file or a listing —
        they retry once if the tree is evicted mid-read, which a bare path can't.

        Args:
            space_id: Space whose source is needed.

        Returns:
            Path to the extracted source root.

        Raises:
            SourceNotAvailableError: If the archive cannot be fetched from S3.
        """
        self._ensure_context_cached(space_id)
        return self.source_provider.source_root(space_id)

    def source_subdir(self, space_id: str, rel_path: str = "") -> Path:
        """Safe absolute path to a subdirectory of a space's source.

        Front door for callers that walk a subtree themselves: warms the context
        cache, hydrates on a miss, and rejects a rel_path that escapes the root.

        Args:
            space_id: Space whose source is needed.
            rel_path: Directory relative to the source root. Empty for the root.

        Returns:
            The resolved absolute path (which may or may not exist yet).

        Raises:
            ValueError: If rel_path escapes the source root.
            SourceNotAvailableError: If the archive cannot be fetched from S3.
        """
        root = self.ensure_source_local(space_id)
        return self.source_provider.resolve_within(root, rel_path)

    # ── space management ──────────────────────────────────────────────

    def list_spaces(self) -> list[dict[str, Any]]:
        """List all spaces by reading lightweight status.json files from S3.

        Fan-out is parallelized across threads (botocore clients are
        thread-safe) so latency is bounded by the connection pool rather
        than by space count.
        """
        import concurrent.futures
        import time

        bucket_name = self.kb_manager._get_s3_bucket()
        logger.debug(f"s3.list_objects_v2: s3://{bucket_name}/spaces/ (delimiter=/)")

        # Paginate — list_objects_v2 caps CommonPrefixes at 1000 per page.
        paginator = self.kb_manager.s3_client.get_paginator("list_objects_v2")
        space_ids: list[str] = []
        for page in paginator.paginate(Bucket=bucket_name, Prefix="spaces/", Delimiter="/"):
            for prefix in page.get("CommonPrefixes", []) or []:
                space_ids.append(prefix["Prefix"].split("/")[1])
        logger.debug(f"s3.list_objects_v2 returned {len(space_ids)} space prefix(es)")

        if not space_ids:
            return []

        # Fan out status.json reads. Cap workers to fit inside the S3
        # client's connection pool (see KnowledgeBaseManager.s3_client).
        max_workers = min(32, max(4, len(space_ids)))
        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            statuses = list(ex.map(self._read_status, space_ids))
        logger.debug(
            f"list_spaces: fetched {len(space_ids)} status.json in "
            f"{(time.perf_counter() - t0) * 1000:.0f}ms (workers={max_workers})"
        )

        spaces: list[dict[str, Any]] = []
        for space_id, status in zip(space_ids, statuses):
            if status:
                stats = status.get("statistics", {})
                spaces.append(
                    {
                        "space_id": space_id,
                        "status": status.get("status", "UNKNOWN"),
                        "component_count": status.get("component_count"),
                        "source_directory": status.get("source_directory"),
                        "git_url": redact_git_url(status.get("git_url")),
                        "git_commit_hash": status.get("git_commit_hash"),
                        "source_type": status.get("source_type", "git"),
                        "space_name": status.get("space_name"),
                        # Empty on legacy spaces → resolves to the V1 stem scheme.
                        "id_scheme": status.get("id_scheme", ""),
                        "languages": status.get("languages"),
                        "language_count": len(status.get("languages", [])),
                        "total_lines": stats.get("total_lines"),
                        "vector_count": status.get("vector_count"),
                        "created_by": status.get("created_by"),
                        # authz fields for list filtering. visibility is computed
                        # from the FULL status (its git_url still has any userinfo)
                        # — the summary's git_url above is redacted, so deriving
                        # from it would misread a legacy private space as public.
                        "visibility": effective_visibility(status),
                        "provider": status.get("provider")
                        or get_provider(status.get("git_url") or ""),
                        "repo_key": status.get("repo_key") or repo_key(status.get("git_url")),
                        "created_by_sub": status.get("created_by_sub"),
                        # Prefer created_at (set on final READY write); fall
                        # back to started_at so spaces still in progress and
                        # legacy spaces without created_at still show a date.
                        "created_at": status.get("created_at") or status.get("started_at"),
                        "omit_low_business_relevance": status.get("omit_low_business_relevance"),
                        "error": status.get("error"),
                    }
                )
            else:
                # Legacy space without status.json — fall back to KB import. Its
                # provenance is unknown, so flag it so authz fails closed instead
                # of the no-git_url heuristic reading it as a public zip.
                space_info: dict[str, Any] = {
                    "space_id": space_id,
                    "status": "UNKNOWN",
                    "authz_unresolved": True,
                }
                try:
                    kb = self.kb_manager.import_knowledge_base(space_id)
                    space_info["component_count"] = kb.get_component_count()
                    space_info["source_directory"] = str(kb.source_directory)
                    space_info["language_count"] = len({c.language for c in kb.components})
                    space_info["languages"] = sorted({c.language for c in kb.components})
                    space_info["status"] = "READY"
                except Exception as e:
                    logger.warning(f"Failed to load space {space_id}: {e}")
                    space_info["error"] = str(e)
                spaces.append(space_info)

        return spaces

    def get_space(self, space_id: str) -> dict[str, Any]:
        """Get space details from status.json (fast path, no KB import).

        REFRESHING and REFRESH_FAILED read here too: these fields only change when a
        refresh completes, so they still describe the last completed snapshot.
        """
        status = self._read_status(space_id)
        if status and status.get("status") in ("READY", "REFRESHING", "REFRESH_FAILED"):
            return {
                "space_id": space_id,
                "space_name": status.get("space_name"),
                "source_directory": status.get("source_directory", ""),
                "component_count": status.get("component_count", 0),
                "created_at": status.get("created_at") or status.get("started_at"),
                "created_by": status.get("created_by"),
                "version": status.get("version", "1.0"),
                # Empty on legacy spaces → resolves to the V1 stem scheme.
                "id_scheme": status.get("id_scheme", ""),
                "statistics": status.get("statistics", {}),
                "vector_count": status.get("vector_count", 0),
                "git_url": redact_git_url(status.get("git_url", "")),
                "git_commit_hash": status.get("git_commit_hash"),
                # Backward compatibility: spaces created before patterns were
                # recorded use the legacy default list.
                "exclude_patterns": status.get("exclude_patterns", LEGACY_DEFAULT_EXCLUDE_PATTERNS),
                # Derived from the FULL status (git_url still carries any
                # userinfo). Zip/URL-less spaces resolve to "public".
                "visibility": effective_visibility(status),
            }

        # Fallback for legacy spaces without enriched status.json
        ctx = self._load_context(space_id)
        kb = ctx.knowledge_base
        return {
            "space_id": space_id,
            "space_name": None,
            "source_directory": str(kb.source_directory),
            "component_count": kb.get_component_count(),
            "created_at": kb.created_at,
            "created_by": None,
            "version": kb.version,
            "id_scheme": kb.id_scheme,
            "statistics": ctx.statistics,
            "vector_count": 0,
            "git_url": "",
            "git_commit_hash": None,
            "exclude_patterns": LEGACY_DEFAULT_EXCLUDE_PATTERNS,
            "visibility": "public",  # legacy/URL-less space → public
        }

    async def delete_space(self, space_id: str) -> dict[str, Any]:
        """Delete a space's vectors, knowledge base, and status from S3.

        **Order matters.** Vectors go first and metadata (the ``spaces/<id>/``
        prefix, including status.json) last, so if the process dies partway
        through, status.json still reads DELETING and the space stays listed for
        a retry.

        Caller must mark_space_deleting() BEFORE scheduling this in the background.
        """
        import asyncio

        self.invalidate_context(space_id)
        bucket_name = self.kb_manager._get_s3_bucket()
        now = datetime.now(timezone.utc).isoformat()

        # Read status before deleting (need clone_directory for local cleanup)
        status_data = self._read_status(space_id)
        try:
            has_kb = self.kb_manager._exists(space_id)
        except Exception as e:
            logger.warning(
                f"KB existence check failed for '{space_id}', proceeding with delete: {e}"
            )
            has_kb = False  # Don't let existence check block deletion
        if status_data is None and not has_kb:
            # Write FAILED status before raising
            self._write_status(space_id, {
                "space_id": space_id,
                "status": "FAILED",
                "error": f"Space '{space_id}' not found",
                "updated_at": now,
            })
            raise SpaceNotFoundError(space_id)

        errors = []

        try:
            # 1. Delete vectors from S3 Vectors store — FIRST, and a hard gate.
            try:
                from ..vector.s3_vectors_client import S3VectorsClient

                vector_client = S3VectorsClient()
                keys = await asyncio.to_thread(self._vector_keys_for_space, space_id)
                deleted = await asyncio.to_thread(vector_client.delete_vectors_by_keys, keys)
                if deleted:
                    logger.info(f"Vector cleanup for '{space_id}': {deleted} key(s) processed")
            except Exception as e:
                logger.error(
                    f"Vector cleanup failed for '{space_id}'; aborting delete before "
                    f"metadata is touched so the operation can be retried: {e}",
                    exc_info=True,
                )
                error_msg = (
                    f"Could not delete the vectors for space '{space_id}', so the space "
                    "was left intact. Nothing was removed. Try again once the vector "
                    "store is reachable."
                )
                # Write FAILED status
                self._write_status(space_id, {
                    "space_id": space_id,
                    "status": "FAILED",
                    "error": error_msg,
                    "updated_at": now,
                    **({"space_name": status_data.get("space_name")} if status_data and status_data.get("space_name") else {}),
                })
                raise ServiceError(error_msg) from e

            # 2. Mark any hyperspaces that reference this space as invalid. We
            # keep the deleted space_id in the list so the user sees what was
            # lost, and we do not cascade-delete even if membership drops below
            # the usual ≥2 minimum — the hyperspace stays until the user
            # explicitly fixes or deletes it.
            hyperspaces_invalidated: list[str] = []
            try:
                for hs in self.list_hyperspaces():
                    hs_id = hs.get("hyperspace_id")
                    space_ids = hs.get("space_ids", [])
                    if not hs_id or space_id not in space_ids:
                        continue
                    missing = list(dict.fromkeys([*hs.get("missing_space_ids", []), space_id]))
                    hs["missing_space_ids"] = missing
                    hs["invalid"] = True
                    hs["invalidated_at"] = datetime.now(timezone.utc).isoformat()
                    # Strip derived fields so we persist only config data
                    hs.pop("spaces", None)
                    self.kb_manager.s3_client.put_object(
                        Bucket=bucket_name,
                        Key=self._hyperspace_s3_key(hs_id),
                        Body=json.dumps(hs).encode("utf-8"),
                        ContentType="application/json",
                    )
                    hyperspaces_invalidated.append(hs_id)
                    logger.info(
                        f"Marked hyperspace '{hs_id}' invalid — member '{space_id}' was deleted"
                    )
            except Exception as e:
                errors.append(f"hyperspace cleanup: {e}")

            # 3. Delete ALL S3 objects under the space prefix
            prefix = f"spaces/{space_id}/"
            try:
                paginator = self.kb_manager.s3_client.get_paginator("list_objects_v2")
                pages_seen = 0
                for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
                    pages_seen += 1
                    keys = [obj["Key"] for obj in page.get("Contents", [])]
                    logger.debug(
                        f"s3.list_objects_v2 page {pages_seen}: prefix=s3://{bucket_name}/{prefix} "
                        f"returned {len(keys)} key(s)"
                    )
                    if keys:
                        logger.debug(
                            f"s3.delete_objects: {len(keys)} key(s) under s3://{bucket_name}/{prefix}"
                        )
                        self.kb_manager.s3_client.delete_objects(
                            Bucket=bucket_name,
                            Delete={"Objects": [{"Key": k} for k in keys]},
                        )
                        logger.info(f"Deleted {len(keys)} S3 objects under {prefix}")
            except Exception as e:
                logger.error(f"S3 prefix cleanup failed for '{space_id}': {e}", exc_info=True)
                error_msg = (
                    f"Could not fully delete space '{space_id}'. Its vectors were "
                    "removed but some stored objects remain; retry the delete to "
                    "finish."
                )
                self._write_status(space_id, {
                    "space_id": space_id,
                    "status": "FAILED",
                    "error": error_msg,
                    "updated_at": now,
                    **({"space_name": status_data.get("space_name")} if status_data and status_data.get("space_name") else {}),
                })
                raise ServiceError(error_msg) from e

            # 4. Best-effort cleanup of local source cache
            for local_path in (
                self._source_local_path(space_id),
                self._analysis_work_path(space_id),
                self.source_provider.work_path(space_id, WORK_DIR_REFRESH),
            ):
                if local_path.exists():
                    if discard_directory(local_path):
                        logger.info(f"Deleted local source directory: {local_path}")
                    else:
                        errors.append(f"local cache cleanup: could not remove {local_path}")

            # Only best-effort steps can have contributed here — anything that could
            # strand data has already raised.
            if errors:
                logger.warning(f"Partial delete for {space_id}: {errors}")

            logger.info(f"Successfully deleted space '{space_id}'")

            return {
                "space_id": space_id,
                "deleted": True,
                "hyperspaces_invalidated": hyperspaces_invalidated,
                "errors": errors,
            }

        except (ServiceError, SpaceNotFoundError):
            # Already logged and status written
            raise
        except Exception as e:
            # Unexpected error — write FAILED status
            logger.error(f"Unexpected error deleting space '{space_id}': {e}", exc_info=True)
            error_msg = f"Unexpected error during deletion: {str(e)}"
            self._write_status(space_id, {
                "space_id": space_id,
                "status": "FAILED",
                "error": error_msg,
                "updated_at": now,
                **({"space_name": status_data.get("space_name")} if status_data and status_data.get("space_name") else {}),
            })
            raise ServiceError(error_msg) from e

    # ── S3 status helpers ───────────────────────────────────────────────

    def read_status(self, space_id: str) -> dict[str, Any] | None:
        """Public seam: a space's status record, or None. Used by authz/backfill."""
        return self._read_status(space_id)

    def write_status(self, space_id: str, status: dict[str, Any]) -> None:
        """Public seam: persist a space's status record. Used by the backfill."""
        self._write_status(space_id, status)

    def _write_status(
        self, space_id: str, status: dict[str, Any], *, if_match: str | None = None
    ) -> None:
        """Write status.json to S3 for a space.

        When ``if_match`` is set, the write is conditional on the object still
        having that ETag (S3 ``IfMatch``). A concurrent writer that changed the
        object first makes this fail with ``PreconditionFailed``, which lets a
        read-modify-write claim the status atomically instead of racing.
        """
        bucket_name = self.kb_manager._get_s3_bucket()
        key = f"spaces/{space_id}/status.json"
        body = json.dumps(status, default=str).encode("utf-8")
        logger.debug(
            f"s3.put_object status: s3://{bucket_name}/{key} "
            f"status={status.get('status', '?')} step={status.get('current_step', '-')} "
            f"({len(body)} bytes)"
        )
        extra = {"IfMatch": if_match} if if_match else {}
        self.kb_manager.s3_client.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=body,
            ContentType="application/json",
            **extra,
        )

    def _read_status(self, space_id: str) -> dict[str, Any] | None:
        """Read status.json from S3. Returns None if not found."""
        status, _etag = self._read_status_with_etag(space_id)
        return status

    def _read_status_with_etag(
        self, space_id: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Read status.json plus its S3 ETag, for a conditional claim write."""
        bucket_name = self.kb_manager._get_s3_bucket()
        key = f"spaces/{space_id}/status.json"
        logger.debug(f"s3.get_object status: s3://{bucket_name}/{key}")
        try:
            resp = self.kb_manager.s3_client.get_object(Bucket=bucket_name, Key=key)
            status = json.loads(resp["Body"].read().decode("utf-8"))
            logger.debug(
                f"s3.get_object status ok: s3://{bucket_name}/{key} "
                f"status={status.get('status', '?')}"
            )
            return status, resp.get("ETag")
        except self.kb_manager.s3_client.exceptions.NoSuchKey:
            logger.debug(f"s3.get_object status NoSuchKey: s3://{bucket_name}/{key}")
            return None, None
        except Exception as e:
            logger.warning(f"s3.get_object status failed for s3://{bucket_name}/{key}: {e}")
            return None, None

    def mark_space_deleting(self, space_id: str) -> None:
        """Mark a space as DELETING before the background delete task starts.

        Atomically writes status=DELETING to status.json so the space appears as
        deleting immediately. Raises SpaceNotFoundError if the space doesn't exist.
        """
        current = self._read_status(space_id)
        if current is None:
            # Space must exist to delete it
            raise SpaceNotFoundError(space_id)

        if current.get("status") in ("ANALYZING", "SUMMARIZING", "REFRESHING"):
            raise SpaceBusyError(space_id)

        now = datetime.now(timezone.utc).isoformat()
        current.update({
            "space_id": space_id,
            "status": "DELETING",
            "updated_at": now,
        })
        self._write_status(space_id, current)
        logger.info(f"Marked space '{space_id}' as DELETING")

    # ── analysis ────────────────────────────────────────────────────────

    def _update_pipeline_status(
        self,
        space_id: str,
        status: str,
        step: str,
        step_status: str,
        extra: dict[str, Any] | None = None,
        refresh_run: bool = False,
    ) -> None:
        """Read-modify-write status.json, updating the pipeline steps list.

        Space-refresh steps are tracked in ``refresh_pipeline_runs`` (one entry
        per refresh run) so the original analysis ``pipeline_steps`` stay visible
        after a refresh instead of being overwritten. The latest run is updated
        when the status is REFRESHING, the step is a refresh-only step, or
        ``refresh_run`` is set: a refresh's last write, READY at ``unchanged`` or
        REFRESH_FAILED at the failed step (which can be a shared one such as ``clone``).
        """
        current = self._read_status(space_id) or {}
        now = datetime.now(timezone.utc).isoformat()

        def _upsert(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
            """Update an existing step in place, or append a new one."""
            for s in steps:
                if s["step"] == step:
                    s["status"] = step_status
                    s["updated_at"] = now
                    return steps
            steps.append(
                {
                    "step": step,
                    "status": step_status,
                    "started_at": now,
                    "updated_at": now,
                }
            )
            return steps

        is_refresh = refresh_run or status == "REFRESHING" or step in _REFRESH_ONLY_STEPS
        if is_refresh:
            # Each refresh run is its own entry in refresh_pipeline_runs so the UI
            # can show the initial analysis plus a history of every space refresh.
            # refresh_space() appends a fresh run on each trigger; here we always
            # update the most recent run.
            runs: list[dict[str, Any]] = current.get("refresh_pipeline_runs", [])
            if not runs:
                # Defensive: a step arrived without a run being opened first.
                runs = [{"started_at": now, "steps": []}]
            runs[-1]["steps"] = _upsert(runs[-1].get("steps", []))
            current["refresh_pipeline_runs"] = runs
        else:
            current["pipeline_steps"] = _upsert(current.get("pipeline_steps", []))

        current.update(
            {
                "space_id": space_id,
                "status": status,
                "current_step": step,
                "updated_at": now,
            }
        )
        if extra:
            current.update(extra)
        self._write_status(space_id, current)

    @staticmethod
    def _model_metadata(region: str | None = None) -> dict[str, Any]:
        """Space-level Bedrock model identity, written by every operation on the space.

        Always the summarization model, which writes the space's descriptions,
        so the field stays stable when the overview or chat use another model.
        Needed so users can compute actual token cost from the AWS Bedrock
        pricing page. Each workload can use its own model, so the per-operation
        ``by_model`` token records are the authoritative per-model breakdown.
        """
        from ..core.llm_settings import Workload
        from ..core.llm_settings import resolve as resolve_llm_settings

        return {
            "model_id": resolve_llm_settings(Workload.SUMMARIZATION).model_id,
            "region": region or require_config("llm.providers.bedrock.region"),
        }

    @staticmethod
    def _creation_usage(summary_stats: dict[str, Any] | None, kb: Any) -> dict[str, Any] | None:
        """The space_creation token record: summarization plus business vocabulary.

        Business vocabulary runs during analysis, before summarization, and its
        usage is carried in the knowledge base's analysis metadata.
        """
        metadata = getattr(kb, "analysis_metadata", None)
        business_context = (
            metadata.get("business_context_usage") if isinstance(metadata, dict) else None
        )
        return operation_usage([summary_stats or {}, business_context or {}])

    @staticmethod
    def _record_recurring_usage(
        existing: dict[str, Any] | None,
        run_usage: dict[str, Any] | None,
        *,
        trigger: str,
        count_key: str = "generation_count",
        last_key: str = "last_generation",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fold one run's token usage into a recurring per-operation record.

        Recurring operations (Space Overview regeneration, and later space
        refresh) run many times against the same space. This keeps a running
        ``cumulative`` total, a monotonically increasing count, the most recent
        run, and a bounded ``recent`` history. Returns the new record; callers
        store it under the appropriate ``token_usage`` key.
        """
        from ..core.token_usage import TokenUsage

        existing = existing or {}
        cumulative = TokenUsage.from_dict(existing.get("cumulative"))
        cumulative.merge(TokenUsage.from_dict(run_usage))

        last = dict(run_usage or {})
        last["at"] = datetime.now(timezone.utc).isoformat()
        last["trigger"] = trigger
        if extra:
            last.update(extra)

        limit = int(require_config("pricing.recent_history_limit"))
        recent = list(existing.get("recent", []))
        recent.append(last)
        if limit > 0:
            recent = recent[-limit:]

        return {
            "cumulative": cumulative.to_dict(),
            count_key: int(existing.get(count_key, 0)) + 1,
            last_key: last,
            "recent": recent,
        }

    # ── source acquisition steps ───────────────────────────────────────

    def _fetch_remote_refs(
        self, git_url: str, env: dict[str, str] | None = None
    ) -> tuple[list[str], list[str]] | None:
        """Return (branches, tags) for a remote repo via ``git ls-remote``.

        Returns None if the remote can't be reached or listed (network/auth),
        so callers can decide whether to surface or ignore that.
        """
        try:
            result = run_git(
                ["git", "ls-remote", "--heads", "--tags", git_url],
                capture_output=True,
                text=True,
                # ls-remote is measured to be short RTT; also caps how long the
                # create-space button stays disabled.
                timeout=10,
                env=env or _GIT_SUBPROCESS_ENV,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"ls-remote timed out for {git_url}")
            return None
        if result.returncode != 0:
            logger.warning(f"ls-remote failed for {git_url}: {result.stderr.strip()}")
            return None

        branches: list[str] = []
        tags: list[str] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 2:
                continue
            ref = parts[1]
            if ref.endswith("^{}"):  # peeled tag entries
                continue
            if ref.startswith("refs/heads/"):
                branches.append(ref.removeprefix("refs/heads/"))
            elif ref.startswith("refs/tags/"):
                tags.append(ref.removeprefix("refs/tags/"))

        return sorted(branches), sorted(tags)

    def list_remote_branches(self, git_url: str, actor_sub: str | None = None) -> dict[str, Any]:
        """List branches and tags for a remote git repository.

        Raises ServiceError if the remote cannot be reached or listed, so the
        API can return a clear error to the caller. When ``actor_sub`` has a
        stored credential for the repo's host, the caller's own token is used so
        private repos can be listed.
        """
        git_url = validate_git_url(git_url)

        refs = self._fetch_remote_refs(git_url, env=build_git_env(git_url, actor_sub))
        if refs is None:
            raise ServiceError(
                f"Could not list branches for {redact_git_url(git_url)}. Check that the URL is "
                "correct and the repository is reachable. For a private repository, save a "
                "credential with access to it on the Git credentials page."
            )
        branches, tags = refs
        return {"git_url": redact_git_url(git_url), "branches": branches, "tags": tags}

    def _validate_remote_branch(
        self, git_url: str, branch: str, env: dict[str, str] | None = None
    ) -> str | None:
        """Check that ``branch`` exists on the remote.

        Returns None if the branch (or tag) exists, otherwise a human-readable
        error message that includes a sample of the available branches. If the
        remote cannot be reached we return None and let the subsequent clone
        surface the real connectivity/auth error.
        """
        refs = self._fetch_remote_refs(git_url, env=env)
        if refs is None:
            # Could not list refs (network/auth). Don't block — let clone report it.
            return None

        branches, tags = refs
        if branch in branches or branch in tags:
            return None

        sample = ", ".join(branches[:10])
        more = f" (and {len(branches) - 10} more)" if len(branches) > 10 else ""
        return (
            f"Branch '{branch}' not found in {redact_git_url(git_url)}. "
            f"Available branches/tags: {sample}{more}"
        )

    def _acquire_source_git(
        self,
        space_id: str,
        source_dir: Path,
        git_url: str,
        branch: str | None = None,
        actor_sub: str | None = None,
    ) -> dict[str, Any] | None:
        """Acquire source code by cloning a git repository.

        If ``branch`` is None or empty, the repository's default branch is used
        (rather than assuming "main", which fails for repos whose default branch
        has a different name).

        When ``actor_sub`` has a stored credential for the repo's host, the
        caller's token is injected (host-scoped ``http.extraHeader``) so private
        repos clone; otherwise the clone runs credential-free as before.

        Returns source-specific metadata for the final status, or None on failure.
        """
        self._update_pipeline_status(space_id, "ANALYZING", "clone", "in_progress")

        # Resolve the caller's git credential for this host, if any. Built once
        # and reused for branch validation and the clone.
        git_env = build_git_env(git_url, actor_sub)

        # Normalize the branch: trim surrounding whitespace/newlines that often
        # sneak in via copy-paste. A value like "b-8.0.x " would otherwise be
        # passed verbatim to git and fail with a misleading "Remote branch not
        # found" error that looks identical to a typo.
        branch = branch.strip() if branch else None

        # If a branch was requested, validate it against the remote first so we
        # can return a clear, actionable error (and the list of available
        # branches) instead of a raw git failure after a wasted clone attempt.
        if branch:
            validation_error = self._validate_remote_branch(git_url, branch, env=git_env)
            if validation_error:
                logger.error(f"[worker] Branch validation failed: {validation_error}")
                self._update_pipeline_status(
                    space_id,
                    "FAILED",
                    "clone",
                    "failed",
                    extra={"error": validation_error},
                )
                return None

        branch_desc = branch if branch else "default"
        logger.info(f"[worker] Cloning {git_url} (branch={branch_desc}) into {source_dir}")

        cmd = ["git", "clone", "--depth", "1"]
        if branch:
            cmd += ["--branch", branch]
        cmd += ["--", git_url, str(source_dir)]
        result = run_git(cmd, capture_output=True, text=True, timeout=300, env=git_env)
        if result.returncode != 0:
            error_msg = result.stderr.strip() or "git clone failed"
            logger.error(f"[worker] Clone failed: {error_msg}")
            self._update_pipeline_status(
                space_id,
                "FAILED",
                "clone",
                "failed",
                extra={
                    "error": "Git clone failed. Check the repository URL, branch, and access, then try again."
                },
            )
            return None
        self._update_pipeline_status(space_id, "ANALYZING", "clone", "completed")

        # Capture the exact commit hash for reproducibility
        commit_hash = None
        try:
            hash_result = run_git(
                ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if hash_result.returncode == 0:
                commit_hash = hash_result.stdout.strip()
                self._update_pipeline_status(
                    space_id,
                    "ANALYZING",
                    "clone",
                    "completed",
                    extra={"git_commit_hash": commit_hash},
                )
        except Exception as e:
            logger.warning(f"[worker] Failed to capture commit hash: {e}")

        # Resolve the actual branch name if it wasn't explicitly requested
        resolved_branch = branch
        if not resolved_branch:
            try:
                branch_result = run_git(
                    ["git", "-C", str(source_dir), "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if branch_result.returncode == 0:
                    resolved_branch = branch_result.stdout.strip() or None
            except Exception as e:
                logger.warning(f"[worker] Failed to resolve default branch: {e}")

        logger.info(f"[worker] Clone complete: {source_dir} (commit={commit_hash})")
        self._upload_source_archive(space_id, source_dir)

        return {
            "git_url": git_url,
            "git_commit_hash": commit_hash,
            "branch": resolved_branch,
        }

    def _acquire_source_upload(
        self, space_id: str, source_dir: Path, space_name: str
    ) -> dict[str, Any] | None:
        """Acquire source code by downloading and extracting an uploaded zip.

        Returns source-specific metadata for the final status, or None on failure.
        """
        self._update_pipeline_status(space_id, "ANALYZING", "extract", "in_progress")
        bucket = self.kb_manager._get_s3_bucket()
        upload_key = self._upload_s3_key(space_id)

        logger.info(f"[worker] Downloading upload from s3://{bucket}/{upload_key}")
        logger.debug(f"s3.get_object: s3://{bucket}/{upload_key}")
        try:
            resp = self.kb_manager.s3_client.get_object(Bucket=bucket, Key=upload_key)
            zip_bytes = resp["Body"].read()
            logger.debug(f"s3.get_object ok: s3://{bucket}/{upload_key} ({len(zip_bytes):,} bytes)")
        except Exception as e:
            logger.error(
                f"s3.get_object failed for upload s3://{bucket}/{upload_key}: {e}",
                exc_info=True,
            )
            self._update_pipeline_status(
                space_id,
                "FAILED",
                "extract",
                "failed",
                extra={"error": f"Failed to download upload: {e}"},
            )
            return None

        # Write zip to temp file and extract
        zip_path = source_dir.parent / f"{space_id}-upload.zip"
        try:
            zip_path.write_bytes(zip_bytes)
            with zipfile.ZipFile(zip_path, "r") as zf:
                safe_extract_zip(zf, source_dir, self._max_extract_bytes())
        except (zipfile.BadZipFile, ValueError) as e:
            logger.warning(
                f"[worker] Rejected upload for space '{space_id}' "
                f"from s3://{bucket}/{upload_key}: {e}"
            )
            self._update_pipeline_status(
                space_id,
                "FAILED",
                "extract",
                "failed",
                extra={"error": f"Invalid zip file: {e}"},
            )
            return None
        finally:
            zip_path.unlink(missing_ok=True)

        # If the zip contained a single root folder, flatten it.
        # Rename to a temp name first to avoid conflicts when a child has
        # the same name as the root dir (e.g. Foo/Foo/...).
        entries = list(source_dir.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            single_dir = entries[0]
            tmp = single_dir.with_name(single_dir.name + "__tmp")
            single_dir.rename(tmp)
            for item in tmp.iterdir():
                item.rename(source_dir / item.name)
            tmp.rmdir()

        self._update_pipeline_status(space_id, "ANALYZING", "extract", "completed")
        logger.info(f"[worker] Extracted upload to {source_dir}")

        self._upload_source_archive(space_id, source_dir)

        # Clean up the upload zip from S3
        try:
            self.kb_manager.s3_client.delete_object(Bucket=bucket, Key=upload_key)
        except Exception as e:
            logger.warning(
                f"[worker] Failed to clean up upload zip s3://{bucket}/{upload_key}: {e}"
            )

        return {
            "source_type": "upload",
            "space_name": space_name,
        }

    # ── unified pipeline ────────────────────────────────────────────────

    def _run_pipeline(
        self,
        space_id: str,
        source_dir: Path,
        summarize: bool,
        omit_low_business_relevance: bool,
        acquire_source: Callable[[], dict[str, Any] | None],
    ) -> None:
        """Background worker: acquire source, analyze, summarize, finalize.

        This is the single pipeline used by both git-clone and zip-upload flows.
        The only variation is the source acquisition step, passed as a callable
        that returns source-specific metadata (or None on failure).

        ``source_dir`` is the ``-analysis`` working tree, not the space's cache
        entry. Keeping the two apart is what makes eviction safe without a lease
        table: this tree is scratch by name, so nothing will reclaim it while the
        pipeline is running, and the ``finally`` below returns the disk when the
        pipeline ends however it ends.
        """
        # The cached source tree, if any, is about to be superseded by whatever this
        # run produces, so drop it now rather than let a reader serve the previous
        # revision. The refresh pipeline does the same in _step_invalidate_caches.
        # discard_directory does not raise, so a failed delete cannot escape ahead of
        # the error handler below and strand the space in ANALYZING with no terminal
        # status written.
        self.source_provider.invalidate(space_id)

        # A working tree left by an earlier attempt (killed container, retried
        # submission) would otherwise be cloned into and confuse the analysis.
        discard_directory(source_dir)
        source_dir.mkdir(parents=True, exist_ok=True)

        # Ask for room before the clone or unzip: one of the two places the local
        # cache grows. make_room only enforces the size ceiling against existing cache
        # entries — the incoming source's size isn't knowable here — so it bounds
        # CodeKB's own cache, not total disk. Free space is not consulted. Never raises.
        self.source_provider.make_room(incoming_space_id=space_id)

        try:
            # Step 1: Acquire source (git clone or zip extract)
            source_metadata = acquire_source()
            if source_metadata is None:
                return  # acquisition failed and already wrote FAILED status

            # Step 2: Parse & analyze
            self._update_pipeline_status(space_id, "ANALYZING", "analyze", "in_progress")
            self.invalidate_context(space_id)
            logger.info(f"[worker] Analyzing {source_dir} as space '{space_id}'...")

            def _on_business_context_status(
                step_status: str, business_context: dict[str, Any] | None
            ) -> None:
                if step_status == "in_progress":
                    self._update_pipeline_status(space_id, "ANALYZING", "analyze", "completed")
                extra = {"business_context": business_context} if business_context else None
                self._update_pipeline_status(
                    space_id,
                    "ANALYZING",
                    "business_context",
                    step_status,
                    extra=extra,
                )

            ctx = self.orchestrator.analyze_space(
                source_dir,
                space_id,
                business_behavior_enabled=omit_low_business_relevance,
                on_business_context_status=_on_business_context_status,
                persisted_source_root=self.source_provider.cache_path(space_id),
            )
            component_count = ctx.knowledge_base.get_component_count()
            languages = sorted({c.language for c in ctx.knowledge_base.components})

            # No components means none of the files matched a supported
            # language parser (a repository written entirely in a language
            # CodeKB has no parser for). Fail with a clear, actionable message
            # rather than producing an empty space.
            if component_count == 0:
                supported = ", ".join(sorted(self.orchestrator.get_supported_languages()))
                raise ServiceError(
                    "No analyzable source code was found in this repository. "
                    "CodeKB analyzed the cloned source but did not find any files "
                    "written in a supported programming language, so there was "
                    "nothing to index.\n\n"
                    f"Supported languages: {supported}.\n\n"
                    "If this repository is written in a language that isn't in "
                    "that list, it can't be analyzed yet. Otherwise, double-check "
                    "that you pointed CodeKB at the correct repository and branch, "
                    "and check the exclusion patterns on the Settings page: files "
                    "matching them are not analyzed."
                )

            self._update_pipeline_status(
                space_id,
                "ANALYZING",
                "analyze",
                "completed",
                extra={
                    "component_count": component_count,
                    "languages": languages,
                    "statistics": ctx.statistics,
                    # Record the id scheme this space was indexed under so the
                    # catalog can tell new (V2 path-hashed) spaces from legacy
                    # (V1 stem) ones without importing the full KB.
                    "id_scheme": getattr(ctx.knowledge_base, "id_scheme", ""),
                },
            )
            logger.info(
                f"[worker] Analysis complete: {component_count} components, languages={languages}"
            )

            # Step 3: Space Overview — skipped during pipeline; user triggers it
            # on-demand via POST /spaces/{id}/generate-overview.

            # Step 4: Summarize (optional)
            summary_stats = None
            if summarize:
                summary_stats = self._run_summarization(
                    space_id,
                    source_dir,
                    ctx,
                    omit_low_business_relevance=omit_low_business_relevance,
                )

            # Step 5: Save & finalize
            self._finalize_pipeline(space_id, ctx, summarize, summary_stats, source_metadata)

        except Exception as e:
            logger.error(f"[worker] Pipeline failed for {space_id}: {e}", exc_info=True)
            existing = self._read_status(space_id) or {}
            # Keep existing fields so git_url + authz survive; a fresh dict would
            # drop git_url and make a failed private repo look like a public zip.
            failed_status: dict[str, Any] = {
                **existing,
                "space_id": space_id,
                "status": "FAILED",
                "error": str(e),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write_status(space_id, failed_status)
        finally:
            # Return the working tree on every exit path — success, a step writing
            # FAILED and returning early, or an exception. It has no readers outside
            # this pipeline: source reads go through the provider, which hydrates the
            # cache entry from S3 instead. Mirrors RefreshOrchestrator.run().
            #
            # Not conditional on success. A failed analysis leaves an unusable
            # half-populated tree, and keeping it would hold disk that a working
            # space could use.
            discard_directory(source_dir)

    def _run_summarization(
        self,
        space_id: str,
        source_dir: Path,
        ctx: AnalysisContext,
        omit_low_business_relevance: bool = False,
    ) -> dict[str, Any] | None:
        """Run the optional summarization step. Returns summary stats or None."""
        self._update_pipeline_status(space_id, "SUMMARIZING", "summarize", "in_progress")
        logger.info(f"[worker] Starting summarization for '{space_id}'...")
        try:
            import asyncio
            import time as _time
            from ..vector.topdown_summarizer import TopDownSummarizer

            # Throttle S3 progress writes: at most once every 15 seconds
            _last_progress_write = [0.0]

            def _on_progress(done: int, total: int) -> None:
                now = _time.monotonic()
                # Always write the first (0/N) and last (N/N), throttle the rest
                if done > 0 and done < total and (now - _last_progress_write[0]) < 15.0:
                    return
                _last_progress_write[0] = now
                pct = round((done / total) * 100, 1) if total > 0 else 0.0
                self._update_pipeline_status(
                    space_id,
                    "SUMMARIZING",
                    "summarize",
                    "in_progress",
                    extra={
                        "summarize_total": total,
                        "summarize_done": done,
                        "summarize_percent": pct,
                    },
                )

            summarizer = TopDownSummarizer(
                str(source_dir),
                space_id,
                codekb_service=self,
                on_progress=_on_progress,
                omit_low_business_relevance=omit_low_business_relevance,
            )
            # A retry runs on the same source as the run it resumes, so the
            # descriptions that run stored are still right: describe only the rest.
            saved: dict[str, str] = {}
            if (self._read_status(space_id) or {}).get("retried_at"):
                from ..vector.s3_vectors_client import S3VectorsClient

                callables = set(self.get_call_depths(space_id))
                saved = S3VectorsClient().get_descriptions(sorted(callables))
                logger.info(f"[worker] Retry keeps {len(saved)} stored description(s)")
            summary_stats = asyncio.run(
                summarizer.summarize_all(
                    targets=callables - set(saved) if saved else None,
                    prior_descriptions=saved or None,
                )
            )
            # total_analyzed becomes the space's vector_count.
            summary_stats["total_analyzed"] += len(saved)
            stopped_early = summary_stats.get("stopped_early")
            if stopped_early:
                # Stopped early: fail the step with a curated remedy under
                # summarize_remedy (kept separate from raw-str(e) summarize_error).
                if stopped_early == "invalid_llm_settings":
                    remedy = (
                        "AI summarization stopped because Bedrock rejected the "
                        "configured model settings. An administrator must correct "
                        "the CodeKB LLM settings named in the service logs, then "
                        "create a new space for this repository."
                    )
                else:
                    remedy = (
                        "AI summarization stopped early and did not cover "
                        "every component. To fix, create a new space for "
                        "this repository to re-run full summarization."
                    )
                self._update_pipeline_status(
                    space_id,
                    "SUMMARIZING",
                    "summarize",
                    "failed",
                    extra={
                        "summarize_remedy": remedy,
                        "summarize_coverage": summary_stats.get("coverage"),
                    },
                )
                logger.warning(
                    f"[worker] Summarization stopped early for {space_id}: "
                    f"{stopped_early} ({summary_stats['total_analyzed']} analyzed)"
                )
            else:
                self._update_pipeline_status(
                    space_id,
                    "SUMMARIZING",
                    "summarize",
                    "completed",
                    extra={
                        "vector_count": summary_stats["total_analyzed"],
                        "summarize_coverage": summary_stats.get("coverage"),
                    },
                )
                logger.info(
                    f"[worker] Summarization complete: "
                    f"{summary_stats['total_analyzed']} analyzed, "
                    f"{summary_stats['total_cached']} cached"
                )
            return summary_stats
        except Exception as e:
            self._update_pipeline_status(
                space_id,
                "SUMMARIZING",
                "summarize",
                "failed",
                extra={"summarize_error": str(e)},
            )
            logger.error(f"[worker] Summarization failed for {space_id}: {e}", exc_info=True)
            return None

    def _run_space_overview(
        self,
        space_id: str,
    ) -> dict[str, Any] | None:
        """Run the Space Overview documentation generation step.

        Uses a Strands agent with the existing Hopper tools to explore the
        codebase and generate reverse engineered markdown documents. Each document
        is saved as a separate .md file in S3. The document catalog is stored
        in status.json.

        Returns overview stats or None on failure.
        """
        # Write overview progress without changing the top-level space status.
        # (Prior docs were already deleted + the catalog cleared synchronously
        # in start_overview_generation, so the UI never sees a catalog pointing
        # at deleted objects during regeneration.)
        current = self._read_status(space_id) or {}
        current["overview_status"] = "in_progress"
        current["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write_status(space_id, current)

        logger.info(f"[worker] Starting Space Overview generation for '{space_id}'...")
        try:
            import time as _time
            from ..vector.space_overview_generator import SpaceOverviewGenerator

            _last_progress_write = [0.0]

            def _on_progress(done: int, total: int) -> None:
                now = _time.monotonic()
                if done > 0 and done < total and (now - _last_progress_write[0]) < 15.0:
                    return
                _last_progress_write[0] = now
                pct = round((done / total) * 100, 1) if total > 0 else 0.0
                cur = self._read_status(space_id) or {}
                cur.update(
                    {
                        "overview_status": "in_progress",
                        "overview_total": total,
                        "overview_done": done,
                        "overview_percent": pct,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                self._write_status(space_id, cur)

            generator = SpaceOverviewGenerator(
                space_id=space_id,
                on_progress=_on_progress,
            )
            result = generator.generate_all()

            # Save each generated .md file to S3
            bucket = self.kb_manager._get_s3_bucket()
            doc_catalog = []
            for doc in result["documents"]:
                s3_path = f"spaces/{space_id}/docs/{doc['filename']}"
                self.kb_manager.s3_client.put_object(
                    Bucket=bucket,
                    Key=s3_path,
                    Body=doc["content"].encode("utf-8"),
                    ContentType="text/markdown",
                )
                doc_catalog.append(
                    {
                        "key": doc["key"],
                        "title": doc["title"],
                        "s3_path": s3_path,
                        "generated_at": doc["generated_at"],
                    }
                )
                logger.info(f"[worker] Saved overview doc: s3://{bucket}/{s3_path}")

            cur = self._read_status(space_id) or {}
            overview_usage = operation_usage([result["stats"]])
            token_usage = cur.get("token_usage") or {}
            prior_overview = token_usage.get("space_overview") or {}
            trigger = "manual" if prior_overview.get("generation_count") else "initial"
            if overview_usage:
                token_usage["space_overview"] = self._record_recurring_usage(
                    prior_overview,
                    overview_usage,
                    trigger=trigger,
                    extra={"documents": result["stats"].get("generated", 0)},
                )
            cur.update(
                {
                    "overview_status": "completed",
                    "space_overview_docs": doc_catalog,
                    "overview_stats": result["stats"],
                    "bedrock_model": self._model_metadata(result["stats"].get("region")),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            if token_usage:
                cur["token_usage"] = token_usage
            self._write_status(space_id, cur)

            logger.info(
                f"[worker] Space Overview complete: "
                f"{result['stats']['generated']} generated, "
                f"{result['stats']['failed']} failed"
            )
            return result["stats"]

        except Exception as e:
            cur = self._read_status(space_id) or {}
            cur.update(
                {
                    "overview_status": "failed",
                    "overview_error": str(e),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            self._write_status(space_id, cur)
            logger.error(
                f"[worker] Space Overview failed for {space_id}: {e}",
                exc_info=True,
            )
            return None

    def get_space_overview_document(self, space_id: str, doc_key: str) -> str:
        """Read a Space Overview markdown document from S3.

        Args:
            space_id: Space identifier.
            doc_key: Document key (e.g. 'business_overview').

        Returns:
            The markdown content as a string.

        Raises:
            SpaceNotFoundError: If the document doesn't exist.
        """
        bucket = self.kb_manager._get_s3_bucket()
        # Look up the filename from the document definitions
        from ..vector.space_overview_generator import OVERVIEW_DOCUMENTS

        doc_def = next((d for d in OVERVIEW_DOCUMENTS if d["key"] == doc_key), None)
        if not doc_def:
            raise SpaceNotFoundError(f"Unknown overview document: {doc_key}")

        s3_key = f"spaces/{space_id}/docs/{doc_def['filename']}"
        try:
            resp = self.kb_manager.s3_client.get_object(Bucket=bucket, Key=s3_key)
            return resp["Body"].read().decode("utf-8")
        except Exception:
            raise SpaceNotFoundError(f"Overview document not found: {doc_key} for space {space_id}")

    @staticmethod
    def _overview_run_is_fresh(record: dict[str, Any]) -> bool:
        """Whether an overview run is in progress and its heartbeat is recent.

        A fresh ``in_progress`` run means a generator is genuinely active, so a
        new trigger must be refused. A run whose ``updated_at`` heartbeat is
        older than ``summarization.fail_after_stale_s`` is treated as dead (e.g.
        its worker task was recycled with no checkpoint/resume) and a restart is
        allowed — mirrors the stale-in-progress detection in get_analysis_status.
        The overview workers refresh ``updated_at`` on every document, so a live
        run always looks fresh and a dead one eventually ages out.
        """
        if record.get("overview_status") != "in_progress":
            return False
        updated_at = record.get("updated_at")
        if not updated_at:
            return True  # in_progress with no heartbeat → assume a run is active
        try:
            age_s = (
                datetime.now(timezone.utc) - datetime.fromisoformat(updated_at)
            ).total_seconds()
        except (ValueError, TypeError):
            return True  # malformed heartbeat → do not clobber a possible run
        fail_after = int(require_config("summarization.fail_after_stale_s"))
        if fail_after <= 0:
            return True
        return age_s <= fail_after

    # Recorded on the space or hyperspace when its job's thread could not start.
    _JOB_START_FAILED = (
        "The background job could not start because the server is short of "
        "resources. Try again in a few minutes."
    )

    @staticmethod
    @contextmanager
    def _undo_unless_submitted(slot: PipelineSlot, undo: Callable[[], None]) -> Iterator[None]:
        """Run ``undo`` if the block raises before or while submitting the job, then re-raise.

        Open this right after the ANALYZING, REFRESHING or overview ``in_progress``
        write, and submit the job as the block's last statement. Anything that
        raises in between leaves that record in progress with no worker behind it:
        an S3 error while deleting a prior overview's documents, or
        ``Thread.start()`` failing ("can't start new thread") when the OS is out of
        threads or memory. The pool returns the slot either way, but the record
        would stay in progress until it goes stale
        (``summarization.fail_after_stale_s``, 12h by default). ``undo`` writes it
        back to a state the user can act on. If ``undo`` itself fails, the
        original error still reaches the caller.
        """
        try:
            yield
        except Exception:
            # The API's catch-all handler logs the traceback; this names the job.
            logger.error(
                f"Pipeline could not start: kind={slot.kind}, target={slot.target}; "
                f"undoing its status write"
            )
            try:
                undo()
            except Exception:
                logger.exception(
                    f"Could not undo the status write: kind={slot.kind}, "
                    f"target={slot.target}. It stays in progress until it goes stale."
                )
            raise

    @staticmethod
    def _is_precondition_failed(err: ClientError) -> bool:
        """True when a conditional S3 write lost the race (another writer won)."""
        return err.response.get("Error", {}).get("Code") == "PreconditionFailed"

    def start_overview_generation(self, space_id: str) -> dict[str, Any]:
        """Trigger Space Overview generation for an existing READY space.

        Runs in a background thread. Returns immediately with status. Raises
        ServiceError if the space is not READY, or OverviewInProgressError (409)
        if a generation run is already active for this space.
        """
        status, etag = self._read_status_with_etag(space_id)
        if not status:
            raise SpaceNotFoundError(space_id)
        if status.get("status") != "READY":
            raise ServiceError(
                f"Space '{space_id}' is not READY (status: {status.get('status')}). "
                f"Cannot generate overview."
            )
        # Refuse a duplicate while a fresh run is in progress (double-click, retry
        # after a slow response, or a second user) — otherwise a second generator
        # starts, doubling Bedrock cost and making the final catalog depend on
        # whichever run finishes last.
        if self._overview_run_is_fresh(status):
            raise OverviewInProgressError(space_id)

        # Reserve a pipeline slot before the claim write, so a 429 leaves no
        # in_progress claim behind. A lost claim below returns the slot.
        with pipeline_pool().reserve("space_overview", space_id) as slot:
            # Claim the run atomically BEFORE touching S3 objects: clear the catalog
            # and mark in_progress with a conditional write (IfMatch on the ETag we
            # just read). If another request claimed it between our read and write,
            # the write fails and we refuse rather than starting a second generator.
            prior_docs = status.get("space_overview_docs") or []
            status["space_overview_docs"] = []
            status["overview_status"] = "in_progress"
            status["overview_done"] = 0
            status["overview_total"] = 0
            status["overview_percent"] = 0.0
            status["updated_at"] = datetime.now(timezone.utc).isoformat()
            try:
                self._write_status(space_id, status, if_match=etag)
            except ClientError as e:
                if self._is_precondition_failed(e):
                    raise OverviewInProgressError(space_id) from e
                raise

            # The claim is ours. If the job then fails to start, end the run failed
            # rather than leave it in_progress. The catalog is already empty and some
            # prior documents may be deleted, so it ends as a failed run would.
            def _undo_claim() -> None:
                status.update(
                    {
                        "overview_status": "failed",
                        "overview_error": self._JOB_START_FAILED,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                self._write_status(space_id, status)

            with self._undo_unless_submitted(slot, _undo_claim):
                # Now safe to delete the previously generated documents from S3 (the
                # catalog pointing at them was just cleared, so no reader follows a
                # dangling reference). A losing request never reaches here.
                if prior_docs:
                    bucket = self.kb_manager._get_s3_bucket()
                    for doc in prior_docs:
                        s3_path = doc.get("s3_path")
                        if not s3_path:
                            continue
                        try:
                            self.kb_manager.s3_client.delete_object(Bucket=bucket, Key=s3_path)
                        except Exception:
                            logger.debug(
                                f"Failed to delete prior overview doc {s3_path}", exc_info=True
                            )

                slot.submit(self._run_space_overview, space_id)

        return {
            "space_id": space_id,
            "status": "SUBMITTED",
            "message": "Space Overview generation started. Poll GET /api/spaces/{id}/status for progress.",
        }

    def _finalize_pipeline(
        self,
        space_id: str,
        ctx: AnalysisContext,
        summarize: bool,
        summary_stats: dict[str, Any] | None,
        source_metadata: dict[str, Any],
    ) -> None:
        """Write the final status after all pipeline steps complete."""
        saving_status = "SUMMARIZING" if summarize else "ANALYZING"
        self._update_pipeline_status(space_id, saving_status, "save", "in_progress")
        kb = ctx.knowledge_base
        stats = ctx.statistics
        self._update_pipeline_status(space_id, saving_status, "save", "completed")

        pipeline_steps = (self._read_status(space_id) or {}).get("pipeline_steps", [])
        any_failed = any(s.get("status") == "failed" for s in pipeline_steps)
        final_status = "FAILED" if any_failed else "READY"
        failed_steps = [s.get("step") for s in pipeline_steps if s.get("status") == "failed"]

        component_count = ctx.knowledge_base.get_component_count()
        languages = sorted({c.language for c in ctx.knowledge_base.components})

        final_data: dict[str, Any] = {
            "space_id": space_id,
            "status": final_status,
            "component_count": component_count,
            "languages": languages,
            "source_directory": str(kb.source_directory),
            "version": kb.version,
            # Id scheme this space was indexed under (V2 for new spaces, empty
            # for legacy). Lets the catalog distinguish them without a full KB
            # import; refresh still reads the authoritative value off the KB.
            "id_scheme": getattr(kb, "id_scheme", ""),
            "created_at": kb.created_at,
            "statistics": stats,
            # The exclusion patterns this space was created with; refresh applies
            # them for the life of the space.
            "exclude_patterns": list(require_config("analysis.exclude_patterns")),
            "vector_count": summary_stats["total_analyzed"] if summary_stats else 0,
            "pipeline_steps": pipeline_steps,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **source_metadata,
        }
        # Preserve fields from intermediate status that aren't rebuilt here
        existing_status = self._read_status(space_id) or {}
        if "space_name" not in final_data and existing_status.get("space_name"):
            final_data["space_name"] = existing_status["space_name"]
        if existing_status.get("created_by"):
            final_data["created_by"] = existing_status["created_by"]
        if existing_status.get("visibility"):
            final_data["visibility"] = existing_status["visibility"]
        # Preserve the authz fields written at create (the rebuild would drop them).
        for _authz_field in ("provider", "repo_key", "created_by_sub", "admitted_by"):
            if existing_status.get(_authz_field) is not None:
                final_data[_authz_field] = existing_status[_authz_field]
        if "omit_low_business_relevance" in existing_status:
            final_data["omit_low_business_relevance"] = existing_status[
                "omit_low_business_relevance"
            ]
        if existing_status.get("space_overview_docs"):
            final_data["space_overview_docs"] = existing_status["space_overview_docs"]
        business_context = existing_status.get("business_context") or (
            kb.analysis_metadata.get("business_context")
            if isinstance(kb.analysis_metadata, dict)
            else None
        )
        if business_context and business_context.get("error") != "disabled":
            final_data["business_context"] = business_context

        # Bedrock token consumption for this analysis run. Stored under a
        # per-operation token_usage map so later operations (overview, refresh)
        # add their own keys without clobbering this one.
        token_usage = existing_status.get("token_usage") or {}
        creation_usage = self._creation_usage(summary_stats, kb)
        if creation_usage:
            creation_usage["generated_at"] = datetime.now(timezone.utc).isoformat()
            token_usage["space_creation"] = creation_usage
        if token_usage:
            final_data["token_usage"] = token_usage

        # Space-level model identity (captured once; uniform across operations).
        final_data["bedrock_model"] = self._model_metadata(
            summary_stats.get("region") if summary_stats else None
        )

        if any_failed:
            final_data["error"] = f"Pipeline step(s) failed: {', '.join(failed_steps)}"
            # Append only the curated remedy (never raw-str(e) summarize_error).
            if existing_status.get("summarize_remedy"):
                final_data["error"] += f" — {existing_status['summarize_remedy']}"

        self._write_status(space_id, final_data)

        # Drop any context cached *during* this run: the KB in S3 has just been
        # replaced, so a cached AnalysisContext built from the previous one is stale.
        self.invalidate_context(space_id)

    def start_analysis(
        self,
        git_url: str,
        branch: str | None = None,
        summarize: bool = False,
        omit_low_business_relevance: bool = False,
        space_name: str | None = None,
        created_by: str | None = None,
        actor_sub: str | None = None,
        visibility: str | None = None,
    ) -> dict[str, Any]:
        """Create a new space from a Git repository and start analysis.

        Returns immediately with SUBMITTED status. ``visibility`` may be supplied
        by the create gate to skip a second provider call; else looked up here.

        Raises ServiceError (before any status write) if the URL scheme is not
        allowed or the saved git token is rejected.
        """
        git_url = validate_git_url(git_url)
        space_id = self._generate_unique_space_id(git_url, from_git_url=True)
        logger.info(f"Starting analysis: space={space_id}, repo={git_url}, branch={branch}")

        # Record visibility before any status write; a rejected token raises here.
        if visibility is None:
            visibility = lookup_repo_visibility(git_url, actor_sub)
        logger.info(f"Repo visibility: space={space_id}, visibility={visibility}")

        # Write initial status to S3
        initial_status: dict[str, Any] = {
            "space_id": space_id,
            "status": "ANALYZING",
            "git_url": git_url,
            "branch": branch,
            "summarize": summarize,
            "omit_low_business_relevance": omit_low_business_relevance,
            "visibility": visibility,
            "provider": get_provider(git_url),
            "repo_key": repo_key(git_url),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        if space_name:
            initial_status["space_name"] = space_name
        if created_by:
            initial_status["created_by"] = created_by
            initial_status["admitted_by"] = created_by
        if actor_sub:
            initial_status["created_by_sub"] = actor_sub

        source_dir = self._analysis_work_path(space_id)
        # Reserve before the first status write: a 429 must not leave an
        # ANALYZING space behind.
        with pipeline_pool().reserve("analysis", space_id) as slot:
            self._write_status(space_id, initial_status)
            undo = lambda: self._write_status(  # noqa: E731
                space_id,
                {
                    **initial_status,
                    "status": "FAILED",
                    "error": self._JOB_START_FAILED,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            with self._undo_unless_submitted(slot, undo):
                slot.submit(
                    self._run_pipeline,
                    space_id,
                    source_dir,
                    summarize,
                    omit_low_business_relevance,
                    lambda: self._acquire_source_git(
                        space_id, source_dir, git_url, branch, actor_sub=actor_sub
                    ),
                )

        return {
            "space_id": space_id,
            "git_url": redact_git_url(git_url),
            "branch": branch,
            "status": "SUBMITTED",
            "message": "Analysis started. Poll GET /api/spaces/{id}/status for progress.",
        }

    # ── retry ──────────────────────────────────────────────────────────

    # Left behind by the run being retried; the new run writes its own.
    _RETRY_DROPPED_FIELDS = ("error", "failed_at", "warning", "pipeline_steps", "current_step")

    @staticmethod
    def _retry_refusal(status: dict[str, Any]) -> str | None:
        """Why this space cannot be retried, or None when it can.

        A FAILED analysis can. So can one still marked in progress whose
        heartbeat is older than ``summarization.warn_after_stale_s``: its worker
        is taken to be gone (for example its task was replaced), without waiting
        for ``fail_after_stale_s`` to mark it FAILED.
        """
        if not status.get("git_url") or not status.get("git_commit_hash"):
            return "it has no stored Git source to resume. Start a new analysis instead."
        state = status.get("status")
        if state == "FAILED":
            return None
        if state in ("ANALYZING", "SUMMARIZING"):
            warn_after = int(require_config("summarization.warn_after_stale_s"))
            try:
                age_s = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(status["updated_at"])
                ).total_seconds()
            except (KeyError, ValueError, TypeError):
                age_s = 0
            if warn_after > 0 and age_s > warn_after:
                return None
            return "its analysis is still running."
        return f"it is {state}. Only a failed or stalled analysis can be retried."

    def retry_space(self, space_id: str) -> dict[str, Any]:
        """Re-run a failed or stalled Git analysis under the same space ID.

        The source is the archive the earlier run stored, so the commit does not
        change, and functions that already have a description are not sent to
        the model again.

        Raises:
            SpaceNotFoundError: No such space.
            ServiceError: The space cannot be retried, or another request
                claimed it first (409).
            PipelineCapacityError: Every pipeline slot is in use.
        """
        with pipeline_pool().reserve("analysis", space_id) as slot:
            prior, etag = self._read_status_with_etag(space_id)
            if prior is None:
                raise SpaceNotFoundError(space_id)
            refusal = self._retry_refusal(prior)
            if refusal:
                raise ServiceError(
                    f"Space '{space_id}' cannot be retried: {refusal}", status_code=409
                )
            now = datetime.now(timezone.utc).isoformat()
            status = {k: v for k, v in prior.items() if k not in self._RETRY_DROPPED_FIELDS}
            status.update(status="ANALYZING", retried_at=now, updated_at=now)
            try:
                # Conditional on the status that was read, so of several retries
                # (from any process) only one starts a job.
                self._write_status(space_id, status, if_match=etag)
            except ClientError as e:
                if self._is_precondition_failed(e):
                    raise ServiceError(
                        f"Space '{space_id}' is busy: another operation started first.",
                        status_code=409,
                    ) from e
                raise
            source_dir = self._analysis_work_path(space_id)
            # Statuses written before the flag was recorded: summarization was on
            # if the earlier run reached that step.
            summarize = prior.get(
                "summarize",
                any(s.get("step") == "summarize" for s in prior.get("pipeline_steps", [])),
            )
            with self._undo_unless_submitted(slot, lambda: self._write_status(space_id, prior)):
                slot.submit(
                    self._run_pipeline,
                    space_id,
                    source_dir,
                    bool(summarize),
                    bool(prior.get("omit_low_business_relevance")),
                    lambda: self._acquire_source_stored(space_id, source_dir, prior),
                )

        return {
            "space_id": space_id,
            "status": "SUBMITTED",
            "message": "Retry started. Poll GET /api/spaces/{id}/status for progress.",
        }

    def _acquire_source_stored(
        self, space_id: str, source_dir: Path, prior: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Acquire source from the archive an earlier run of this space stored.

        Returns source-specific metadata for the final status, or None on failure.
        """
        self._update_pipeline_status(space_id, "ANALYZING", "clone", "in_progress")
        try:
            # symlinks=True copies a link as a link, so nothing outside the stored
            # tree is ever read into the working tree.
            shutil.copytree(
                self.source_provider.source_root(space_id),
                source_dir,
                symlinks=True,
                dirs_exist_ok=True,
            )
        except Exception as e:
            logger.error(f"[worker] Stored source unavailable for {space_id}: {e}")
            self._update_pipeline_status(
                space_id,
                "FAILED",
                "clone",
                "failed",
                extra={
                    "error": "The stored source of this space could not be read. "
                    "Start a new analysis instead."
                },
            )
            return None
        self._update_pipeline_status(space_id, "ANALYZING", "clone", "completed")
        return {name: prior.get(name) for name in ("git_url", "git_commit_hash", "branch")}

    # ── space refresh ──────────────────────────────────────────────────

    def refresh_space(
        self,
        space_id: str,
        actor: str | None = None,
        actor_sub: str | None = None,
    ) -> dict[str, Any]:
        """Trigger a Space Refresh for an existing git-sourced Space.

        Reads prior status.json to validate that the Space exists, is READY or
        REFRESH_FAILED, and is git-sourced.
        Raises SpaceNotFoundError if no status.json exists.
        Raises ServiceError(status_code=409) if current status is neither, or if
        another request changed status.json between the read and the REFRESHING write.
        Raises ServiceError(status_code=400) if prior status lacks a git_url (upload-sourced).
        Writes an initial status.json with status=REFRESHING, current_step="clone",
        refresh_started_at=<now>, refresh_actor=<actor>.
        Dispatches _run_refresh_pipeline on the pipeline pool; raises
        PipelineCapacityError (429), before any status write, if the pool is full.
        Returns immediately with {"space_id": ..., "status": "SUBMITTED", "message": ...}.
        """
        # Read and validate prior status
        # The ETag lets the REFRESHING write below fail if another request wrote first.
        prior_status, etag = self._read_status_with_etag(space_id)
        if prior_status is None:
            raise SpaceNotFoundError(space_id)

        current_state = prior_status.get("status") or "UNKNOWN"
        # A failed refresh never advanced git_commit_hash, so a retry repeats its diff.
        if current_state not in ("READY", "REFRESH_FAILED"):
            if current_state in ("ANALYZING", "SUMMARIZING", "REFRESHING"):
                raise ServiceError(
                    f"Space '{space_id}' is busy (status: {current_state}). "
                    "Wait for the current operation to finish before refreshing.",
                    status_code=409,
                )
            raise ServiceError(
                f"Space '{space_id}' is not ready (status: {current_state}). "
                "Space refresh requires a successfully analyzed Space.",
                status_code=409,
            )
        # Overview generation keeps the Space READY, and its worker rewrites status.json without a lock.
        if self._overview_run_is_fresh(prior_status):
            raise ServiceError(
                f"Space '{space_id}' is generating its Space Overview. "
                "Wait for it to finish before refreshing.",
                status_code=409,
            )

        git_url = prior_status.get("git_url")
        if not git_url:
            raise ServiceError(
                f"Space '{space_id}' is upload-sourced. "
                f"Space refresh supports git-sourced Spaces only in this release.",
                status_code=400,
            )

        logger.info(
            f"Starting space refresh: space={space_id}, "
            f"prior_commit={prior_status.get('git_commit_hash', 'unknown')}"
        )

        # Write initial REFRESHING status
        now = datetime.now(timezone.utc).isoformat()
        # Append a fresh refresh run to the history. The analysis pipeline_steps
        # and all prior refresh runs are preserved so the UI can show the full
        # timeline: initial space creation + space refresh #1..#N.
        refresh_runs = list(prior_status.get("refresh_pipeline_runs", []))
        refresh_runs.append({"started_at": now, "steps": []})
        initial_status: dict[str, Any] = {
            **prior_status,
            "status": "REFRESHING",
            "current_step": "clone",
            "refresh_pipeline_runs": refresh_runs,
            "refresh_started_at": now,
            "updated_at": now,
        }
        # Drop the legacy single-run field if a prior (old-code) refresh left it.
        initial_status.pop("refresh_pipeline_steps", None)
        # A retry clears the failed run's error; this run writes its own on failure.
        initial_status.pop("error", None)
        if actor:
            initial_status["refresh_actor"] = actor

        # Reserve before the REFRESHING write: a 429 must leave the space READY.
        with pipeline_pool().reserve("refresh", space_id) as slot:
            # Resolve the refreshing user's git credential for this repo's host (if
            # any) and hand the ready-built subprocess env to the worker. Built here,
            # while we still hold the caller's sub, and before the REFRESHING write
            # so the only step between that write and the submit is the submit.
            git_env = build_git_env(git_url, actor_sub)
            try:
                self._write_status(space_id, initial_status, if_match=etag)
            except ClientError as e:
                # Another request changed status.json after our read, e.g. a second refresh won.
                if self._is_precondition_failed(e):
                    raise ServiceError(
                        f"Space '{space_id}' is busy: another operation started first. "
                        "Wait for it to finish before refreshing.",
                        status_code=409,
                    ) from e
                raise
            # Nothing has run yet, so a refresh that cannot start puts the READY
            # record back, as a 429 leaves it.
            with self._undo_unless_submitted(
                slot, lambda: self._write_status(space_id, prior_status)
            ):
                slot.submit(self._run_refresh_pipeline, space_id, prior_status, git_env=git_env)

        return {
            "space_id": space_id,
            "status": "SUBMITTED",
            "message": "Space refresh started. Poll GET /api/spaces/{id}/status for progress.",
        }

    def _run_refresh_pipeline(
        self,
        space_id: str,
        prior_status: dict[str, Any],
        git_env: dict[str, str] | None = None,
    ) -> None:
        """Background worker: construct RefreshOrchestrator and run the pipeline.

        ``git_env`` carries the refreshing user's host-scoped git credential (or
        the credential-free base env), resolved by ``refresh_space``.

        On uncaught exception, writes REFRESH_FAILED status and logs the error.
        """
        from ..analysis.refresh_orchestrator import RefreshOrchestrator
        from ..vector.s3_vectors_client import S3VectorsClient

        try:
            vectors_client = S3VectorsClient()
            orchestrator = RefreshOrchestrator(
                space_id=space_id,
                prior_status=prior_status,
                kb_manager=self.kb_manager,
                vectors_client=vectors_client,
                codekb_service=self,
                git_env=git_env,
            )
            result = orchestrator.run()

            # run() catches RefreshFailed internally and returns a RefreshResult
            # with final_error_step set rather than raising, so a hard pipeline
            # failure surfaces here rather than in the except block below. The
            # terminal status.json (READY, or REFRESH_FAILED) is written by run() itself.
            if result is not None and result.final_error_step:
                logger.error(
                    f"[refresh] Pipeline failed for {space_id} at step "
                    f"'{result.final_error_step}' ({result.final_error_reason})"
                )

        except Exception as e:
            logger.error(f"[refresh] Pipeline failed for {space_id}: {e}", exc_info=True)
            existing = self._read_status(space_id) or {}
            now_iso = datetime.now(timezone.utc).isoformat()
            # Not FAILED: git_commit_hash still names the prior commit, so a retry
            # repeats the same diff.
            failed_status: dict[str, Any] = {
                **existing,
                "space_id": space_id,
                "status": "REFRESH_FAILED",
                "error": f"[refresh] unexpected error: {e}",
                "updated_at": now_iso,
            }
            self._fail_in_progress_refresh_steps(failed_status, now_iso)
            self._write_status(space_id, failed_status)

    def _fail_in_progress_refresh_steps(self, status: dict[str, Any], now_iso: str) -> None:
        """Mark the latest refresh run's in-progress steps failed; otherwise they spin forever."""
        runs = status.get("refresh_pipeline_runs") or []
        for step in runs[-1].get("steps", []) if runs else []:
            if step.get("status") == "in_progress":
                step["status"] = "failed"
                step["updated_at"] = now_iso

    # ── zip upload ──────────────────────────────────────────────────────

    def _upload_s3_key(self, space_id: str) -> str:
        """S3 key for the temporary upload zip."""
        return f"uploads/{space_id}/source.zip"

    def _max_upload_bytes(self) -> int:
        """Maximum accepted upload-zip size in bytes (from config).

        Fails closed: the cap MUST be configured (it ships in default.yaml);
        a missing value is a misconfiguration, not a reason to silently assume
        a limit. Guards CWE-770 -- the download path reads the whole zip into
        memory, so an oversized object could exhaust worker memory. A
        non-positive configured value disables the check.
        """
        mb = get_config("storage.s3.max_upload_size_mb")
        if mb is None:
            raise RuntimeError(
                "storage.s3.max_upload_size_mb is not configured; refusing to "
                "process uploads without an enforced size cap"
            )
        return int(mb) * 1024 * 1024

    def _max_extract_bytes(self) -> int:
        """Max decompressed size allowed for an uploaded source zip.

        Derived from the compressed-upload cap so raising/lowering the upload
        limit scales this in lock-step. Bounds decompression expansion to guard
        against zip bombs. A non-positive value disables the check.
        """
        return self._max_upload_bytes() * self._MAX_DECOMPRESSION_RATIO

    def _generate_unique_space_id(
        self, raw: str, from_git_url: bool = False, max_attempts: int = 5
    ) -> str:
        """Generate a space ID that doesn't collide with an existing space.

        Retries with a fresh random suffix up to max_attempts times.
        """
        id_fn = generate_space_id if from_git_url else generate_space_id_from_name
        for _ in range(max_attempts):
            candidate = id_fn(raw)
            if self._read_status(candidate) is None:
                return candidate
        raise ServiceError(
            f"Could not generate a unique space ID for '{raw}' after {max_attempts} attempts"
        )

    def generate_upload_url(
        self,
        space_name: str,
        summarize: bool = False,
        omit_low_business_relevance: bool = False,
        created_by: str | None = None,
        actor_sub: str | None = None,
    ) -> dict[str, Any]:
        """Generate a presigned S3 URL for uploading a source zip.

        Returns the space_id, presigned URL, and the S3 key so the frontend
        can PUT the zip directly to S3, then call start_upload_analysis.
        """
        space_id = self._generate_unique_space_id(space_name)
        bucket = self.kb_manager._get_s3_bucket()
        key = self._upload_s3_key(space_id)

        url = self.kb_manager.s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                "ContentType": "application/zip",
            },
            ExpiresIn=3600,  # 1 hour
        )
        logger.info(
            f"Generated presigned PUT URL for s3://{bucket}/{key} (expires in 3600s, "
            f"space_id={space_id})"
        )

        # Write a preliminary status so created_by is captured early
        status_data: dict[str, Any] = {
            "space_id": space_id,
            "status": "PENDING_UPLOAD",
            "source_type": "upload",
            "space_name": space_name,
            "omit_low_business_relevance": omit_low_business_relevance,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        if created_by:
            status_data["created_by"] = created_by
        if actor_sub:
            status_data["created_by_sub"] = actor_sub
        self._write_status(space_id, status_data)

        return {
            "space_id": space_id,
            "upload_url": url,
            "s3_key": key,
            "expires_in": 3600,
        }

    def start_upload_analysis(
        self,
        space_id: str,
        space_name: str,
        summarize: bool = False,
        omit_low_business_relevance: bool = False,
        created_by: str | None = None,
        actor_sub: str | None = None,
    ) -> dict[str, Any]:
        """Start analysis of an uploaded zip file.

        The zip must already be uploaded to S3 via the presigned URL
        from generate_upload_url().

        Returns immediately with SUBMITTED status.
        """
        logger.info(f"Starting upload analysis: space={space_id}, name={space_name}")

        # Authz gate for the upload route, which accepts an arbitrary space_id.
        # Only the space's creator may drive its upload, and only into the fresh
        # PENDING_UPLOAD slot that generate_upload_url created. It holds
        # regardless of the authz feature switch, and it keeps a later signed-in
        # caller from taking over an existing space's owner key.
        existing_status = self._read_status(space_id) or {}
        existing_owner_sub = existing_status.get("created_by_sub")
        if existing_owner_sub and actor_sub and existing_owner_sub != actor_sub:
            # Non-owner: 404 so we never confirm the space exists (matches the
            # ReadDenied posture in the authz gate).
            raise ServiceError("You do not have access to this space.", status_code=404)
        if existing_status and existing_status.get("status") != "PENDING_UPLOAD":
            # Owner-only reaches here (a non-owner was already rejected above).
            # Re-upload into an existing space is not a supported flow, so return
            # an honest conflict rather than a misleading "no access".
            raise ServiceError(
                f"Space '{space_id}' already exists and cannot be re-uploaded.",
                status_code=409,
            )

        # Verify the upload exists
        bucket = self.kb_manager._get_s3_bucket()
        upload_key = self._upload_s3_key(space_id)
        logger.debug(f"s3.head_object: s3://{bucket}/{upload_key}")
        try:
            head = self.kb_manager.s3_client.head_object(Bucket=bucket, Key=upload_key)
        except Exception as e:
            logger.warning(f"s3.head_object failed for upload s3://{bucket}/{upload_key}: {e}")
            raise ServiceError(f"Upload not found for space {space_id}. Upload the zip first.")

        # Reject oversized uploads before spawning the worker.
        max_upload_bytes = self._max_upload_bytes()
        content_length = head.get("ContentLength")
        if (
            max_upload_bytes > 0
            and content_length is not None
            and content_length > max_upload_bytes
        ):
            logger.warning(
                f"Rejected oversized upload for space '{space_id}': "
                f"{content_length:,} bytes > limit {max_upload_bytes:,} bytes"
            )
            # Remove the oversized object so it can't be re-submitted or linger.
            try:
                self.kb_manager.s3_client.delete_object(Bucket=bucket, Key=upload_key)
            except Exception as e:
                logger.warning(f"Failed to delete oversized upload s3://{bucket}/{upload_key}: {e}")
            limit_mb = max_upload_bytes // (1024 * 1024)
            actual_mb = content_length / (1024 * 1024)
            self._write_status(
                space_id,
                {
                    "space_id": space_id,
                    "status": "FAILED",
                    "source_type": "upload",
                    "space_name": space_name,
                    "error": (
                        f"Upload is too large ({actual_mb:.1f} MB). "
                        f"Maximum allowed is {limit_mb} MB."
                    ),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            raise ServiceError(
                f"Upload is too large ({actual_mb:.1f} MB). Maximum allowed is {limit_mb} MB."
            )

        # Write initial status
        status_data: dict[str, Any] = {
            "space_id": space_id,
            "status": "ANALYZING",
            "source_type": "upload",
            "space_name": space_name,
            "omit_low_business_relevance": omit_low_business_relevance,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        # The owner key is write-once: the PENDING_UPLOAD record written by
        # generate_upload_url wins, so a later caller can never reassign it.
        # created_by_sub is the zip space's owner key for authz.
        effective_created_by = existing_status.get("created_by") or created_by
        if effective_created_by:
            status_data["created_by"] = effective_created_by
        effective_created_by_sub = existing_status.get("created_by_sub") or actor_sub
        if effective_created_by_sub:
            status_data["created_by_sub"] = effective_created_by_sub

        source_dir = self._analysis_work_path(space_id)
        # Reserve before the ANALYZING write: on a 429 the status record and the
        # uploaded zip are untouched, so the same upload can be started again.
        with pipeline_pool().reserve("upload_analysis", space_id) as slot:
            self._write_status(space_id, status_data)
            # Nothing has run yet: put the PENDING_UPLOAD record back so the same
            # upload can be started again, as after a 429. With no record to put
            # back, end FAILED instead.
            undo = lambda: self._write_status(  # noqa: E731
                space_id,
                existing_status
                or {
                    **status_data,
                    "status": "FAILED",
                    "error": self._JOB_START_FAILED,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            with self._undo_unless_submitted(slot, undo):
                slot.submit(
                    self._run_pipeline,
                    space_id,
                    source_dir,
                    summarize,
                    omit_low_business_relevance,
                    lambda: self._acquire_source_upload(space_id, source_dir, space_name),
                )

        return {
            "space_id": space_id,
            "source_type": "upload",
            "status": "SUBMITTED",
            "message": "Analysis started. Poll GET /api/spaces/{id}/status for progress.",
        }

    def get_analysis_status(self, space_id: str) -> dict[str, Any]:
        """Get analysis status by reading status.json from S3.

        Includes stale in-progress detection: if the status is ANALYZING,
        SUMMARIZING, or REFRESHING and updated_at is older than 1800s,
        the status is rewritten as FAILED (REFRESH_FAILED for a refresh).
        """
        status = self._read_status(space_id)
        if status:
            # Surface the AWS Bedrock pricing link alongside token usage so the
            # UI can show "see pricing to estimate cost" — we never compute $.
            if status.get("token_usage"):
                from ..core.token_usage import pricing_url

                status.setdefault("pricing_url", pricing_url())
            # Stale in-progress detection — two-tier and age-based. Thresholds
            # are config-driven (summarization.warn_after_stale_s /
            # fail_after_stale_s in default.yaml); age uses the updated_at
            # heartbeat, which the summarize on_progress callback refreshes every
            # ~15s while the worker is making progress.
            current_state = status.get("status", "")
            if current_state in ("ANALYZING", "SUMMARIZING", "REFRESHING"):
                updated_at = status.get("updated_at")
                if updated_at:
                    try:
                        updated_dt = datetime.fromisoformat(updated_at)
                        age_s = (datetime.now(timezone.utc) - updated_dt).total_seconds()
                        warn_after = int(require_config("summarization.warn_after_stale_s"))
                        fail_after = int(require_config("summarization.fail_after_stale_s"))
                        current_step = status.get("current_step", "unknown")

                        if fail_after > 0 and age_s > fail_after:
                            # Hard flip (FAILED; a stalled refresh goes to REFRESH_FAILED).
                            # Because updated_at is a ~15s heartbeat while the worker
                            # progresses, an age past this (12h default) means the run
                            # is genuinely dead/wedged — e.g. its ECS task was recycled
                            # mid step with no checkpoint/resume — not merely slow.
                            # Persist so the space stops being reported
                            # in-progress forever and can be retried.
                            now_iso = datetime.now(timezone.utc).isoformat()
                            stalled = (
                                f"step '{current_step}' made no progress for {age_s:.0f}s "
                                f"(limit {fail_after}s). The worker was interrupted or stalled."
                            )
                            if current_state == "REFRESHING":
                                # git_commit_hash is still the prior commit, so a retry
                                # repeats the same diff.
                                status["status"] = "REFRESH_FAILED"
                                status["error"] = f"Refresh failed: {stalled}"
                                self._fail_in_progress_refresh_steps(status, now_iso)
                            else:
                                status["status"] = "FAILED"
                                status["error"] = f"Analysis failed: {stalled} Please retry."
                                status["failed_at"] = now_iso
                            status["updated_at"] = now_iso
                            status.pop("warning", None)
                            self._write_status(space_id, status)
                            logger.error(
                                f"Stale in-progress flipped to {status['status']} for {space_id}: "
                                f"status={current_state}, step={current_step}, "
                                f"age={age_s:.0f}s (limit {fail_after}s)"
                            )
                        elif warn_after > 0 and age_s > warn_after:
                            # Soft warning only — the background worker is likely
                            # still running and will complete. Keep the current
                            # status so the UI shows "taking longer than expected"
                            # rather than a false failure that self-resolves.
                            done = status.get("summarize_done")
                            total = status.get("summarize_total")
                            progress_detail = (
                                f" ({done}/{total})"
                                if done is not None and total is not None
                                else ""
                            )
                            status["warning"] = (
                                f"Taking longer than usual — step "
                                f"'{current_step}' is still running"
                                f"{progress_detail}. Large repositories "
                                f"can take a while. No action needed."
                            )
                            logger.warning(
                                f"Stale in-progress detected for {space_id}: "
                                f"status={current_state}, step={current_step}, "
                                f"age={age_s:.0f}s"
                            )
                    except (ValueError, TypeError):
                        pass  # Malformed timestamp — skip detection
            # TODO Temporary: Redact credentials from git_url before returning to callers
            if "git_url" in status:
                status["git_url"] = redact_git_url(status["git_url"])
            return status
        # No status file — check if KB exists (legacy space)
        try:
            ctx = self._load_context(space_id)
            return {
                "space_id": space_id,
                "status": "READY",
                "component_count": ctx.knowledge_base.get_component_count(),
            }
        except SpaceNotFoundError:
            return {
                "space_id": space_id,
                "status": "NOT_FOUND",
            }
        except Exception as e:
            return {
                "space_id": space_id,
                "status": "ERROR",
                "error": str(e),
            }

    # ── global metrics ────────────────────────────────────────────────────

    def get_global_metrics(
        self, spaces: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Aggregate metrics across a set of spaces and the vector store.

        ``spaces`` scopes the aggregation. The route passes the caller's
        readable spaces when authorization is on, so a tenant's metrics never
        count spaces they cannot see. When ``None`` (authorization off), this
        falls back to every space.
        """
        if spaces is None:
            spaces = self.list_spaces()
        total_spaces = len(spaces)
        ready_spaces = sum(1 for p in spaces if p.get("status") == "READY")
        failed_spaces = sum(
            1 for p in spaces if p.get("status") in ("FAILED", "REFRESH_FAILED")
        )
        in_progress_spaces = sum(
            1 for p in spaces if p.get("status") in ("ANALYZING", "SUBMITTED", "SUMMARIZING")
        )
        total_components = sum(p.get("component_count") or 0 for p in spaces)
        total_lines = sum(p.get("total_lines") or 0 for p in spaces)

        # Collect all languages across spaces
        all_languages: set[str] = set()
        for p in spaces:
            for lang in p.get("languages") or []:
                all_languages.add(lang)

        # Vector count is persisted per-space in status.json at summarize
        # time — sum those instead of walking the entire S3 Vectors index,
        # which is O(total_vectors) and dominated /metrics latency.
        total_vectors = sum(p.get("vector_count") or 0 for p in spaces)

        return {
            "total_spaces": total_spaces,
            "ready_spaces": ready_spaces,
            "failed_spaces": failed_spaces,
            "in_progress_spaces": in_progress_spaces,
            "total_components": total_components,
            "total_lines_of_code": total_lines,
            "total_vectors": total_vectors,
            "languages": sorted(all_languages),
            "language_count": len(all_languages),
        }

    # ── raw knowledge base ──────────────────────────────────────────────

    def get_knowledge_base_raw(self, space_id: str) -> dict[str, Any]:
        """Return the raw knowledge_base.json content without deserializing into objects."""
        bucket_name = self.kb_manager._get_s3_bucket()
        key = self.kb_manager._get_s3_key(space_id)
        try:
            resp = self.kb_manager.s3_client.get_object(Bucket=bucket_name, Key=key)
            return json.loads(resp["Body"].read().decode("utf-8"))
        except Exception:
            raise SpaceNotFoundError(space_id)

    def _vector_keys_for_space(self, space_id: str) -> list[str]:
        """Reconstruct a space's vector keys from its knowledge_base.json."""
        if not self.kb_manager._exists(space_id):
            logger.info(
                f"No knowledge_base.json for '{space_id}' (no committed KB) — "
                f"zero vector keys; delete proceeds."
            )
            return []
        kb = self.get_knowledge_base_raw(space_id)
        prefix = f"{space_id}::"
        keys: list[str] = []
        for component in kb["components"]:
            component_id = component["id"]
            if not component_id.startswith(prefix):
                logger.error(
                    f"Vector key reconstruction for '{space_id}': component id "
                    f"'{component_id}' does not start with the '{prefix}' prefix "
                    f"— SKIPPING (cross-tenant guard; not added to the delete list)."
                )
                continue
            keys.append(component_id)
        return keys

    # ── code querying ───────────────────────────────────────────────────

    def find_component(self, space_id: str, name: str) -> dict[str, Any] | None:
        """Find a component by exact name."""
        ctx = self._load_context(space_id)
        comp = ctx.catalog.find_component(name)
        if not comp:
            return None
        result = comp.to_dict()
        source_code = self._read_component_source(space_id, ctx, comp)
        if source_code is not None:
            result["source_code"] = source_code
        return result

    def _read_component_source(self, space_id: str, ctx: AnalysisContext, comp) -> str | None:
        """Read the exact source range for a component when source is available."""
        component_path = Path(comp.file_path)
        source_dir = Path(ctx.knowledge_base.source_directory)
        candidate_paths = [component_path]
        if not component_path.is_absolute():
            candidate_paths.append(source_dir / component_path)

        seen_paths: set[Path] = set()
        for path in candidate_paths:
            if path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                if not path.is_file():
                    continue
                lines = path.read_text(errors="replace").splitlines()
                start = max(comp.line_number - 1, 0)
                end = min(comp.end_line_number, len(lines))
                return "\n".join(lines[start:end])
            except Exception as e:
                logger.debug(f"Could not read component source from {path}: {e}")

        try:
            rel_path = (
                component_path.resolve().relative_to(source_dir.resolve())
                if component_path.is_absolute()
                else component_path
            )
            # read_text hydrates and, if the tree is evicted between hydrating and
            # opening, re-hydrates and retries once — so disk pressure elsewhere
            # degrades this into a slower read rather than a missing snippet.
            lines = self.source_provider.read_text(space_id, str(rel_path)).splitlines()
            start = max(comp.line_number - 1, 0)
            end = min(comp.end_line_number, len(lines))
            return "\n".join(lines[start:end])
        except Exception as e:
            logger.debug(f"Could not read cached component source for {space_id}: {e}")
        return None

    def get_vector_description(self, space_id: str, component_id: str) -> str | None:
        """Fetch the AI-generated description from the vector store for a component."""
        try:
            from ..vector.s3_vectors_client import S3VectorsClient

            client = S3VectorsClient()
            vector = client.get_vector(component_id)
            if vector:
                return vector.get("metadata", {}).get("description")
            return None
        except Exception as e:
            logger.warning(f"Failed to fetch vector description for '{component_id}': {e}")
            return None

    def _component_relevance_metadata(self, space_id: str, component_id: str) -> dict[str, Any]:
        """Return persisted business relevance metadata for a component."""
        try:
            ctx = self._load_context(space_id)
            comp = ctx.catalog.find_component(component_id)
            if not comp:
                return {}
            behavior = comp.business_behavior
            return {
                "business_relevance_score": behavior.business_relevance_score,
                "business_relevance_tier": behavior.business_relevance_tier,
                "business_relevance_reasons": behavior.business_relevance_reasons,
                "business_relevance_signals": behavior.business_relevance_signals,
                "business_relevance_contributions": behavior.business_relevance_contributions,
            }
        except Exception as e:
            logger.debug(
                f"Could not load relevance metadata for '{component_id}' in '{space_id}': {e}"
            )
            return {}

    def _enrich_search_results_with_relevance(
        self, results: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Add catalog relevance metadata to vector search results when available."""
        for result in results:
            metadata = result.setdefault("metadata", {})
            if metadata.get("business_relevance_score") is not None:
                continue
            space_id = metadata.get("space_id")
            component_id = metadata.get("component_id") or result.get("key")
            if not space_id or not component_id:
                continue
            metadata.update(self._component_relevance_metadata(space_id, component_id))
        return results

    def search_components(
        self, space_id: str, pattern: str, component_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Search components by wildcard pattern, optionally filtered by type.

        Args:
            space_id: Space to search.
            pattern: Wildcard name pattern (e.g. ``User*``).
            component_type: Restrict results to this type (function, class,
                method, variable, ...). Unknown types yield no results, matching
                ``list_components`` behaviour.
        """
        from ..core.models import ComponentType

        ctx = self._load_context(space_id)
        results = ctx.catalog.search_components(pattern)
        if component_type:
            try:
                wanted = ComponentType(component_type.lower())
            except ValueError:
                logger.warning(f"Invalid component type: {component_type}")
                return []
            results = [c for c in results if c.type == wanted]
        return [c.to_dict() for c in results]

    def list_components(
        self, space_id: str, component_type: str | None = None
    ) -> list[dict[str, Any]]:
        """List all components, optionally filtered by type."""
        ctx = self._load_context(space_id)
        if component_type:
            comps = ctx.catalog.get_components_by_type(component_type)
        else:
            comps = ctx.catalog.get_all_components()
        return [c.to_dict() for c in comps]

    def get_stats(self, space_id: str) -> dict[str, Any]:
        """Get codebase statistics for a space."""
        ctx = self._load_context(space_id)
        return ctx.statistics

    # ── flow analysis ───────────────────────────────────────────────────

    def trace_flow(self, space_id: str, component: str, depth: int = 10) -> dict[str, Any] | None:
        """Trace execution flow from a component."""
        ctx = self._load_context(space_id)
        comp = ctx.catalog.find_component(component)
        if not comp:
            return None
        flow = ctx.flow_analyzer.trace_execution_flow(comp.id, depth)
        return flow.to_dict() if flow else None

    def show_dependencies(self, space_id: str, component: str) -> dict[str, Any] | None:
        """Show dependencies for a component."""
        ctx = self._load_context(space_id)
        comp = ctx.catalog.find_component(component)
        if not comp:
            return None
        deps = ctx.flow_analyzer.analyze_dependencies(comp.id)
        return deps.to_dict() if deps else None

    def get_call_graph_with_source(
        self, space_id: str, component_id: str, depth: int
    ) -> dict[str, Any] | None:
        """Build call graph with full source code for a component."""
        return self.get_call_graph_with_source_diagnosed(space_id, component_id, depth)[0]

    def get_call_graph_with_source_diagnosed(
        self, space_id: str, component_id: str, depth: int
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Like ``get_call_graph_with_source`` but also returns WHY a build failed.

        Returns ``(graph, None)`` on success, or ``(None, reason)`` where ``reason``
        names the exact condition — component not in the catalog, a non-callable
        type, or the source tree being evicted mid-build. The reason is captured
        here, at build time, so a caller's warning reflects the actual cause even
        when a transient eviction has since resolved.
        """
        ctx = self._load_context(space_id)
        comp = ctx.catalog.find_component(component_id)
        if not comp:
            return None, "component id not found in the catalog"
        from ..core.models import ComponentType

        if comp.type not in [ComponentType.FUNCTION, ComponentType.METHOD]:
            return None, f"component type '{comp.type.value}' is not a function or method"

        # Hydrate before the flow analyzer reads the files with raw open() calls; a
        # recycled container starts with an empty cache.
        #
        # This is the one reader that hydrates once and then opens many files itself,
        # so it cannot use the provider's retrying read_text. If the tree is evicted
        # part-way through the build the analyzer bakes "Error reading source" into the
        # returned bodies, so re-hydrate and rebuild once. If the tree is still gone
        # after that retry, return None rather than a graph whose source is error text:
        # a persistent absence is not eviction, and the caller cannot otherwise tell
        # degraded content from real content.
        for attempt in (1, 2):
            source_root = self.source_provider.source_root(space_id)
            result = ctx.flow_analyzer.build_call_graph_with_source(
                component_id, depth, source_root
            )
            if source_root.is_dir():
                return result, None
            if attempt == 1:
                logger.info(
                    f"Source tree for '{space_id}' vanished while building a call graph "
                    f"(evicted under disk pressure); re-hydrating and rebuilding once"
                )
        return None, "source tree evicted from the disk cache during the build (disk pressure)"

    # ── summarizer support ──────────────────────────────────────────────

    def get_max_depth(self, space_id: str) -> int:
        """Get maximum call depth in the call graph for a space."""
        ctx = self._load_context(space_id)
        return ctx.flow_analyzer.get_max_depth()

    def get_call_depths(self, space_id: str) -> dict[str, int]:
        """Get call depths for all callable components in a space."""
        ctx = self._load_context(space_id)
        return ctx.flow_analyzer.get_call_depths()

    def get_components_by_depth(self, space_id: str, depth: int) -> list[str]:
        """Get component IDs at a specific call depth."""
        ctx = self._load_context(space_id)
        return ctx.flow_analyzer.get_components_by_depth(depth)

    def get_business_relevance_tier(self, space_id: str, component_id: str) -> str | None:
        """Return the computed business relevance tier for a component."""
        ctx = self._load_context(space_id)
        comp = ctx.catalog.find_component(component_id)
        if not comp:
            return None
        return comp.business_behavior.business_relevance_tier

    # ── semantic search ─────────────────────────────────────────────────

    def semantic_search(
        self, query: str, n_results: int | None = None, space_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Semantic search across component descriptions."""
        from ..vector.s3_vectors_client import S3VectorsClient

        logger.info(
            f"semantic_search: space_id={space_id or '<all>'}, n_results={n_results}, "
            f"query_len={len(query)}"
        )
        logger.debug(f"semantic_search query text: {query!r}")

        store = S3VectorsClient()
        if space_id:
            results = store.retrieve_with_metadata_filter(
                query, {"space_id": space_id}, top_k=n_results
            )
        else:
            # The "all spaces" $exists path is unscoped. With authorization on it
            # would read spaces the caller can't see, so it's only allowed off.
            from ..authz.startup import require_authorization

            if require_authorization():
                raise ValueError("semantic_search needs a space_id when authorization is on")
            results = store.retrieve_with_metadata_filter(
                query, {"space_id": {"$exists": True}}, top_k=n_results
            )
        self._enrich_search_results_with_relevance(results)
        logger.info(f"semantic_search: {len(results)} result(s) for space_id={space_id or '<all>'}")
        return results

    def global_semantic_search(
        self,
        query: str,
        n_results: int | None = None,
        space_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic search across multiple (or all) spaces."""
        from ..vector.s3_vectors_client import S3VectorsClient

        scope = "<all spaces>" if not space_ids else f"{len(space_ids)} space(s)"
        logger.info(
            f"global_semantic_search: scope={scope}, n_results={n_results}, query_len={len(query)}"
        )
        logger.debug(
            f"global_semantic_search query text: {query!r}, resolved space_ids={space_ids}"
        )

        store = S3VectorsClient()
        if not space_ids:
            # No scope = search every space. With authz on that would leak
            # unreadable spaces, so it's refused (allowed only when authz is off).
            from ..authz.startup import require_authorization

            if require_authorization():
                raise ValueError(
                    "global_semantic_search needs an explicit scope when authorization is on"
                )
            results = store.retrieve_documents(query, top_k=n_results)
        else:
            # One $in query, or scatter-gather + merge when the scope exceeds the
            # per-query id cap. retrieve_scoped handles both.
            results = store.retrieve_scoped(query, space_ids, top_k=n_results)
        self._enrich_search_results_with_relevance(results)
        logger.info(f"global_semantic_search: {len(results)} result(s) (scope={scope})")
        return results

    def index_component_description(
        self, space_id: str, component_key: str, description: str
    ) -> str:
        """Store a business-level description of a component in the vector store."""
        from ..core.models import ComponentType
        from ..vector.s3_vectors_client import S3VectorsClient

        ctx = self._load_context(space_id)
        comp = ctx.catalog.find_component(component_key)
        if not comp:
            return f"Component '{component_key}' not found in catalog."
        if comp.type not in (ComponentType.FUNCTION, ComponentType.METHOD):
            return (
                f"Skipped '{component_key}': type '{comp.type.value}' is not a function or method."
            )

        store = S3VectorsClient()
        metadata = {
            "component_id": component_key,
            "description": description,
            "space_id": space_id,
            "type": comp.type.value,
            "file_path": self._vector_file_path(space_id, comp.file_path),
            "line_number": comp.line_number,
            "business_relevance_score": comp.business_behavior.business_relevance_score,
            "business_relevance_tier": comp.business_behavior.business_relevance_tier,
            "business_relevance_reasons": comp.business_behavior.business_relevance_reasons,
        }
        # S3 Vectors metadata only supports strings, numbers, booleans, or
        # non-empty arrays of those primitives. Filter out incompatible values.
        metadata = {
            k: v
            for k, v in metadata.items()
            if v is not None and v != [] and not isinstance(v, dict)
        }
        # component_key is already {space_id}::-prefixed, so this vector key is
        # space-qualified as-is (not a bare component id).
        store.insert_document(component_key, description, metadata=metadata)
        return f"Stored description for '{component_key}'."

    def _vector_file_path(self, space_id: str, file_path: Path) -> str:
        """The path creation stores. Refresh summarizes from its clone, which is deleted after."""
        clone_dir = self.source_provider.work_path(space_id, WORK_DIR_REFRESH)
        try:
            relative = Path(file_path).relative_to(clone_dir)
        except ValueError:
            return str(file_path)
        return str(self.source_provider.cache_path(space_id) / relative)

    # ── hyperspaces ─────────────────────────────────────────────────────

    def _hyperspace_s3_key(self, hyperspace_id: str) -> str:
        return f"hyperspaces/{hyperspace_id}/config.json"

    def list_hyperspaces(
        self, *, status_sink: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """List all hyperspaces from S3, enriched with member-space status.

        ``status_sink`` (optional): receives the member ``space_id ->
        status.json`` map fetched here in parallel, so the authz layer can
        annotate inaccessible members without re-reading each status from S3.

        The naive implementation did one ``GetObject`` per hyperspace and
        one more per member space inside ``_enrich_hyperspace``, all
        sequential — O(hyperspaces × members). We flatten that:

        1. Read every hyperspace config in parallel.
        2. Collect the union of member ``space_id`` values across all
           hyperspaces and fetch their statuses in parallel (deduped).
        3. Stitch the cached statuses into each hyperspace.
        """
        import concurrent.futures
        import time

        bucket = self.kb_manager._get_s3_bucket()
        logger.debug(f"s3.list_objects_v2: s3://{bucket}/hyperspaces/ (delimiter=/)")

        paginator = self.kb_manager.s3_client.get_paginator("list_objects_v2")
        hs_ids: list[str] = []
        for page in paginator.paginate(Bucket=bucket, Prefix="hyperspaces/", Delimiter="/"):
            for prefix in page.get("CommonPrefixes", []) or []:
                hs_ids.append(prefix["Prefix"].split("/")[1])
        logger.debug(f"s3.list_objects_v2 returned {len(hs_ids)} hyperspace prefix(es)")

        if not hs_ids:
            return []

        t0 = time.perf_counter()

        # 1. Parallel fetch of hyperspace configs.
        hs_workers = min(32, max(4, len(hs_ids)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=hs_workers) as ex:
            configs = list(ex.map(self._read_hyperspace, hs_ids))

        # 2. Unique member space_ids referenced across all configs.
        unique_space_ids: list[str] = []
        seen: set[str] = set()
        for cfg in configs:
            if not cfg:
                continue
            for sid in cfg.get("space_ids", []) or []:
                if sid not in seen:
                    seen.add(sid)
                    unique_space_ids.append(sid)

        # Parallel fetch of member statuses (deduped).
        status_by_id: dict[str, dict[str, Any] | None] = {}
        if unique_space_ids:
            status_workers = min(32, max(4, len(unique_space_ids)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=status_workers) as ex:
                statuses = list(ex.map(self._read_status, unique_space_ids))
            status_by_id = dict(zip(unique_space_ids, statuses))

        # 3. Enrich each hyperspace from the in-memory status cache.
        results: list[dict[str, Any]] = []
        for cfg in configs:
            if cfg:
                results.append(self._enrich_hyperspace_with_cache(cfg, status_by_id))

        if status_sink is not None:
            status_sink.update(status_by_id)

        logger.debug(
            f"list_hyperspaces: {len(results)} hyperspace(s), "
            f"{len(unique_space_ids)} unique member space(s) in "
            f"{(time.perf_counter() - t0) * 1000:.0f}ms"
        )
        return results

    def _read_hyperspace(self, hyperspace_id: str) -> dict[str, Any] | None:
        cfg, _etag = self._read_hyperspace_with_etag(hyperspace_id)
        return cfg

    def _read_hyperspace_with_etag(
        self, hyperspace_id: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Read a hyperspace config.json plus its S3 ETag, for a conditional claim."""
        bucket = self.kb_manager._get_s3_bucket()
        key = self._hyperspace_s3_key(hyperspace_id)
        logger.debug(f"s3.get_object hyperspace: s3://{bucket}/{key}")
        try:
            resp = self.kb_manager.s3_client.get_object(Bucket=bucket, Key=key)
            return json.loads(resp["Body"].read().decode("utf-8")), resp.get("ETag")
        except Exception as e:
            logger.debug(f"s3.get_object hyperspace miss or failure s3://{bucket}/{key}: {e}")
            return None, None

    def _enrich_hyperspace(
        self, cfg: dict[str, Any], *, status_sink: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Attach member-space status and derived invalid/missing flags.

        Single-hyperspace path. Fetches each member's status.json
        individually in parallel — fine for ``get_hyperspace`` /
        ``update_hyperspace`` which only work on one hyperspace at a time.
        For the bulk ``list_hyperspaces`` path, we pre-fetch statuses once
        across all hyperspaces and use ``_enrich_hyperspace_with_cache``
        to avoid redundant reads. ``status_sink`` lets the authz layer reuse
        the fetched statuses instead of re-reading them per member.
        """
        import concurrent.futures

        space_ids = list(cfg.get("space_ids", []) or [])
        status_by_id: dict[str, dict[str, Any] | None] = {}
        if space_ids:
            max_workers = min(32, max(4, len(space_ids)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
                statuses = list(ex.map(self._read_status, space_ids))
            status_by_id = dict(zip(space_ids, statuses))
        if status_sink is not None:
            status_sink.update(status_by_id)
        return self._enrich_hyperspace_with_cache(cfg, status_by_id)

    def _enrich_hyperspace_with_cache(
        self,
        cfg: dict[str, Any],
        status_by_id: dict[str, dict[str, Any] | None],
    ) -> dict[str, Any]:
        """Enrich a hyperspace from a pre-fetched status cache.

        ``status_by_id`` maps ``space_id -> status.json contents`` (or
        ``None`` if missing). Any member ``space_id`` not in the cache is
        treated as missing.
        """
        spaces_info: list[dict[str, Any]] = []
        missing: list[str] = []
        for sid in cfg.get("space_ids", []) or []:
            status = status_by_id.get(sid)
            if status is None:
                missing.append(sid)
            spaces_info.append(
                {
                    "space_id": sid,
                    "status": status.get("status", "UNKNOWN") if status else "MISSING",
                    "space_name": status.get("space_name") if status else None,
                    "component_count": status.get("component_count") if status else None,
                }
            )
        cfg["spaces"] = spaces_info
        # Union of persisted and freshly-detected missing ids — covers
        # hyperspaces created before the invalidation logic was in place.
        persisted_missing = cfg.get("missing_space_ids", []) or []
        cfg["missing_space_ids"] = list(dict.fromkeys([*persisted_missing, *missing]))
        cfg["invalid"] = bool(cfg.get("invalid")) or bool(cfg["missing_space_ids"])
        cfg["overview_staleness"] = self._compute_overview_staleness(cfg, status_by_id)
        return cfg

    def _compute_overview_staleness(
        self,
        cfg: dict[str, Any],
        status_by_id: dict[str, dict[str, Any] | None],
    ) -> dict[str, Any]:
        """Per-member staleness of the hyperspace overview, computed on read.

        Compares the provenance snapshot captured at synthesis against the
        current member statuses (already fetched for enrichment, so this is
        cheap). Git members drift when their indexed commit changes; zip members
        have no commit (immutable source) and can only change via a membership
        swap, surfaced as member_added / member_removed. Returns
        ``{"stale": bool, "reasons": [{space_id, space_name, reason, ...}]}``.
        """
        docs = cfg.get("hyperspace_overview_docs") or []
        snapshot = cfg.get("overview_member_snapshot")
        # No overview yet, or generated without a snapshot: nothing to compare.
        if not docs or snapshot is None:
            return {"stale": False, "reasons": []}

        snap_by_id = {e.get("space_id"): e for e in snapshot}
        current_ids = list(cfg.get("space_ids") or [])
        current_set = set(current_ids)
        reasons: list[dict[str, Any]] = []

        for sid in current_ids:
            st = status_by_id.get(sid)
            snap = snap_by_id.get(sid)
            name = (st.get("space_name") if st else None) or (
                snap.get("space_name") if snap else None
            )
            if snap is None:
                reasons.append({"space_id": sid, "space_name": name, "reason": "member_added"})
                continue
            if st is None:
                reasons.append({"space_id": sid, "space_name": name, "reason": "space_missing"})
                continue
            if st.get("status") != "READY":
                reasons.append({"space_id": sid, "space_name": name, "reason": "not_ready"})
                continue
            cur_commit = st.get("git_commit_hash")
            snap_commit = snap.get("git_commit_hash")
            # Zip members have no commit on either side → never code-stale.
            if (cur_commit or snap_commit) and cur_commit != snap_commit:
                reasons.append(
                    {
                        "space_id": sid,
                        "space_name": name,
                        "reason": "code_changed",
                        "from_commit": snap_commit,
                        "to_commit": cur_commit,
                    }
                )

        for sid, snap in snap_by_id.items():
            if sid not in current_set:
                reasons.append(
                    {
                        "space_id": sid,
                        "space_name": snap.get("space_name"),
                        "reason": "member_removed",
                    }
                )

        return {"stale": len(reasons) > 0, "reasons": reasons}

    def _validate_hyperspace_members(self, space_ids: list[str]) -> None:
        """Members must number 1..MEMBER_CAP and each exist and be READY."""
        from ..authz.hyperspace import MEMBER_CAP

        if len(space_ids) < 1:
            raise ServiceError("A hyperspace requires at least 1 space.")
        if len(space_ids) > MEMBER_CAP:
            raise ServiceError(f"A hyperspace can have at most {MEMBER_CAP} spaces.")
        for sid in space_ids:
            status = self._read_status(sid)
            if not status:
                raise SpaceNotFoundError(sid)
            if status.get("status") not in QUERYABLE_STATUSES:
                raise ServiceError(f"Space '{sid}' is not READY (status: {status.get('status')}).")

    def create_hyperspace(
        self,
        name: str,
        space_ids: list[str],
        created_by: str | None = None,
        created_by_sub: str | None = None,
    ) -> dict[str, Any]:
        """Create a hyperspace (named group of 1 or more spaces).

        ``created_by_sub`` is the caller's immutable subject, stored so later
        owner checks (update / delete) don't depend on a mutable email.
        """
        self._validate_hyperspace_members(space_ids)

        hyperspace_id = _slugify_to_space_id(name)
        config: dict[str, Any] = {
            "hyperspace_id": hyperspace_id,
            "name": name,
            "space_ids": space_ids,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if created_by:
            config["created_by"] = created_by
        if created_by_sub:
            config["created_by_sub"] = created_by_sub
        bucket = self.kb_manager._get_s3_bucket()
        self.kb_manager.s3_client.put_object(
            Bucket=bucket,
            Key=self._hyperspace_s3_key(hyperspace_id),
            Body=json.dumps(config).encode("utf-8"),
            ContentType="application/json",
        )
        return config

    def read_hyperspace_config(self, hyperspace_id: str) -> dict[str, Any] | None:
        """Public seam: raw hyperspace config, or None. No member enrichment —
        for the owner gate, which only needs created_by and must not pay an
        N-member S3 fan-out."""
        return self._read_hyperspace(hyperspace_id)

    def get_hyperspace(
        self, hyperspace_id: str, *, status_sink: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Get a hyperspace config, enriched with space summaries.

        ``status_sink`` (optional): seeded with the member ``space_id ->
        status.json`` map so the authz layer can annotate inaccessible members
        without re-reading each status from S3.
        """
        cfg = self._read_hyperspace(hyperspace_id)
        if not cfg:
            raise HyperspaceNotFoundError(hyperspace_id)
        enriched = self._enrich_hyperspace(cfg, status_sink=status_sink)
        # Surface the Bedrock pricing link next to token usage, matching the
        # space status response. Cost is never computed — the UI links out.
        if enriched.get("token_usage"):
            from ..core.token_usage import pricing_url

            enriched.setdefault("pricing_url", pricing_url())
        return enriched

    def update_hyperspace(
        self,
        hyperspace_id: str,
        space_ids: list[str] | None = None,
        updated_by: str | None = None,
    ) -> dict[str, Any]:
        """Update a hyperspace's member spaces.

        The hyperspace_id, name, created_at, and created_by are preserved. The
        name cannot be edited (parity with spaces, which have no rename). When
        space_ids is provided, it fully replaces the existing membership and
        every space in the new list must exist and be READY (same rules as
        create). A successful swap to fully valid spaces clears the
        invalid/missing flags so teams can keep pointing at a stable
        hyperspace id while rotating the underlying spaces.
        """
        cfg = self._read_hyperspace(hyperspace_id)
        if not cfg:
            raise HyperspaceNotFoundError(hyperspace_id)

        if space_ids is None:
            raise ServiceError("No changes provided: specify space_ids.")

        if space_ids is not None:
            self._validate_hyperspace_members(space_ids)
            cfg["space_ids"] = space_ids
            # Swapped to a fully valid membership — clear any stale invalid
            # markers left over from a previously deleted member.
            cfg["missing_space_ids"] = []
            cfg["invalid"] = False
            cfg.pop("invalidated_at", None)

        cfg["updated_at"] = datetime.now(timezone.utc).isoformat()
        if updated_by:
            cfg["updated_by"] = updated_by

        # Don't persist derived fields
        cfg.pop("spaces", None)

        bucket = self.kb_manager._get_s3_bucket()
        self.kb_manager.s3_client.put_object(
            Bucket=bucket,
            Key=self._hyperspace_s3_key(hyperspace_id),
            Body=json.dumps(cfg).encode("utf-8"),
            ContentType="application/json",
        )
        return self._enrich_hyperspace(cfg)

    def delete_hyperspace(self, hyperspace_id: str) -> dict[str, Any]:
        """Delete a hyperspace config from S3."""
        cfg = self._read_hyperspace(hyperspace_id)
        if not cfg:
            raise HyperspaceNotFoundError(hyperspace_id)
        bucket = self.kb_manager._get_s3_bucket()
        self.kb_manager.s3_client.delete_object(
            Bucket=bucket, Key=self._hyperspace_s3_key(hyperspace_id)
        )
        return {"hyperspace_id": hyperspace_id, "deleted": True}

    # ── hyperspace overview (summary-of-summaries) ──────────────────────

    def _write_hyperspace(self, cfg: dict[str, Any], *, if_match: str | None = None) -> None:
        """Persist a hyperspace config.json to S3.

        ``spaces`` is a derived field (added by ``_enrich_hyperspace``) and is
        never persisted; callers should drop it before calling, but we defend
        here too.

        When ``if_match`` is set, the write is conditional on the object still
        having that ETag (S3 ``IfMatch``), so a read-modify-write can claim the
        config atomically; a concurrent writer makes it fail with
        ``PreconditionFailed``.
        """
        cfg.pop("spaces", None)
        bucket = self.kb_manager._get_s3_bucket()
        extra = {"IfMatch": if_match} if if_match else {}
        self.kb_manager.s3_client.put_object(
            Bucket=bucket,
            Key=self._hyperspace_s3_key(cfg["hyperspace_id"]),
            Body=json.dumps(cfg).encode("utf-8"),
            ContentType="application/json",
            **extra,
        )

    def _load_member_overview(self, space_id: str) -> dict[str, Any]:
        """Load one member space's metadata + its Space Overview documents.

        Reads the member's status.json for metadata and its persisted
        ``space_overview_docs`` catalog, then fetches the subset of overview
        markdown used for hyperspace synthesis. ``docs`` is empty when the
        member has no Space Overview yet — the generator reports that as a gap.
        """
        from ..vector.hyperspace_overview_generator import MEMBER_DOC_KEYS_FOR_SYNTHESIS

        status = self._read_status(space_id) or {}
        docs: dict[str, str] = {}
        wanted = set(MEMBER_DOC_KEYS_FOR_SYNTHESIS)
        bucket = self.kb_manager._get_s3_bucket()
        for entry in status.get("space_overview_docs") or []:
            key = entry.get("key")
            s3_path = entry.get("s3_path")
            if key not in wanted or not s3_path:
                continue
            try:
                resp = self.kb_manager.s3_client.get_object(Bucket=bucket, Key=s3_path)
                docs[key] = resp["Body"].read().decode("utf-8")
            except Exception:
                logger.debug(f"Failed to read member overview doc {s3_path}", exc_info=True)

        bc = status.get("business_context")
        business_context = bc if isinstance(bc, str) else None

        return {
            "space_id": space_id,
            "space_name": status.get("space_name"),
            "component_count": status.get("component_count"),
            "languages": status.get("languages"),
            "business_context": business_context,
            "docs": docs,
        }

    def _member_overview_needs_generation(self, status: dict[str, Any] | None) -> bool:
        """Whether a member space's Space Overview must be (re)generated.

        Returns True when the space is READY and either has no Space Overview
        yet, or has one that a later code refresh marked stale (i.e. the overview
        is older than the latest indexed code). Non-READY spaces return False —
        their source/KB isn't in a state we can generate from, so we skip them
        and let the synthesis note the gap.
        """
        if not status or status.get("status") != "READY":
            return False
        docs = status.get("space_overview_docs") or []
        if not docs:
            return True
        return any(d.get("stale") for d in docs)

    def get_hyperspace_overview_document(self, hyperspace_id: str, doc_key: str) -> str:
        """Read a Hyperspace Overview markdown document from S3.

        Raises HyperspaceNotFoundError if the hyperspace, the doc_key, or the
        generated document does not exist.
        """
        cfg = self._read_hyperspace(hyperspace_id)
        if not cfg:
            raise HyperspaceNotFoundError(hyperspace_id)

        from ..vector.hyperspace_overview_generator import HYPERSPACE_OVERVIEW_DOCUMENTS

        doc_def = next((d for d in HYPERSPACE_OVERVIEW_DOCUMENTS if d["key"] == doc_key), None)
        if not doc_def:
            raise HyperspaceNotFoundError(f"Unknown hyperspace overview document: {doc_key}")

        bucket = self.kb_manager._get_s3_bucket()
        s3_key = f"hyperspaces/{hyperspace_id}/docs/{doc_def['filename']}"
        try:
            resp = self.kb_manager.s3_client.get_object(Bucket=bucket, Key=s3_key)
            return resp["Body"].read().decode("utf-8")
        except Exception:
            raise HyperspaceNotFoundError(
                f"Overview document not found: {doc_key} for hyperspace {hyperspace_id}"
            )

    def start_hyperspace_overview_generation(self, hyperspace_id: str) -> dict[str, Any]:
        """Trigger Hyperspace Overview generation for an existing hyperspace.

        Synthesizes a system-level overview from the member spaces' existing
        Space Overview documents. Runs in a background thread and returns
        immediately.

        NOTE: this mirrors the Space Overview execution model (a slot on the
        in-process pipeline pool; raises PipelineCapacityError when the pool is
        full). For large hyperspaces this is a heavy, long-running
        Bedrock job; moving overview generation onto a shared job queue is
        tracked separately as an infrastructure change.
        """
        cfg, etag = self._read_hyperspace_with_etag(hyperspace_id)
        if not cfg:
            raise HyperspaceNotFoundError(hyperspace_id)
        # Refuse a duplicate while a fresh run is in progress (double-click, retry
        # after a slow response, or a second user) — otherwise a second generator
        # starts, doubling Bedrock cost and making the final catalog depend on
        # whichever run finishes last.
        if self._overview_run_is_fresh(cfg):
            raise OverviewInProgressError(hyperspace_id)

        # Reserve a pipeline slot before the claim write, so a 429 leaves no
        # in_progress claim behind. A lost claim below returns the slot.
        with pipeline_pool().reserve("hyperspace_overview", hyperspace_id) as slot:
            # Claim the run atomically BEFORE touching S3 objects: clear the catalog
            # and mark in_progress with a conditional write (IfMatch on the ETag we
            # just read). If another request claimed it between our read and write,
            # the write fails and we refuse rather than starting a second generator.
            prior_docs = cfg.get("hyperspace_overview_docs") or []
            cfg["hyperspace_overview_docs"] = []
            cfg["overview_status"] = "in_progress"
            cfg["overview_done"] = 0
            cfg["overview_total"] = 0
            cfg["overview_percent"] = 0.0
            cfg.pop("overview_error", None)
            cfg["updated_at"] = datetime.now(timezone.utc).isoformat()
            try:
                self._write_hyperspace(cfg, if_match=etag)
            except ClientError as e:
                if self._is_precondition_failed(e):
                    raise OverviewInProgressError(hyperspace_id) from e
                raise

            # The claim is ours. If the job then fails to start, end the run failed
            # rather than leave it in_progress. The catalog is already empty and some
            # prior documents may be deleted, so it ends as a failed run would.
            def _undo_claim() -> None:
                cfg.update(
                    {
                        "overview_status": "failed",
                        "overview_error": self._JOB_START_FAILED,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                self._write_hyperspace(cfg)

            with self._undo_unless_submitted(slot, _undo_claim):
                # Now safe to delete the previously generated documents (the catalog
                # pointing at them was just cleared). A losing request never reaches
                # here, so it cannot delete the winner's documents.
                if prior_docs:
                    bucket = self.kb_manager._get_s3_bucket()
                    for doc in prior_docs:
                        s3_path = doc.get("s3_path")
                        if not s3_path:
                            continue
                        try:
                            self.kb_manager.s3_client.delete_object(Bucket=bucket, Key=s3_path)
                        except Exception:
                            logger.debug(
                                f"Failed to delete prior hyperspace overview doc {s3_path}",
                                exc_info=True,
                            )

                slot.submit(self._run_hyperspace_overview, hyperspace_id)

        return {
            "hyperspace_id": hyperspace_id,
            "status": "SUBMITTED",
            "message": (
                "Hyperspace Overview generation started. "
                "Poll GET /api/hyperspaces/{id} for progress."
            ),
        }

    def _run_hyperspace_overview(self, hyperspace_id: str) -> dict[str, Any] | None:
        """Background runner: synthesize and persist the hyperspace overview."""
        logger.info(f"[worker] Starting Hyperspace Overview generation for '{hyperspace_id}'...")
        try:
            from ..vector.hyperspace_overview_generator import (
                HYPERSPACE_OVERVIEW_DOCUMENTS,
                HyperspaceOverviewGenerator,
            )

            cfg = self._read_hyperspace(hyperspace_id)
            if not cfg:
                logger.warning(
                    f"[worker] Hyperspace '{hyperspace_id}' disappeared before overview run"
                )
                return None

            space_ids = list(cfg.get("space_ids", []) or [])

            # Phase 1 — ensure every READY member has an up-to-date Space
            # Overview, (re)generating any that are missing or stale (older than
            # the latest code refresh). Synchronous and potentially slow/costly;
            # the UI warns and confirms before this is triggered. Overall
            # progress spans both this phase and the synthesis phase below.
            member_statuses = {sid: (self._read_status(sid) or {}) for sid in space_ids}
            to_generate = [
                sid
                for sid in space_ids
                if self._member_overview_needs_generation(member_statuses[sid])
            ]
            synth_total = len(HYPERSPACE_OVERVIEW_DOCUMENTS)
            overall_total = len(to_generate) + synth_total

            def _write_progress(done: int) -> None:
                pct = round((done / overall_total) * 100, 1) if overall_total > 0 else 0.0
                cur = self._read_hyperspace(hyperspace_id)
                if not cur:
                    return
                cur.update(
                    {
                        "overview_status": "in_progress",
                        "overview_total": overall_total,
                        "overview_done": done,
                        "overview_percent": pct,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                self._write_hyperspace(cur)

            _write_progress(0)
            for idx, sid in enumerate(to_generate):
                logger.info(
                    f"[worker] Hyperspace '{hyperspace_id}': ensuring member Space "
                    f"Overview {idx + 1}/{len(to_generate)} for '{sid}'"
                )
                # Synchronous; writes the member space's own docs + status.
                # Never raises (records the member's own failure and returns None).
                self._run_space_overview(sid)
                _write_progress(idx + 1)

            # Phase 2 — synthesize the hyperspace overview from the (now
            # populated) member Space Overviews.
            members = [self._load_member_overview(sid) for sid in space_ids]
            gen_count = len(to_generate)
            generator = HyperspaceOverviewGenerator(
                hyperspace_id=hyperspace_id,
                hyperspace_name=cfg.get("name") or hyperspace_id,
                members=members,
                on_progress=lambda done, total: _write_progress(gen_count + done),
            )
            result = generator.generate_all()

            # Persist each generated .md file to S3.
            bucket = self.kb_manager._get_s3_bucket()
            doc_catalog = []
            for doc in result["documents"]:
                s3_path = f"hyperspaces/{hyperspace_id}/docs/{doc['filename']}"
                self.kb_manager.s3_client.put_object(
                    Bucket=bucket,
                    Key=s3_path,
                    Body=doc["content"].encode("utf-8"),
                    ContentType="text/markdown",
                )
                doc_catalog.append(
                    {
                        "key": doc["key"],
                        "title": doc["title"],
                        "s3_path": s3_path,
                        "generated_at": doc["generated_at"],
                    }
                )
                logger.info(f"[worker] Saved hyperspace overview doc: s3://{bucket}/{s3_path}")

            cur = self._read_hyperspace(hyperspace_id) or cfg
            overview_usage = operation_usage([result["stats"]])
            token_usage = cur.get("token_usage") or {}
            prior_overview = token_usage.get("hyperspace_overview") or {}
            trigger = "manual" if prior_overview.get("generation_count") else "initial"
            if overview_usage:
                token_usage["hyperspace_overview"] = self._record_recurring_usage(
                    prior_overview,
                    overview_usage,
                    trigger=trigger,
                    extra={"documents": result["stats"].get("generated", 0)},
                )
            # Provenance snapshot: the source identity each member overview was
            # synthesized from, so staleness can be computed per member on read.
            # Git members record their indexed commit; zip members have none
            # (immutable source — they change only via a membership swap, which
            # the space_id set diff catches).
            member_snapshot = []
            for sid in space_ids:
                st = self._read_status(sid) or {}
                docs = st.get("space_overview_docs") or []
                ov_at = max(
                    (d.get("generated_at") for d in docs if d.get("generated_at")),
                    default=None,
                )
                member_snapshot.append(
                    {
                        "space_id": sid,
                        "space_name": st.get("space_name"),
                        "git_commit_hash": st.get("git_commit_hash"),
                        "overview_generated_at": ov_at,
                    }
                )

            cur.update(
                {
                    "overview_status": "completed",
                    "hyperspace_overview_docs": doc_catalog,
                    "overview_stats": result["stats"],
                    "overview_member_snapshot": member_snapshot,
                    "bedrock_model": self._model_metadata(result["stats"].get("region")),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            if token_usage:
                cur["token_usage"] = token_usage
            self._write_hyperspace(cur)

            logger.info(
                f"[worker] Hyperspace Overview complete: "
                f"{result['stats']['generated']} generated, "
                f"{result['stats']['failed']} failed"
            )
            return result["stats"]

        except Exception as e:
            cur = self._read_hyperspace(hyperspace_id)
            if cur:
                cur.update(
                    {
                        "overview_status": "failed",
                        "overview_error": str(e),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                self._write_hyperspace(cur)
            logger.error(
                f"[worker] Hyperspace Overview failed for {hyperspace_id}: {e}",
                exc_info=True,
            )
            return None


# ── exceptions ──────────────────────────────────────────────────────────
#
# Defined in service/errors.py and re-exported here. The definitions moved so
# source_provider can raise them without importing this module (import cycle);
# the re-export keeps `from ...service.codekb_service import ServiceError`
# working for the API routes that already import from here.

__all__ = [
    "SOURCE_CACHE_ROOT",
    "CodeKBService",
    "HyperspaceNotFoundError",
    "ServiceError",
    "SourceNotAvailableError",
    "SpaceNotFoundError",
    "sweep_source_cache_scratch",
    "zip_upload_enabled",
]
