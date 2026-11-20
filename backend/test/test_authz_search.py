"""Phase 3 (§6.3): the global /search route scopes to readable spaces.

A minimal app mounts the real search router + the AuthzError handler. Auth is
bypassed and the authz seams are patched, so we test the route's wiring:
resolve_scope decides what gets searched, an empty scope searches nothing, and an
expired token fails the request.
"""

from unittest.mock import MagicMock, patch

import src.api.auth as auth_mod
from src.api.app import authz_exception_handler
from src.api.routes import search
from src.authz.errors import AuthzError, CredentialExpired


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()
    app.include_router(search.router, prefix="/api/search")
    app.add_exception_handler(AuthzError, authz_exception_handler)
    return TestClient(app, raise_server_exceptions=False)


def _ctx():
    return MagicMock()


def _env(svc, *, ctx=None, resolve=None, resolve_exc=None):
    patches = [
        patch.object(auth_mod, "_REQUIRE_AUTH", False),
        patch("src.api.routes.search.require_authorization", return_value=True),
        patch("src.api.routes.search.current_auth_context", return_value=ctx or _ctx()),
        patch("src.service.codekb_service.CodeKBService.instance", return_value=svc),
    ]
    if resolve_exc is not None:
        patches.append(patch("src.api.routes.search.resolve_scope", side_effect=resolve_exc))
    else:
        patches.append(patch("src.api.routes.search.resolve_scope", return_value=resolve))
    return patches


def _enter(patches):
    for p in patches:
        p.start()


def _exit(patches):
    for p in patches:
        p.stop()


def test_search_scopes_to_readable_spaces():
    # Caller can read 2 of the requested spaces → search runs over exactly those.
    svc = MagicMock()
    svc.global_semantic_search.return_value = [{"key": "c1"}]
    patches = _env(svc, resolve=["s1", "s2"])
    _enter(patches)
    try:
        resp = _client().get("/api/search?q=payments")
    finally:
        _exit(patches)
    assert resp.status_code == 200
    assert resp.json() == [{"key": "c1"}]
    svc.global_semantic_search.assert_called_once_with("payments", None, ["s1", "s2"])


def test_search_empty_scope_returns_empty_without_querying():
    # Caller can read nothing → no vector query at all.
    svc = MagicMock()
    patches = _env(svc, resolve=[])
    _enter(patches)
    try:
        resp = _client().get("/api/search?q=payments")
    finally:
        _exit(patches)
    assert resp.status_code == 200
    assert resp.json() == []
    svc.global_semantic_search.assert_not_called()


def test_search_expired_token_fails_401():
    svc = MagicMock()
    patches = _env(svc, resolve_exc=CredentialExpired("github"))
    _enter(patches)
    try:
        resp = _client().get("/api/search?q=payments")
    finally:
        _exit(patches)
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"] == "git_credentials_expired"
    assert body["provider"] == "github"
    svc.global_semantic_search.assert_not_called()
