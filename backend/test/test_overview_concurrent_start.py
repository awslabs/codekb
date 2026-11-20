"""Regression tests: overview generation must not start twice concurrently.

Covers the fix for the bug where ``start_overview_generation`` /
``start_hyperspace_overview_generation`` only checked that the target was READY
and ignored ``overview_status``, so a second request (double-click, retry after
a slow response, or a second user) started another background generator —
doubling Bedrock cost and making the final catalog depend on whichever run
finished last.

The contract now:
  - a second trigger while a fresh run is ``in_progress`` is refused with 409
    (OverviewInProgressError) and the generator runs exactly once;
  - a run whose heartbeat is stale (dead worker) can still be restarted;
  - the claim write is conditional (IfMatch), so a request that loses the
    read-modify-write race is refused rather than starting a second generator.
"""

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from src.service.codekb_service import CodeKBService, OverviewInProgressError


def _seed_ready_status(svc: CodeKBService, space_id: str, **extra) -> None:
    """Write a minimal READY status.json for a space into the mocked bucket."""
    bucket = svc.kb_manager._get_s3_bucket()
    doc = {"space_id": space_id, "status": "READY", "space_name": space_id, **extra}
    svc.kb_manager.s3_client.put_object(
        Bucket=bucket,
        Key=f"spaces/{space_id}/status.json",
        Body=json.dumps(doc).encode("utf-8"),
        ContentType="application/json",
    )


def _seed_hyperspace(svc: CodeKBService, hyperspace_id: str, **extra) -> None:
    """Write a minimal hyperspace config.json into the mocked bucket."""
    bucket = svc.kb_manager._get_s3_bucket()
    cfg = {"hyperspace_id": hyperspace_id, "space_ids": [], "name": hyperspace_id, **extra}
    svc.kb_manager.s3_client.put_object(
        Bucket=bucket,
        Key=svc._hyperspace_s3_key(hyperspace_id),
        Body=json.dumps(cfg).encode("utf-8"),
        ContentType="application/json",
    )


class _BlockingWorker:
    """Stub overview worker that counts runs and blocks until released.

    Blocking keeps ``overview_status`` at ``in_progress`` for the duration of the
    test so a concurrent trigger sees a genuinely active run.
    """

    def __init__(self) -> None:
        self.calls = 0
        self._release = threading.Event()
        self._started = threading.Event()

    def __call__(self, _target_id):
        # Patched onto the class as a plain object, so it is NOT bound as a
        # method: the service calls ``self._run_space_overview(id)`` which lands
        # here as ``__call__(id)`` with no implicit ``self`` for the service.
        self.calls += 1
        self._started.set()
        self._release.wait(timeout=5.0)

    def wait_started(self) -> None:
        assert self._started.wait(timeout=5.0), "worker thread never started"

    def release(self) -> None:
        self._release.set()


def test_space_overview_second_trigger_refused_while_in_progress(monkeypatch):
    svc = CodeKBService.instance()
    _seed_ready_status(svc, "sp1")

    worker = _BlockingWorker()
    monkeypatch.setattr(CodeKBService, "_run_space_overview", worker)

    first = svc.start_overview_generation("sp1")
    assert first["status"] == "SUBMITTED"
    worker.wait_started()

    # Second request while the first run is in progress must be refused.
    with pytest.raises(OverviewInProgressError):
        svc.start_overview_generation("sp1")

    worker.release()
    # The generator was started exactly once.
    assert worker.calls == 1

    # Status still reflects the single in-progress claim.
    status = svc._read_status("sp1")
    assert status["overview_status"] == "in_progress"


def test_space_overview_restarts_when_prior_run_is_stale(monkeypatch):
    svc = CodeKBService.instance()
    # A run marked in_progress but whose heartbeat is far older than the stale
    # ceiling — e.g. its worker task was recycled with no resume. Restartable.
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    _seed_ready_status(svc, "sp2", overview_status="in_progress", updated_at=stale)

    worker = _BlockingWorker()
    monkeypatch.setattr(CodeKBService, "_run_space_overview", worker)

    result = svc.start_overview_generation("sp2")
    assert result["status"] == "SUBMITTED"
    worker.wait_started()
    worker.release()
    assert worker.calls == 1


def test_space_overview_conditional_write_loses_race_refuses(monkeypatch):
    svc = CodeKBService.instance()
    _seed_ready_status(svc, "sp3")

    worker = _BlockingWorker()
    monkeypatch.setattr(CodeKBService, "_run_space_overview", worker)

    # Simulate another writer having changed status.json between our read and
    # write by handing back a stale ETag; the IfMatch claim write then fails.
    real_read = svc._read_status_with_etag

    def stale_etag_read(space_id):
        status, _etag = real_read(space_id)
        return status, '"stale-etag-that-no-longer-matches"'

    monkeypatch.setattr(svc, "_read_status_with_etag", stale_etag_read)

    with pytest.raises(OverviewInProgressError):
        svc.start_overview_generation("sp3")
    # No worker should have been launched.
    assert worker.calls == 0


def test_hyperspace_overview_second_trigger_refused_while_in_progress(monkeypatch):
    svc = CodeKBService.instance()
    _seed_hyperspace(svc, "hs1")

    worker = _BlockingWorker()
    monkeypatch.setattr(CodeKBService, "_run_hyperspace_overview", worker)

    first = svc.start_hyperspace_overview_generation("hs1")
    assert first["status"] == "SUBMITTED"
    worker.wait_started()

    with pytest.raises(OverviewInProgressError):
        svc.start_hyperspace_overview_generation("hs1")

    worker.release()
    assert worker.calls == 1


def test_hyperspace_overview_conditional_write_loses_race_refuses(monkeypatch):
    svc = CodeKBService.instance()
    _seed_hyperspace(svc, "hs2")

    worker = _BlockingWorker()
    monkeypatch.setattr(CodeKBService, "_run_hyperspace_overview", worker)

    real_read = svc._read_hyperspace_with_etag

    def stale_etag_read(hyperspace_id):
        cfg, _etag = real_read(hyperspace_id)
        return cfg, '"stale-etag-that-no-longer-matches"'

    monkeypatch.setattr(svc, "_read_hyperspace_with_etag", stale_etag_read)

    with pytest.raises(OverviewInProgressError):
        svc.start_hyperspace_overview_generation("hs2")
    assert worker.calls == 0
