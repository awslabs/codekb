"""authz.scope.resolve_scope — null = all readable, no cap (plan §6.3).

The scope is no longer capped: global search chunks a large id set into separate
vector queries and merges, so any number of readable spaces is searchable.
"""

from unittest.mock import patch

from src.authz import scope
from src.authz.context import AuthContext


class FakeService:
    def __init__(self, space_ids):
        self._ids = list(space_ids)

    def list_spaces(self):
        return [{"space_id": sid, "visibility": "public"} for sid in self._ids]

    def read_status(self, sid):
        if sid in self._ids:
            return {"space_id": sid, "visibility": "public"}
        return None


def _ctx(ids):
    return AuthContext(sub="u1", email="u@x.com", service=FakeService(ids))


def test_null_scope_expands_to_all_readable():
    # Switch off: every space is readable, so a null scope expands to all.
    with patch("src.authz.repo_access.require_authorization", return_value=False):
        ids = scope.resolve_scope(_ctx(["a", "b", "c"]), None)
    assert set(ids) == {"a", "b", "c"}


def test_null_scope_is_not_capped():
    many = [f"s{i}" for i in range(250)]
    with patch("src.authz.repo_access.require_authorization", return_value=False):
        ids = scope.resolve_scope(_ctx(many), None)
    assert len(ids) == 250


def test_explicit_list_keeps_only_readable_and_existing():
    ctx = _ctx(["a", "b"])

    # Switch on; a is readable, b is not; "ghost" has no record.
    def fake_readable(_ctx, record):
        return record["space_id"] == "a"

    with (
        patch("src.authz.scope.require_authorization", return_value=True),
        patch("src.authz.scope.repo_access.can_read_bool", side_effect=fake_readable),
    ):
        ids = scope.resolve_scope(ctx, ["a", "b", "ghost"])
    assert ids == ["a"]


def _expired_for_private(_ctx, record):
    from src.authz.errors import CredentialExpired

    if record.get("visibility") == "private":
        raise CredentialExpired("github")


class MixedService(FakeService):
    def list_spaces(self):
        return [
            {"space_id": "pub", "visibility": "public"},
            {"space_id": "priv", "visibility": "private"},
        ]

    def read_status(self, sid):
        return next((s for s in self.list_spaces() if s["space_id"] == sid), None)


def test_null_scope_with_expired_token_keeps_public():
    ctx = AuthContext(sub="u1", email="u@x.com", service=MixedService([]))
    with patch("src.authz.repo_access.can_read", side_effect=_expired_for_private):
        ids = scope.resolve_scope(ctx, None)
    assert ids == ["pub"]


def test_explicit_list_with_expired_token_still_raises():
    import pytest

    from src.authz.errors import CredentialExpired

    ctx = AuthContext(sub="u1", email="u@x.com", service=MixedService([]))
    with (
        patch("src.authz.scope.require_authorization", return_value=True),
        patch("src.authz.repo_access.can_read", side_effect=_expired_for_private),
        pytest.raises(CredentialExpired),
    ):
        scope.resolve_scope(ctx, ["pub", "priv"])
