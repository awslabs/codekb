"""authz.hyperspace — all-or-nothing member gates + precedence (plan §3, §6.4).

check_read/check_write run the per-space gate over every member. Precedence when
several fail: provider down (503) wins, else an expired token (401), else the
denied member ids are named in a 403.
"""

from unittest.mock import patch

import pytest

from src.authz import hyperspace as hs
from src.authz.errors import (
    AuthorizationUnavailable,
    CredentialExpired,
    HyperspaceAccessDenied,
    ReadDenied,
    WriteDenied,
)


def _members(*ids):
    return [{"space_id": i} for i in ids]


def _patch(gate_name, impl, authz=True):
    return [
        patch("src.authz.hyperspace.require_authorization", return_value=authz),
        patch(f"src.authz.hyperspace.repo_access.{gate_name}", side_effect=impl),
    ]


def _run(patches, fn, members):
    for p in patches:
        p.start()
    try:
        fn(object(), members)
    finally:
        for p in patches:
            p.stop()


def test_check_read_passes_when_all_members_readable():
    _run(_patch("can_read", lambda ctx, rec: None), hs.check_read, _members("a", "b"))


def test_check_read_names_the_denied_member():
    def gate(ctx, rec):
        if rec["space_id"] == "b":
            raise ReadDenied()

    with pytest.raises(HyperspaceAccessDenied) as ei:
        _run(_patch("can_read", gate), hs.check_read, _members("a", "b"))
    assert ei.value.inaccessible_space_ids == ["b"]
    assert "access this hyperspace" in str(ei.value)


def test_provider_down_wins_over_denied():
    def gate(ctx, rec):
        if rec["space_id"] == "a":
            raise AuthorizationUnavailable(provider="github")
        raise ReadDenied()

    with pytest.raises(AuthorizationUnavailable):
        _run(_patch("can_read", gate), hs.check_read, _members("a", "b"))


def test_expired_wins_over_denied():
    def gate(ctx, rec):
        if rec["space_id"] == "a":
            raise CredentialExpired("github")
        raise ReadDenied()

    with pytest.raises(CredentialExpired):
        _run(_patch("can_read", gate), hs.check_read, _members("a", "b"))


def test_switch_off_is_a_noop():
    def boom(ctx, rec):
        raise ReadDenied()

    # authz off → the per-member gate is never consulted.
    _run(_patch("can_read", boom, authz=False), hs.check_read, _members("a", "b"))


def test_check_write_uses_the_write_message():
    def gate(ctx, rec):
        if rec["space_id"] == "b":
            raise WriteDenied()

    with pytest.raises(HyperspaceAccessDenied) as ei:
        _run(_patch("can_write", gate), hs.check_write, _members("a", "b"))
    assert ei.value.inaccessible_space_ids == ["b"]
    assert "generate an overview" in str(ei.value)
