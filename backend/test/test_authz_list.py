"""Phase 2 (c): list returns only the spaces the caller can read (visible_spaces)."""

from unittest.mock import patch

from src.authz.cache import get_cache
from src.authz.context import AuthContext

SPACES = [
    {"space_id": "pub", "visibility": "public", "git_url": "https://github.com/o/pub.git"},
    {
        "space_id": "priv",
        "visibility": "private",
        "git_url": "https://github.com/o/priv.git",
        "provider": "github",
        "repo_key": "github.com/o/priv",
    },
    {"space_id": "zip", "source_type": "upload", "created_by": "u@x.com"},
]


class FakeSvc:
    def list_spaces(self):
        return list(SPACES)

    def read_status(self, sid):
        return next((s for s in SPACES if s["space_id"] == sid), None)


def test_visible_spaces_hides_unreadable_private():
    get_cache().clear()
    ctx = AuthContext(sub="list-u", email="u@x.com", service=FakeSvc())
    with (
        patch("src.authz.repo_access.require_authorization", return_value=True),
        patch("src.authz.repo_access._kms_configured", return_value=True),
        patch("src.authz.repo_access.provider_for_host", side_effect=lambda h: object() if h else None),
        patch("src.authz.repo_access.GitCredentialStore") as GS,
        patch("src.authz.repo_access.list_accessible_repos", return_value={}),  # no access
    ):
        GS.return_value.get_credential_state.return_value = (False, None, "tok")
        visible = {s["space_id"] for s in ctx.visible_spaces()}

    # public is readable by any signed-in user; the zip is readable because this
    # caller is its uploader; the private repo the caller can't access is hidden.
    assert visible == {"pub", "zip"}


def test_visible_spaces_shows_private_when_accessible():
    get_cache().clear()
    ctx = AuthContext(sub="list-u2", email="u@x.com", service=FakeSvc())
    with (
        patch("src.authz.repo_access.require_authorization", return_value=True),
        patch("src.authz.repo_access._kms_configured", return_value=True),
        patch("src.authz.repo_access.provider_for_host", side_effect=lambda h: object() if h else None),
        patch("src.authz.repo_access.GitCredentialStore") as GS,
        patch(
            "src.authz.repo_access.list_accessible_repos",
            return_value={"github.com/o/priv": {"pull": True}},  # read access
        ),
    ):
        GS.return_value.get_credential_state.return_value = (False, None, "tok")
        visible = {s["space_id"] for s in ctx.visible_spaces()}

    assert visible == {"pub", "priv", "zip"}


# An expired token must hide only private spaces, exactly like having no token.
def _patched_expired(**store_state):
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("src.authz.repo_access.require_authorization", return_value=True))
    stack.enter_context(patch("src.authz.repo_access._kms_configured", return_value=True))
    stack.enter_context(
        patch("src.authz.repo_access.provider_for_host", side_effect=lambda h: object() if h else None)
    )
    GS = stack.enter_context(patch("src.authz.repo_access.GitCredentialStore"))
    GS.return_value.get_credential_state.return_value = store_state["state"]
    lister = stack.enter_context(
        patch("src.authz.repo_access.list_accessible_repos", **store_state.get("lister", {}))
    )
    return stack, GS, lister


SPACES_TWO_PRIVATE = SPACES + [
    {
        "space_id": "priv2",
        "visibility": "private",
        "git_url": "https://github.com/o/priv2.git",
        "provider": "github",
        "repo_key": "github.com/o/priv2",
    },
]


class FakeSvcTwoPrivate(FakeSvc):
    def list_spaces(self):
        return list(SPACES_TWO_PRIVATE)


def test_visible_spaces_flagged_expired_token_still_lists_public():
    get_cache().clear()
    ctx = AuthContext(sub="list-exp", email="u@x.com", service=FakeSvcTwoPrivate())
    stack, GS, lister = _patched_expired(state=(True, None, None))
    with stack:
        visible = {s["space_id"] for s in ctx.visible_spaces()}
    assert visible == {"pub", "zip"}
    lister.assert_not_called()
    # The first expiry is remembered for the request: no store read per private space.
    assert GS.return_value.get_credential_state.call_count == 1


def test_visible_spaces_provider_rejects_token_still_lists_public():
    from src.gitcreds.providers import TokenInvalid

    get_cache().clear()
    ctx = AuthContext(sub="list-401", email="u@x.com", service=FakeSvcTwoPrivate())
    stack, GS, lister = _patched_expired(
        state=(False, None, "tok"), lister={"side_effect": TokenInvalid("bad")}
    )
    with stack:
        visible = {s["space_id"] for s in ctx.visible_spaces()}
    assert visible == {"pub", "zip"}
    GS.return_value.mark_invalid.assert_called_once_with("list-401", "github")
    assert lister.call_count == 1


def test_visible_spaces_provider_down_still_fails():
    import pytest

    from src.authz.errors import AuthorizationUnavailable
    from src.gitcreds.providers import ProviderError

    get_cache().clear()
    ctx = AuthContext(sub="list-503", email="u@x.com", service=FakeSvc())
    stack, _, _ = _patched_expired(
        state=(False, None, "tok"), lister={"side_effect": ProviderError("down")}
    )
    with stack, pytest.raises(AuthorizationUnavailable):
        ctx.visible_spaces()
