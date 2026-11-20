"""Hyperspace gates: all-or-nothing across members.

READ needs READ on every member; generate-overview needs WRITE on every member.
When several fail, precedence is: provider down (503) → expired token → denied.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import repo_access
from .errors import (
    AuthorizationUnavailable,
    CredentialExpired,
    HyperspaceAccessDenied,
    ReadDenied,
    WriteDenied,
)
from .startup import require_authorization

if TYPE_CHECKING:
    from .context import AuthContext

MEMBER_CAP = 200

READ_MESSAGE = (
    "You don't have the correct permissions to access this hyperspace. "
    "Make sure you have access to all the spaces within the hyperspace."
)
WRITE_MESSAGE = (
    "You don't have the correct permissions to generate an overview for this "
    "hyperspace. Make sure you have write access to all the spaces within the hyperspace."
)


def _check_all(ctx: "AuthContext", members: list[dict], write: bool, message: str) -> None:
    if not require_authorization():
        return
    gate = repo_access.can_write if write else repo_access.can_read
    pending_expired: CredentialExpired | None = None
    denied = False  # track the denial itself — a missing space_id must still fail closed
    inaccessible: list[str] = []
    for record in members:
        try:
            gate(ctx, record)
        except AuthorizationUnavailable:
            raise  # 503 wins
        except CredentialExpired as e:
            pending_expired = pending_expired or e
        except (ReadDenied, WriteDenied):
            denied = True
            sid = record.get("space_id")
            if sid:
                inaccessible.append(sid)
    if pending_expired is not None:
        raise pending_expired
    if denied:
        raise HyperspaceAccessDenied(message, inaccessible_space_ids=inaccessible)


def check_read(ctx: "AuthContext", members: list[dict]) -> None:
    """Raise unless the caller can READ every member."""
    _check_all(ctx, members, write=False, message=READ_MESSAGE)


def check_write(ctx: "AuthContext", members: list[dict]) -> None:
    """Raise unless the caller can WRITE every member."""
    _check_all(ctx, members, write=True, message=WRITE_MESSAGE)
