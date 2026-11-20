"""Authorization errors. Each carries the API ``error`` code and HTTP status."""

from __future__ import annotations

from typing import Optional


class AuthzError(Exception):
    """Base class. ``code`` is the response body error code."""

    code: str = "authorization_error"
    http_status: int = 403


class ReadDenied(AuthzError):
    """Can't read the space (or it doesn't exist). 401 so it never confirms existence."""

    code = "read_denied"
    http_status = 401

    def __init__(self, message: str = "You do not have access to this space.") -> None:
        super().__init__(message)


class WriteDenied(AuthzError):
    """Can read but not write."""

    code = "write_denied"
    http_status = 403

    def __init__(self, message: str = "You do not have write access to this space.") -> None:
        super().__init__(message)


class RepoReadDenied(AuthzError):
    """Create: can't read the git repo."""

    code = "repo_read_denied"
    http_status = 403

    def __init__(self, message: str = "You do not have access to this repository.") -> None:
        super().__init__(message)


class CredentialExpired(AuthzError):
    """A saved git token was rejected or is flagged invalid."""

    code = "git_credentials_expired"
    http_status = 401

    def __init__(self, provider: str, message: Optional[str] = None) -> None:
        self.provider = provider
        super().__init__(message or f"Your {provider} credentials have expired.")


class AuthorizationUnavailable(AuthzError):
    """Git provider unreachable or rate-limited — no decision possible."""

    code = "authorization_unavailable"
    http_status = 503

    def __init__(
        self,
        message: str = "Could not check your git permissions, try again.",
        provider: Optional[str] = None,
        retry_after: int = 30,
    ) -> None:
        self.provider = provider
        self.retry_after = retry_after
        super().__init__(message)


class HyperspaceAccessDenied(AuthzError):
    """A hyperspace member is out of reach."""

    code = "hyperspace_access_denied"
    http_status = 403

    def __init__(
        self,
        message: str,
        inaccessible_space_ids: Optional[list[str]] = None,
    ) -> None:
        self.inaccessible_space_ids = inaccessible_space_ids or []
        super().__init__(message)
