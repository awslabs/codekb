"""Exception classes for the service layer.

Split out of ``codekb_service`` so collaborators in this package — notably
``source_provider`` — can raise service errors without importing the service
module and creating an import cycle. Mirrors ``analysis/errors.py``.

``codekb_service`` re-exports every name defined here, so
``from ...service.codekb_service import ServiceError`` keeps working for the API
routes that already do that.
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base exception for service layer errors.

    Attributes:
        status_code: HTTP status code hint for the API layer.
            When set, the API route uses this to determine the appropriate
            HTTP response code (e.g. 409 for busy, 400 for bad request).
            Defaults to 400.
    """

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class SpaceNotFoundError(ServiceError):
    """Raised when a space is not found."""

    def __init__(self, space_id: str) -> None:
        self.space_id = space_id
        super().__init__(f"Space not found: {space_id}")


class SpaceBusyError(ServiceError):
    """Raised when a space cannot be deleted because it is being indexed."""

    def __init__(self, space_id: str) -> None:
        self.space_id = space_id
        super().__init__(
            f"Space '{space_id}' is being indexed; retry once indexing finishes.",
            status_code=409,
        )


class OverviewInProgressError(ServiceError):
    """Raised when overview generation is triggered while a run is already active.

    Carries a 409 so a double-click, a retry after a slow response, or a second
    user cannot start a second generator (which would double the Bedrock cost and
    make the resulting catalog depend on whichever run finished last).
    """

    def __init__(self, target_id: str) -> None:
        self.target_id = target_id
        super().__init__(
            f"Overview generation for '{target_id}' is already in progress.",
            status_code=409,
        )


class SourceNotAvailableError(ServiceError):
    """Raised when a space's source archive is not available in S3."""

    def __init__(self, space_id: str) -> None:
        self.space_id = space_id
        super().__init__(f"Source code not available for space: {space_id}")


class HyperspaceNotFoundError(ServiceError):
    """Raised when a hyperspace is not found."""

    def __init__(self, hyperspace_id: str) -> None:
        self.hyperspace_id = hyperspace_id
        super().__init__(f"Hyperspace not found: {hyperspace_id}")


class PipelineCapacityError(Exception):
    """Raised when every background-pipeline slot on this process is in use.

    Deliberately not a ServiceError: the routes map ServiceError to their own
    status codes (some hard-code 409), and this must always surface as 429 with a
    Retry-After header. api/app.py registers a handler for it.
    """

    def __init__(self, limit: int, retry_after: int) -> None:
        self.limit = limit
        self.retry_after = retry_after
        super().__init__(
            f"CodeKB is already running its maximum of {limit} background jobs on "
            f"this server. Try again in about {retry_after} seconds."
        )
