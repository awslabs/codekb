"""Smoke test for the FastAPI application.

We only check that the app imports without exploding and that /api/health
responds. Full route-level coverage belongs in a later phase; this test
exists to catch wiring regressions (missing routers, import cycles, startup
errors) early.
"""

import pytest


@pytest.fixture(scope="module")
def client():
    """Construct the FastAPI TestClient, skipping if starlette isn't around."""
    starlette = pytest.importorskip("starlette.testclient")
    from src.api.app import app  # import lazily to avoid failing collection

    with starlette.TestClient(app) as test_client:
        yield test_client


def test_health_endpoint_returns_ok(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    # Be permissive about exact shape; we only care that the endpoint is live.
    assert isinstance(body, dict)
