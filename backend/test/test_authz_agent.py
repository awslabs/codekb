"""Phase 3 (§6.3): the agent chat route scopes to readable spaces before the
model runs. An empty readable scope answers without calling the model (and never
falls through to "all spaces"); an expired token fails the request.
"""

import threading
from unittest.mock import MagicMock, patch

import src.api.auth as auth_mod
from src.api.app import authz_exception_handler
from src.api.routes import agent
from src.authz.errors import AuthzError, CredentialExpired


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()
    app.include_router(agent.router, prefix="/api/agent")
    app.add_exception_handler(AuthzError, authz_exception_handler)
    return TestClient(app, raise_server_exceptions=False)


def _ctx():
    return MagicMock()


def _patches(run_mock, *, ctx=None, resolve=None, resolve_exc=None):
    ps = [
        patch.object(auth_mod, "_REQUIRE_AUTH", False),
        patch("src.api.routes.agent.require_authorization", return_value=True),
        patch("src.api.routes.agent.current_auth_context", return_value=ctx or _ctx()),
        patch("src.api.routes.agent.pricing_url", return_value="http://pricing"),
        patch("src.api.routes.agent.run_agent_query", run_mock),
    ]
    if resolve_exc is not None:
        ps.append(patch("src.api.routes.agent.resolve_scope", side_effect=resolve_exc))
    else:
        ps.append(patch("src.api.routes.agent.resolve_scope", return_value=resolve))
    return ps


def _run(ps):
    for p in ps:
        p.start()


def _stop(ps):
    for p in ps:
        p.stop()


def test_agent_runs_over_resolved_scope():
    run_mock = MagicMock(return_value=("the answer", [{"role": "user", "content": "hi"}], {}))
    ps = _patches(run_mock, resolve=["s1"])
    _run(ps)
    try:
        resp = _client().post("/api/agent/chat", json={"message": "hi"})
    finally:
        _stop(ps)
    assert resp.status_code == 200
    body = resp.json()
    assert body["response"] == "the answer"
    assert body["space_ids"] == ["s1"]
    run_mock.assert_called_once_with(["s1"], "hi", mode="chat", history=None)


def test_agent_explicit_scope_refuses_when_incomplete():
    # An explicit scope is all-or-nothing: one unreadable member → refuse (names it),
    # the model never runs. This is the backend enforcement of the hyperspace rule.
    run_mock = MagicMock()
    ps = _patches(run_mock, resolve=["s1"])  # requested s1,s2 but only s1 readable
    _run(ps)
    try:
        resp = _client().post("/api/agent/chat", json={"message": "hi", "space_ids": ["s1", "s2"]})
    finally:
        _stop(ps)
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"] == "read_denied"
    assert "s2" in body["message"]
    run_mock.assert_not_called()


def test_agent_empty_scope_answers_without_model():
    run_mock = MagicMock()
    ps = _patches(run_mock, resolve=[])
    _run(ps)
    try:
        resp = _client().post("/api/agent/chat", json={"message": "hi"})
    finally:
        _stop(ps)
    assert resp.status_code == 200
    body = resp.json()
    assert body["space_ids"] == []
    assert "access" in body["response"].lower()
    run_mock.assert_not_called()


def test_agent_expired_token_fails_401():
    run_mock = MagicMock()
    ps = _patches(run_mock, resolve_exc=CredentialExpired("gitlab"))
    _run(ps)
    try:
        resp = _client().post("/api/agent/chat", json={"message": "hi"})
    finally:
        _stop(ps)
    assert resp.status_code == 401
    assert resp.json()["error"] == "git_credentials_expired"
    run_mock.assert_not_called()


def test_agent_chat_does_not_block_the_event_loop():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    started = threading.Event()
    release = threading.Event()

    def slow_agent(*args, **kwargs):
        started.set()
        release.wait(10)
        return ("done", [], {})

    app = FastAPI()
    app.include_router(agent.router, prefix="/api/agent")
    app.add_exception_handler(AuthzError, authz_exception_handler)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    ps = _patches(MagicMock(side_effect=slow_agent), resolve=["s1"])
    _run(ps)
    results = {}
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            chat = threading.Thread(
                target=lambda: results.update(
                    chat=client.post("/api/agent/chat", json={"message": "hi"})
                )
            )
            chat.start()
            assert started.wait(5), "agent call never started"
            pinger = threading.Thread(target=lambda: results.update(ping=client.get("/ping")))
            pinger.start()
            pinger.join(2)
            ping_answered_during_agent_run = "ping" in results
            release.set()
            chat.join(10)
            pinger.join(10)
    finally:
        release.set()
        _stop(ps)
    assert ping_answered_during_agent_run, "a running agent call blocked the event loop"
    assert results["ping"].status_code == 200
    assert results["chat"].status_code == 200
