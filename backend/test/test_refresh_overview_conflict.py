"""A refresh waits for a running Space Overview generation, which keeps the Space READY."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.service.codekb_service import CodeKBService, ServiceError


def _seed(space_id: str, overview_heartbeat: datetime) -> CodeKBService:
    svc = CodeKBService.instance()
    svc.kb_manager.s3_client.put_object(
        Bucket=svc.kb_manager._get_s3_bucket(),
        Key=f"spaces/{space_id}/status.json",
        Body=json.dumps(
            {
                "status": "READY",
                "git_url": "https://example.com/r.git",
                "git_commit_hash": "old",
                "overview_status": "in_progress",
                "updated_at": overview_heartbeat.isoformat(),
            }
        ).encode("utf-8"),
        ContentType="application/json",
    )
    return svc


def test_a_refresh_is_refused_while_an_overview_generates(monkeypatch):
    svc = _seed("refresh-during-overview", datetime.now(timezone.utc))
    monkeypatch.setattr("src.service.codekb_service.build_git_env", lambda url, sub: {})
    worker = MagicMock()
    monkeypatch.setattr(svc, "_run_refresh_pipeline", worker)

    with pytest.raises(ServiceError) as exc_info:
        svc.refresh_space("refresh-during-overview")

    assert exc_info.value.status_code == 409
    assert "Space Overview" in str(exc_info.value)
    worker.assert_not_called()
    status = svc._read_status("refresh-during-overview")
    assert (status["status"], status["overview_status"]) == ("READY", "in_progress")


def test_a_dead_overview_run_does_not_block_a_refresh(monkeypatch):
    # Past summarization.fail_after_stale_s (12 h by default): the overview worker is gone.
    svc = _seed("refresh-after-dead-overview", datetime.now(timezone.utc) - timedelta(days=2))
    monkeypatch.setattr("src.service.codekb_service.build_git_env", lambda url, sub: {})
    monkeypatch.setattr(svc, "_run_refresh_pipeline", MagicMock())

    assert svc.refresh_space("refresh-after-dead-overview")["status"] == "SUBMITTED"
    assert svc._read_status("refresh-after-dead-overview")["status"] == "REFRESHING"
