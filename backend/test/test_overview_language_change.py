"""Changing a space's overview language when regenerating its Overview.

``start_overview_generation(space_id, overview_language=...)`` stores the new
language in the same conditional claim write that starts the run, so the run
and later runs use it. Omitting it keeps the stored setting, and a refused
request leaves the setting unchanged.
"""

import json
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from src.service.codekb_service import CodeKBService, OverviewInProgressError


def _seed_status(svc: CodeKBService, space_id: str, **extra) -> None:
    bucket = svc.kb_manager._get_s3_bucket()
    doc = {"space_id": space_id, "status": "READY", "space_name": space_id, **extra}
    svc.kb_manager.s3_client.put_object(
        Bucket=bucket,
        Key=f"spaces/{space_id}/status.json",
        Body=json.dumps(doc).encode("utf-8"),
        ContentType="application/json",
    )


class _RecordingWorker:
    """Stub overview worker that records the language the run resolves."""

    def __init__(self) -> None:
        self.resolved: list[str] = []

    def __call__(self, space_id):
        svc = CodeKBService.instance()
        self.resolved.append(svc._resolve_overview_language(space_id).resolved_language_code)


@pytest.fixture
def worker(monkeypatch):
    w = _RecordingWorker()
    monkeypatch.setattr(CodeKBService, "_run_space_overview", w)
    return w


def _wait_for_run(worker: _RecordingWorker, count: int = 1) -> None:
    import time

    deadline = time.monotonic() + 5.0
    while len(worker.resolved) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(worker.resolved) >= count, "overview worker never ran"


def test_language_set_on_space_created_without_one(worker):
    svc = CodeKBService.instance()
    _seed_status(svc, "lc1")

    svc.start_overview_generation("lc1", overview_language="ja")
    _wait_for_run(worker)

    assert svc._read_status("lc1")["overview_language"] == "ja"
    assert worker.resolved == ["ja"]


def test_language_replaces_existing_setting(worker):
    svc = CodeKBService.instance()
    _seed_status(svc, "lc2", overview_language="ja", analysis_language="ja")

    svc.start_overview_generation("lc2", overview_language="en")
    _wait_for_run(worker)

    status = svc._read_status("lc2")
    assert status["overview_language"] == "en"
    # Only the overview language changes; analysis language is untouched.
    assert status["analysis_language"] == "ja"
    assert worker.resolved == ["en"]


def test_omitted_language_keeps_stored_setting(worker):
    svc = CodeKBService.instance()
    _seed_status(svc, "lc3", overview_language="ja")

    svc.start_overview_generation("lc3")
    _wait_for_run(worker)

    assert svc._read_status("lc3")["overview_language"] == "ja"
    assert worker.resolved == ["ja"]


def test_omitted_language_on_unset_space_stays_absent(worker):
    svc = CodeKBService.instance()
    _seed_status(svc, "lc4")

    svc.start_overview_generation("lc4")
    _wait_for_run(worker)

    assert "overview_language" not in svc._read_status("lc4")
    assert worker.resolved == ["en"]


def test_refused_request_does_not_change_language(monkeypatch):
    svc = CodeKBService.instance()
    from datetime import datetime, timezone

    _seed_status(
        svc,
        "lc5",
        overview_language="en",
        overview_status="in_progress",
        updated_at=datetime.now(timezone.utc).isoformat(),
    )

    with pytest.raises(OverviewInProgressError):
        svc.start_overview_generation("lc5", overview_language="ja")

    assert svc._read_status("lc5")["overview_language"] == "en"


def test_route_stores_language_and_accepts_empty_body(worker):
    import src.api.auth as auth_mod
    from fastapi import FastAPI

    from src.api.routes import analysis

    svc = CodeKBService.instance()
    _seed_status(svc, "lc6")
    app = FastAPI()
    app.include_router(analysis.router, prefix="/api/spaces")
    client = TestClient(app, raise_server_exceptions=False)
    with (
        patch.object(auth_mod, "_REQUIRE_AUTH", False),
        patch("src.api.gates.require_authorization", return_value=False),
        patch("src.api.routes.analysis.overview_generation_enabled", return_value=True),
    ):
        r = client.post("/api/spaces/lc6/generate-overview", json={"overview_language": "ja"})
        assert r.status_code == 202, r.text
        _wait_for_run(worker, 1)
        assert svc._read_status("lc6")["overview_language"] == "ja"

        # The status record is left in_progress by the stub worker; mark the
        # run finished so the second request is not refused as a duplicate.
        status = svc._read_status("lc6")
        status["overview_status"] = "completed"
        svc._write_status("lc6", status)

        r = client.post("/api/spaces/lc6/generate-overview")
        assert r.status_code == 202, r.text
        _wait_for_run(worker, 2)
        assert svc._read_status("lc6")["overview_language"] == "ja"

    assert worker.resolved == ["ja", "ja"]
