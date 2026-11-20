"""Regression tests: the fail-closed auth block must cover every MCP path, and a
misconfigured server must never accept an unverified identity.


"""

import base64
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from .test_auth_audience_validation import POOL_ID, auth_module

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


# An unsigned token claiming an identity. Only a verifier can reject it.
FORGED_TOKEN = (
    _b64({"alg": "none", "typ": "JWT"})
    + "."
    + _b64({"email": "attacker@example.com", "sub": "attacker"})
    + ".sig"
)


def _request(headers: dict):
    from starlette.requests import Request

    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/mcp", "headers": raw})


# ── Path classification ────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/mcp/mcp", "/mcp/anything/else"])
def test_mcp_paths_are_protected(path):
    from src.api.auth import is_mcp_path, is_protected_path

    assert is_mcp_path(path)
    assert is_protected_path(path)


@pytest.mark.parametrize("path", ["/api/spaces", "/api/health", "/api/"])
def test_api_paths_are_protected(path):
    from src.api.auth import is_mcp_path, is_protected_path

    assert not is_mcp_path(path)
    assert is_protected_path(path)


@pytest.mark.parametrize(
    "path", ["/", "/mcpx", "/mcp-docs", "/api", "/spaces/foo", "/oauth2/token"]
)
def test_unprotected_paths(path):
    from src.api.auth import is_protected_path

    assert not is_protected_path(path)


# ── Identity resolution when auth is required but cannot be enforced ───────


@pytest.fixture
def _require_jwt():
    pytest.importorskip("jwt")


@pytest.mark.parametrize(
    "headers",
    [
        {"x-user-identity": "attacker@example.com"},
        {"authorization": f"Bearer {FORGED_TOKEN}"},
        {"authorization": FORGED_TOKEN},  # bare JWT, no scheme
    ],
    ids=["identity-header", "bearer-unsigned", "bare-unsigned"],
)
def test_misconfigured_rejects_unverified_identity(_require_jwt, headers):
    """No pool + auth required: nothing the caller sends establishes identity."""
    with auth_module() as auth:
        assert auth.is_auth_misconfigured() is True
        assert auth.get_current_user(_request(headers)) is None


def test_misconfigured_require_current_user_raises_401(_require_jwt):
    from fastapi import HTTPException

    with auth_module() as auth:
        with pytest.raises(HTTPException) as exc:
            auth.require_current_user(_request({"x-user-identity": "attacker@example.com"}))
        assert exc.value.status_code == 401


def test_jwks_init_failure_rejects_unverified_identity(_require_jwt, monkeypatch):
    """Pool set but JWKS client failed to build → same as no verifier."""
    import jwt

    def _boom(*args, **kwargs):
        raise RuntimeError("jwks unavailable")

    monkeypatch.setattr(jwt, "PyJWKClient", _boom)
    with auth_module(CODEKB_COGNITO_USER_POOL_ID=POOL_ID) as auth:
        assert auth.is_auth_enforced() is False
        assert auth.is_auth_misconfigured() is True
        assert auth.get_current_user(_request({"x-user-identity": "attacker@example.com"})) is None
        assert auth.get_current_user(_request({"authorization": f"Bearer {FORGED_TOKEN}"})) is None


def test_no_auth_mode_still_accepts_local_dev_identity(_require_jwt):
    """Explicit opt-out (local dev) keeps the documented convenience fallbacks."""
    with auth_module(CODEKB_REQUIRE_AUTH="false") as auth:
        user = auth.get_current_user(_request({"x-user-identity": "dev@example.com"}))
        assert user is not None and user.email == "dev@example.com"
        user = auth.get_current_user(_request({"authorization": f"Bearer {FORGED_TOKEN}"}))
        assert user is not None and user.email == "attacker@example.com"


# ── End to end: the real app in misconfigured mode ──────────────────────────
#
# app.py installs its middlewares at import time from auth state, so this runs
# in a fresh interpreter rather than reloading the app inside the test process.

_E2E_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from starlette.testclient import TestClient
    from src.api import auth
    from src.api.app import app

    assert auth.is_auth_misconfigured() and not auth.is_auth_enforced()
    forged = sys.argv[1]
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "list_spaces", "arguments": {}}}
    hdr = {"accept": "application/json, text/event-stream",
           "content-type": "application/json"}
    results = {}
    with TestClient(app) as c:
        for path in ["/mcp", "/mcp/", "/mcp/mcp", "/api/spaces"]:
            for name, extra in [("header", {"x-user-identity": "attacker@example.com"}),
                                ("token", {"authorization": "Bearer " + forged})]:
                r = c.post(path, json=body, headers={**hdr, **extra},
                           follow_redirects=False)
                results[f"POST {path} {name}"] = r.status_code
        results["GET /api/health"] = c.get("/api/health").status_code
    print("RESULTS=" + json.dumps(results))
    """
)


def test_misconfigured_app_blocks_every_mcp_path():
    pytest.importorskip("starlette.testclient")
    pytest.importorskip("jwt")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CODEKB_COGNITO_") and k != "CODEKB_REQUIRE_AUTH"
    }
    env.update(
        CODEKB_REQUIRE_AUTHENTICATION="true",
        PYTHONPATH=str(BACKEND_DIR),
        OTEL_LOGS_EXPORTER=env.get("OTEL_LOGS_EXPORTER", "none"),
        CODEKB_BEDROCK_GUARDRAIL_ENABLED=env.get("CODEKB_BEDROCK_GUARDRAIL_ENABLED", "false"),
        CODEKB_LLM_STARTUP_CHECK=env.get("CODEKB_LLM_STARTUP_CHECK", "false"),
    )
    proc = subprocess.run(
        [sys.executable, "-c", _E2E_SCRIPT, FORGED_TOKEN],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULTS=")), None)
    assert line, f"probe failed (rc={proc.returncode}):\n{proc.stderr[-3000:]}"
    results = json.loads(line[len("RESULTS=") :])

    assert results.pop("GET /api/health") == 200, "health must stay reachable for LBs"
    not_blocked = {k: v for k, v in results.items() if v != 503}
    assert not not_blocked, f"expected 503 on every protected path, got {not_blocked}"
