"""The caller, as authorization sees them.

Built from a ``UserIdentity``. Carries the subject and email, and memoizes
per-request space reads.
"""

from __future__ import annotations

from typing import Any, Optional

from ..core.logging_utils import get_logger

logger = get_logger(__name__)


class AuthContext:
    """One request's caller. Memos fill lazily on first use."""

    def __init__(
        self,
        sub: Optional[str],
        email: Optional[str],
        *,
        service: Any = None,
    ) -> None:
        self.sub = sub
        self.email = email or ""
        self._service = service
        self._status_memo: dict[str, Optional[dict]] = {}
        self._visible_memo: Optional[list[dict]] = None
        # Providers whose token was found expired during this request.
        self.expired_providers: set[str] = set()

    @classmethod
    def from_identity(
        cls,
        identity: Any,
        *,
        service: Any = None,
    ) -> "AuthContext":
        return cls(
            sub=getattr(identity, "sub", None),
            email=getattr(identity, "email", "") or "",
            service=service,
        )

    def _svc(self) -> Any:
        if self._service is None:
            from ..service.codekb_service import CodeKBService

            self._service = CodeKBService.instance()
        return self._service

    def space_status(self, space_id: str) -> Optional[dict]:
        """A space's status record, read at most once per request."""
        if space_id not in self._status_memo:
            self._status_memo[space_id] = self._svc().read_status(space_id)
        return self._status_memo[space_id]

    def preload_statuses(self, statuses: dict[str, Optional[dict]]) -> None:
        """Fill the memo from a bulk parallel fetch so later per-space checks
        don't re-read S3 one by one. Won't overwrite an already-cached entry."""
        for sid, record in statuses.items():
            self._status_memo.setdefault(sid, record)

    def visible_spaces(self) -> list[dict]:
        """Space summaries the caller can READ, computed once per request.

        Denied spaces are skipped, and so are private spaces behind an expired
        token (public ones stay listed, as with no token). A provider that is
        down (AuthorizationUnavailable) propagates: readable can't be told apart.
        """
        if self._visible_memo is not None:
            return self._visible_memo
        from . import repo_access

        out = [s for s in self._svc().list_spaces() if repo_access.can_list(self, s)]
        self._visible_memo = out
        return out
