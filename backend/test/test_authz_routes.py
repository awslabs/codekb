"""Phase 2 (§6.2.12): the gates are wired to the routes and denials map to the
right HTTP status + body error code, exercised end-to-end through the ASGI stack.

A minimal app mounts the real spaces router + the AuthzError handler. Auth is
bypassed to a local-dev identity (_REQUIRE_AUTH off) and the authz seams are
flipped on, so we test routing/wiring without the Cognito-misconfigured
middleware the full app registers in tests.
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import src.api.auth as auth_mod
from src.api.app import authz_exception_handler
from src.api.routes import spaces
from src.authz.cache import get_cache
from src.authz.errors import AuthzError

PRIVATE = {
    "space_id": "s1",
    "visibility": "private",
    "git_url": "https://github.com/o/r.git",
    "provider": "github",
    "repo_key": "github.com/o/r",
}
PUBLIC = {
    "space_id": "s2",
    "visibility": "public",
    "git_url": "https://github.com/o/pub.git",
}
# A full SpaceDetail so the allow path passes response-model validation.
DETAIL = {
    "space_id": "s1",
    "source_directory": "/tmp/s1",
    "component_count": 0,
    "version": "1.0",
    "statistics": {},
    "git_url": "https://github.com/o/r.git",
}


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()
    app.include_router(spaces.router, prefix="/api/spaces")
    app.add_exception_handler(AuthzError, authz_exception_handler)
    return TestClient(app, raise_server_exceptions=False)


class _Store:
    def get_credential_state(self, sub, provider):
        # (invalid, host, token): valid token present for the host.
        return False, None, "tok"

    def mark_invalid(self, sub, provider):
        return True


def _env(record, listed=None, create_visibility="private"):
    """Enter a context with auth off + authz seams on.

    ``record`` is what the gate's read_status returns; ``listed`` is the
    caller's accessible repos (keyed by repo_key). The route handler's
    get_space is stubbed to a valid detail so the allow path returns 200.
    """
    get_cache().clear()
    svc = MagicMock()
    svc.read_status.return_value = record
    svc.get_space.return_value = DETAIL
    stack = ExitStack()
    stack.enter_context(patch.object(auth_mod, "_REQUIRE_AUTH", False))
    stack.enter_context(patch("src.api.gates.require_authorization", return_value=True))
    stack.enter_context(
        patch("src.service.codekb_service.CodeKBService.instance", return_value=svc)
    )
    stack.enter_context(
        patch("src.api.gates.lookup_repo_visibility", return_value=create_visibility)
    )
    stack.enter_context(patch("src.api.gates.validate_git_url", side_effect=lambda u: u))
    stack.enter_context(patch("src.authz.repo_access.require_authorization", return_value=True))
    stack.enter_context(patch("src.authz.repo_access._kms_configured", return_value=True))
    stack.enter_context(
        patch(
            "src.authz.repo_access.provider_for_host",
            side_effect=lambda h: object() if h else None,
        )
    )
    stack.enter_context(patch("src.authz.repo_access.GitCredentialStore", return_value=_Store()))
    stack.enter_context(
        patch("src.authz.repo_access.list_accessible_repos", return_value=dict(listed or {}))
    )
    return stack


def test_read_route_denies_private_no_access_401():
    with _env(PRIVATE, listed={}):
        resp = _client().get("/api/spaces/s1")
    assert resp.status_code == 401
    assert resp.json()["error"] == "read_denied"


def test_write_route_denies_readable_but_unwritable_403():
    # Public space (readable by any signed-in user) but caller isn't the creator
    # and has no git write access → write_denied.
    with _env(PUBLIC, listed={}):
        resp = _client().delete("/api/spaces/s2")
    assert resp.status_code == 403
    assert resp.json()["error"] == "write_denied"


def test_create_route_denies_repo_read_403():
    with _env(PRIVATE, listed={}, create_visibility="private"):
        resp = _client().post("/api/spaces", json={"git_url": "https://github.com/o/r.git"})
    assert resp.status_code == 403
    assert resp.json()["error"] == "repo_read_denied"


def test_read_route_allows_when_caller_has_access_200():
    # Caller can pull the repo → gate passes and the handler returns the detail.
    with _env(PRIVATE, listed={"github.com/o/r": {"pull": True}}):
        resp = _client().get("/api/spaces/s1")
    assert resp.status_code == 200
    assert resp.json()["space_id"] == "s1"
