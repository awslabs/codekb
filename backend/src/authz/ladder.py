"""Git role → gate level. See the code = READ; maintainer tier = WRITE."""

from __future__ import annotations

from typing import Any, Optional

from ..core.logging_utils import get_logger

logger = get_logger(__name__)

NONE = "none"
READ = "read"
WRITE = "write"
MAINTAIN = "maintain"
ADMIN = "admin"

READ_LEVELS = frozenset({READ, WRITE, MAINTAIN, ADMIN})
WRITE_LEVELS = frozenset({MAINTAIN, ADMIN})


def can_read(level: Optional[str]) -> bool:
    return level in READ_LEVELS


def can_write(level: Optional[str]) -> bool:
    return level in WRITE_LEVELS


def github_level(permissions: Any) -> str:
    """Map a GitHub repo ``permissions`` object to a level."""
    if not isinstance(permissions, dict):
        return NONE
    if permissions.get("admin"):
        return ADMIN
    if permissions.get("maintain"):
        return MAINTAIN
    if permissions.get("push"):
        return WRITE
    if permissions.get("triage") or permissions.get("pull"):
        return READ
    return NONE


# GitLab numeric access levels → level. Guest/Minimal/none get nothing.
_GITLAB_LEVELS = {
    60: MAINTAIN,  # Admin
    50: MAINTAIN,  # Owner
    40: MAINTAIN,  # Maintainer
    30: WRITE,  # Developer
    25: READ,  # Security Manager
    20: READ,  # Reporter
    15: READ,  # Planner
    10: NONE,  # Guest
    5: NONE,  # Minimal
    0: NONE,
}


def gitlab_level(access_level: Any) -> str:
    """Map a GitLab access level to a level. Unknown → none (logged)."""
    try:
        value = int(access_level)
    except (TypeError, ValueError):
        logger.warning("authz.ladder: non-numeric GitLab level %r → none", access_level)
        return NONE
    level = _GITLAB_LEVELS.get(value)
    if level is None:
        logger.warning("authz.ladder: unknown GitLab level %s → none", value)
        return NONE
    return level


def bitbucket_level(permission: Any) -> str:
    """Map a Bitbucket permission (admin/write/read) to a level."""
    value = str(permission or "").strip().lower()
    if value == "admin":
        return ADMIN
    if value == "write":
        return WRITE
    if value == "read":
        return READ
    return NONE
