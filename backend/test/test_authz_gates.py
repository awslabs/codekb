"""Phase 2 (b): the route gates and the AuthzError → HTTP mapping."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.api.app import authz_exception_handler
from src.api.gates import gate_read, gate_repo_read, gate_write
from src.authz.errors import (
    AuthorizationUnavailable,
    CredentialExpired,
    HyperspaceAccessDenied,
    ReadDenied,
    RepoReadDenied,
    WriteDenied,
)

GIT_URL = "https://github.com/octo/repo.git"


def _req():
    return MagicMock(name="request")


def _ctx():
    return SimpleNamespace(sub="sub-1", email="u@x.com")


# ── gate_read / gate_write ─────────────────────────────────────────────────


def test_gate_read_is_noop_when_off():
    with patch("src.api.gates.require_authorization", return_value=False):
        assert gate_read("sid", _req()) is None  # no exception, no service touch


def test_gate_read_missing_space_is_denied():
    with (
        patch("src.api.gates.require_authorization", return_value=True),
        patch("src.api.gates.current_auth_context", return_value=_ctx()),
        patch("src.service.codekb_service.CodeKBService.instance") as gs,
    ):
        gs.return_value.read_status.return_value = None
        with pytest.raises(ReadDenied):
            gate_read("sid", _req())


def test_gate_read_delegates_to_can_read():
    record = {"space_id": "sid", "visibility": "private"}
    with (
        patch("src.api.gates.require_authorization", return_value=True),
        patch("src.api.gates.current_auth_context", return_value="CTX"),
        patch("src.service.codekb_service.CodeKBService.instance") as gs,
        patch("src.api.gates.repo_access.can_read") as can_read,
    ):
        gs.return_value.read_status.return_value = record
        gate_read("sid", _req())
        can_read.assert_called_once_with("CTX", record)


def test_gate_write_delegates_to_can_write():
    record = {"space_id": "sid"}
    with (
        patch("src.api.gates.require_authorization", return_value=True),
        patch("src.api.gates.current_auth_context", return_value="CTX"),
        patch("src.service.codekb_service.CodeKBService.instance") as gs,
        patch("src.api.gates.repo_access.can_write") as can_write,
    ):
        gs.return_value.read_status.return_value = record
        gate_write("sid", _req())
        can_write.assert_called_once_with("CTX", record)


# ── gate_repo_read (create) ─────────────────────────────────────────────────


def test_gate_repo_read_off_returns_none():
    with patch("src.api.gates.require_authorization", return_value=False):
        assert gate_repo_read(_req(), GIT_URL) is None


def test_gate_repo_read_public_passes_and_returns_visibility():
    with (
        patch("src.api.gates.require_authorization", return_value=True),
        patch("src.api.gates.validate_git_url", side_effect=lambda u: u),
        patch("src.api.gates.current_auth_context", return_value=_ctx()),
        patch("src.api.gates.lookup_repo_visibility", return_value="public"),
        patch("src.api.gates.repo_access.can_read"),
    ):
        assert gate_repo_read(_req(), GIT_URL) == "public"


def test_gate_repo_read_denied_maps_to_repo_read_denied():
    with (
        patch("src.api.gates.require_authorization", return_value=True),
        patch("src.api.gates.validate_git_url", side_effect=lambda u: u),
        patch("src.api.gates.current_auth_context", return_value=_ctx()),
        patch("src.api.gates.lookup_repo_visibility", return_value="private"),
        patch("src.api.gates.repo_access.can_read", side_effect=ReadDenied()),
    ):
        with pytest.raises(RepoReadDenied):
            gate_repo_read(_req(), GIT_URL)


def test_gate_repo_read_validates_url_before_probing():
    # SSRF guard: the host allowlist runs before any outbound visibility probe.
    from src.service.codekb_service import ServiceError

    with (
        patch("src.api.gates.require_authorization", return_value=True),
        patch("src.api.gates.validate_git_url", side_effect=ServiceError("host not allowed")),
        patch("src.api.gates.lookup_repo_visibility") as probe,
    ):
        with pytest.raises(ServiceError):
            gate_repo_read(_req(), "https://169.254.169.254/x.git")
        probe.assert_not_called()


# ── exception handler mapping (plan §3) ─────────────────────────────────────


def _handle(exc):
    resp = asyncio.run(authz_exception_handler(_req(), exc))
    return resp, json.loads(resp.body)


def test_handler_read_denied_is_401():
    resp, body = _handle(ReadDenied())
    assert resp.status_code == 401
    assert body["error"] == "read_denied"


def test_handler_write_denied_is_403():
    resp, body = _handle(WriteDenied())
    assert resp.status_code == 403
    assert body["error"] == "write_denied"


def test_handler_credential_expired_carries_provider_and_update_url():
    resp, body = _handle(CredentialExpired("github"))
    assert resp.status_code == 401
    assert body["error"] == "git_credentials_expired"
    assert body["provider"] == "github"
    assert "git-creds" in body["update_url"]


def test_handler_unavailable_is_503_with_retry_after():
    resp, body = _handle(AuthorizationUnavailable())
    assert resp.status_code == 503
    assert body["error"] == "authorization_unavailable"
    assert resp.headers["Retry-After"]


def test_handler_hyperspace_lists_inaccessible_ids():
    resp, body = _handle(HyperspaceAccessDenied("msg", inaccessible_space_ids=["a", "b"]))
    assert resp.status_code == 403
    assert body["inaccessible_space_ids"] == ["a", "b"]
