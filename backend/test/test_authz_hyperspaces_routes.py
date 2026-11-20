"""Phase 4 (§6.4): the hyperspace routes are gated.

Minimal app mounts the real hyperspaces router + the AuthzError handler. We test
the wiring: read-overview needs READ on every member, generate-overview needs
WRITE, and update/delete need the owner.
"""

from unittest.mock import MagicMock, patch

import src.api.auth as auth_mod
from src.api.app import authz_exception_handler
from src.api.routes import hyperspaces
from src.authz.errors import AuthzError, HyperspaceAccessDenied, ReadDenied


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()
    app.include_router(hyperspaces.router, prefix="/api/hyperspaces")
    app.add_exception_handler(AuthzError, authz_exception_handler)
    return TestClient(app, raise_server_exceptions=False)


def _ctx(sub="u1"):
    ctx = MagicMock()
    ctx.sub = sub
    ctx.space_status.side_effect = lambda sid: {"space_id": sid, "visibility": "private"}
    return ctx


def _svc(cfg):
    svc = MagicMock()
    svc.get_hyperspace.return_value = cfg
    return svc


def _base_patches(svc, ctx):
    return [
        patch.object(auth_mod, "_REQUIRE_AUTH", False),
        patch("src.api.routes.hyperspaces.require_authorization", return_value=True),
        patch("src.api.routes.hyperspaces.current_auth_context", return_value=ctx),
        patch("src.service.codekb_service.CodeKBService.instance", return_value=svc),
    ]


def _run(patches):
    for p in patches:
        p.start()


def _stop(patches):
    for p in patches:
        p.stop()


CFG = {"hyperspace_id": "h1", "space_ids": ["s1", "s2"], "created_by_sub": "owner"}


def test_read_overview_denied_names_members_403():
    svc = _svc(CFG)
    patches = _base_patches(svc, _ctx()) + [
        patch(
            "src.api.routes.hyperspaces.hyperspace_authz.check_read",
            side_effect=HyperspaceAccessDenied("nope", inaccessible_space_ids=["s2"]),
        )
    ]
    _run(patches)
    try:
        resp = _client().get("/api/hyperspaces/h1/overview/system_overview")
    finally:
        _stop(patches)
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"] == "hyperspace_access_denied"
    assert body["inaccessible_space_ids"] == ["s2"]
    svc.get_hyperspace_overview_document.assert_not_called()


def test_read_overview_allowed_returns_markdown():
    svc = _svc(CFG)
    svc.get_hyperspace_overview_document.return_value = "# overview"
    patches = _base_patches(svc, _ctx()) + [
        patch("src.api.routes.hyperspaces.hyperspace_authz.check_read", return_value=None)
    ]
    _run(patches)
    try:
        resp = _client().get("/api/hyperspaces/h1/overview/system_overview")
    finally:
        _stop(patches)
    assert resp.status_code == 200
    assert resp.text == "# overview"


def test_overview_read_denied_when_all_members_deleted():
    # Every member deleted → empty member set → fail closed (don't serve the
    # previously-generated overview to anyone).
    svc = _svc(CFG)
    ctx = _ctx()
    ctx.space_status.side_effect = lambda sid: None
    patches = _base_patches(svc, ctx)
    _run(patches)
    try:
        resp = _client().get("/api/hyperspaces/h1/overview/system_overview")
    finally:
        _stop(patches)
    assert resp.status_code == 403
    assert resp.json()["error"] == "hyperspace_access_denied"
    svc.get_hyperspace_overview_document.assert_not_called()


def test_generate_overview_denied_403():
    svc = _svc(CFG)
    patches = _base_patches(svc, _ctx()) + [
        patch(
            "src.api.routes.hyperspaces.hyperspace_authz.check_write",
            side_effect=HyperspaceAccessDenied("nope", inaccessible_space_ids=["s2"]),
        )
    ]
    _run(patches)
    try:
        resp = _client().post("/api/hyperspaces/h1/generate-overview")
    finally:
        _stop(patches)
    assert resp.status_code == 403
    assert resp.json()["error"] == "hyperspace_access_denied"
    svc.start_hyperspace_overview_generation.assert_not_called()


def test_delete_non_owner_403():
    svc = _svc(CFG)
    patches = _base_patches(svc, _ctx()) + [
        patch("src.api.routes.hyperspaces.repo_access.is_space_owner", return_value=False)
    ]
    _run(patches)
    try:
        resp = _client().delete("/api/hyperspaces/h1")
    finally:
        _stop(patches)
    assert resp.status_code == 403
    assert resp.json()["error"] == "write_denied"
    svc.delete_hyperspace.assert_not_called()


def test_delete_owner_allowed():
    svc = _svc(CFG)
    svc.delete_hyperspace.return_value = {"hyperspace_id": "h1", "deleted": True}
    patches = _base_patches(svc, _ctx(sub="owner")) + [
        patch("src.api.routes.hyperspaces.repo_access.is_space_owner", return_value=True)
    ]
    _run(patches)
    try:
        resp = _client().delete("/api/hyperspaces/h1")
    finally:
        _stop(patches)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    svc.delete_hyperspace.assert_called_once_with("h1")


def _deny(sids):
    """can_read side effect: ReadDenied for the given member ids, else allow."""

    def _impl(ctx, record):
        if record.get("space_id") in sids:
            raise ReadDenied()

    return _impl


def test_get_hyperspace_annotates_inaccessible_members():
    svc = _svc(CFG)
    patches = _base_patches(svc, _ctx()) + [
        patch("src.api.routes.hyperspaces.repo_access.can_read", side_effect=_deny({"s2"}))
    ]
    _run(patches)
    try:
        resp = _client().get("/api/hyperspaces/h1")
    finally:
        _stop(patches)
    assert resp.status_code == 200
    assert resp.json()["inaccessible_space_ids"] == ["s2"]


def test_list_hyperspaces_annotates_inaccessible_members():
    svc = _svc(CFG)
    svc.list_hyperspaces.return_value = [dict(CFG)]
    patches = _base_patches(svc, _ctx()) + [
        patch("src.api.routes.hyperspaces.repo_access.can_read", side_effect=_deny({"s2"}))
    ]
    _run(patches)
    try:
        resp = _client().get("/api/hyperspaces")
    finally:
        _stop(patches)
    assert resp.status_code == 200
    assert resp.json()[0]["inaccessible_space_ids"] == ["s2"]


def test_inaccessible_empty_when_all_readable():
    svc = _svc(CFG)
    patches = _base_patches(svc, _ctx()) + [
        patch("src.api.routes.hyperspaces.repo_access.can_read", return_value=None)
    ]
    _run(patches)
    try:
        resp = _client().get("/api/hyperspaces/h1")
    finally:
        _stop(patches)
    assert resp.status_code == 200
    assert resp.json()["inaccessible_space_ids"] == []


def test_transient_error_during_annotation_fails_loud_not_marked():
    # A down provider must surface as the transient error, not be recorded as a
    # permanent "inaccessible" marker (matches can_read_bool / visible_spaces).
    from src.authz.errors import AuthorizationUnavailable

    svc = _svc(CFG)
    patches = _base_patches(svc, _ctx()) + [
        patch(
            "src.api.routes.hyperspaces.repo_access.can_read",
            side_effect=AuthorizationUnavailable(provider="github"),
        )
    ]
    _run(patches)
    try:
        resp = _client().get("/api/hyperspaces/h1")
    finally:
        _stop(patches)
    assert resp.status_code == 503
    assert resp.json()["error"] == "authorization_unavailable"
