"""Regression tests: background jobs run on a bounded pool and are refused when it is full.

Covers the fix for POST /api/spaces starting one unbounded daemon thread per
request. The contract:
  - every background job (git analysis, zip-upload analysis, space refresh, space
    overview, hyperspace overview) runs on the process-wide pipeline pool;
  - when every slot is taken, the request is refused with PipelineCapacityError,
    surfaced as HTTP 429 with Retry-After, and nothing is written for it;
  - a slot always comes back: when the job ends, when it raises, and when the
    request fails before the job's thread starts;
  - when anything between a call site's status write and its submit raises,
    including the job's thread failing to start, the call site undoes the write;
  - the pool size comes from service.pipeline_pool_size, and 0 turns the limit
    off as an emergency switch.

Every test builds its own pool (size 1 through the ``pool`` fixture, or 0, 2
or 3 where the test needs it), so none depends on the configured default or
on jobs left running by another test.
"""

import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

import src.api.auth as auth_mod
from src.core.config import ConfigError
from src.service import pipeline_pool as pp
from src.service.codekb_service import CodeKBService, OverviewInProgressError
from src.service.errors import PipelineCapacityError

GIT_URL = "https://github.com/octo/repo.git"


# ── helpers ──────────────────────────────────────────────────────────────


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _free_slots(pool: pp.PipelinePool) -> int:
    """Count free slots without changing the pool's state."""
    taken = 0
    while pool._slots.acquire(blocking=False):
        taken += 1
    for _ in range(taken):
        pool._slots.release()
    return taken


def _seed_status(svc, space_id, doc):
    svc.kb_manager.s3_client.put_object(
        Bucket=svc.kb_manager._get_s3_bucket(),
        Key=f"spaces/{space_id}/status.json",
        Body=json.dumps(doc).encode("utf-8"),
        ContentType="application/json",
    )


def _seed_hyperspace(svc, hyperspace_id):
    cfg = {"hyperspace_id": hyperspace_id, "space_ids": [], "name": hyperspace_id}
    svc.kb_manager.s3_client.put_object(
        Bucket=svc.kb_manager._get_s3_bucket(),
        Key=svc._hyperspace_s3_key(hyperspace_id),
        Body=json.dumps(cfg).encode("utf-8"),
        ContentType="application/json",
    )


@pytest.fixture
def pool():
    """A size-1 pool used in place of the process-wide one."""
    p = pp.PipelinePool(1)
    with patch("src.service.codekb_service.pipeline_pool", return_value=p):
        yield p


@pytest.fixture
def full_pool(pool):
    """The size-1 pool with its only slot held for the duration of the test."""
    with pool.reserve("held-by-test"):
        yield pool


# ── the pool ─────────────────────────────────────────────────────────────


def test_refuses_when_full_and_admits_again_once_the_job_ends(pool):
    release = threading.Event()
    with pool.reserve("a") as slot:
        slot.submit(release.wait, 5)

    with pytest.raises(PipelineCapacityError) as ei:
        with pool.reserve("b"):
            pass
    assert ei.value.limit == 1
    assert ei.value.retry_after == pp.RETRY_AFTER_SECONDS

    release.set()
    assert _wait_until(lambda: _free_slots(pool) == 1)
    ran = threading.Event()
    with pool.reserve("c") as slot:
        slot.submit(ran.set)
    assert ran.wait(5)


def test_runs_n_jobs_at_once_and_refuses_the_next():
    pool = pp.PipelinePool(3)
    release = threading.Event()
    running = threading.Semaphore(0)

    def job():
        running.release()
        release.wait(5)

    for _ in range(3):
        with pool.reserve("job") as slot:
            slot.submit(job)
    for _ in range(3):
        assert running.acquire(timeout=5), "not all 3 jobs were running at once"

    with pytest.raises(PipelineCapacityError):
        with pool.reserve("fourth"):
            pass

    release.set()
    assert _wait_until(lambda: _free_slots(pool) == 3)


def test_slot_returns_when_job_raises_and_next_job_runs(pool):
    with pool.reserve("boom") as slot:
        slot.submit(lambda: 1 / 0)
    assert _wait_until(lambda: _free_slots(pool) == 1)

    ran = threading.Event()
    with pool.reserve("after") as slot:
        slot.submit(ran.set)
    assert ran.wait(5), "a job ran after the failing one released its slot"


def test_slot_returns_when_request_fails_before_submit(pool):
    with pytest.raises(RuntimeError):
        with pool.reserve("x"):
            raise RuntimeError("status write failed")
    assert _free_slots(pool) == 1


def test_slot_returns_when_job_thread_cannot_start(pool):
    with patch.object(
        pp.threading.Thread, "start", side_effect=RuntimeError("can't start new thread")
    ):
        with pytest.raises(RuntimeError):
            with pool.reserve("x") as slot:
                slot.submit(lambda: None)
    assert _free_slots(pool) == 1


def test_slot_cannot_be_submitted_twice(pool):
    with pool.reserve("x") as slot:
        slot.submit(lambda: None)
        with pytest.raises(RuntimeError):
            slot.submit(lambda: None)
    assert _wait_until(lambda: _free_slots(pool) == 1)


@pytest.mark.parametrize("size", [1, 0])
def test_slot_cannot_be_submitted_after_its_block_ends(size):
    pool = pp.PipelinePool(size)
    with pool.reserve("x", "sp") as slot:
        pass
    ran = threading.Event()
    with pytest.raises(RuntimeError, match="after its reserve"):
        slot.submit(ran.set)
    assert not ran.wait(0.1)
    assert pool.in_use == 0  # the permit was returned once, not twice
    if pool.enabled:
        assert _free_slots(pool) == 1


def test_job_thread_is_named_after_its_kind_and_target(pool):
    names = []
    done = threading.Event()

    def job():
        names.append(threading.current_thread().name)
        done.set()

    with pool.reserve("refresh", "sp-named") as slot:
        slot.submit(job)
    assert done.wait(5)
    assert names == ["codekb-pipeline-refresh-sp-named"]


def test_negative_pool_size_is_rejected():
    with pytest.raises(ValueError):
        pp.PipelinePool(-1)


# ── off switch: size 0 ───────────────────────────────────────────────────


def test_disabled_pool_never_refuses_and_runs_jobs_directly(caplog):
    pool = pp.PipelinePool(0, warn_after_s=0.01)
    assert not pool.enabled
    release = threading.Event()
    started = threading.Semaphore(0)

    def job():
        started.release()
        release.wait(5)

    with (
        patch.object(pp.PipelinePool, "_run", side_effect=AssertionError("wrapper used")) as run,
        caplog.at_level(logging.INFO, logger=pp.__name__),
    ):
        for i in range(100):
            with pool.reserve("analysis", f"sp-{i}") as slot:
                slot.submit(job)
        for _ in range(100):
            assert started.acquire(timeout=5), "not all 100 jobs started"
        release.set()
        time.sleep(0.1)

    run.assert_not_called()  # no wrapper: no counting, logging or watchdog
    assert pool.in_use == 0
    assert "Pipeline" not in caplog.text


def test_disabled_pool_passes_job_arguments_through():
    pool = pp.PipelinePool(0)
    got = {}
    done = threading.Event()

    def job(a, b, *, c):
        got.update(a=a, b=b, c=c)
        done.set()

    with pool.reserve("refresh", "sp") as slot:
        slot.submit(job, 1, 2, c=3)
    assert done.wait(5)
    assert got == {"a": 1, "b": 2, "c": 3}


def test_disabled_pool_lets_errors_before_submit_propagate():
    pool = pp.PipelinePool(0)
    with pytest.raises(RuntimeError):
        with pool.reserve("x"):
            raise RuntimeError("status write failed")


def test_disabled_pool_call_site_starts_its_job(monkeypatch):
    pool = pp.PipelinePool(0)
    monkeypatch.setattr("src.service.codekb_service.pipeline_pool", lambda: pool)
    svc = CodeKBService.instance()
    ran = threading.Event()
    with (
        patch("src.service.codekb_service.validate_git_url", side_effect=lambda u: u),
        patch("src.service.codekb_service.lookup_repo_visibility", return_value="private"),
        patch.object(svc, "_run_pipeline", side_effect=lambda *a, **k: ran.set()),
    ):
        assert svc.start_analysis(GIT_URL, actor_sub="sub-1")["status"] == "SUBMITTED"
    assert ran.wait(5)


def test_startup_warns_when_the_pool_is_disabled(monkeypatch, caplog):
    monkeypatch.setattr(pp, "_pool", None)
    values = {"service.pipeline_pool_size": 0, "summarization.warn_after_stale_s": 1800}
    monkeypatch.setattr(pp, "require_config", values.__getitem__)
    with caplog.at_level(logging.WARNING, logger=pp.__name__):
        pool = pp.pipeline_pool()
    assert not pool.enabled
    assert "Pipeline pool disabled (service.pipeline_pool_size=0)" in caplog.text


def test_env_var_zero_disables_the_pool(monkeypatch):
    monkeypatch.setenv("CODEKB_PIPELINE_POOL_SIZE", "0")
    from src.core.config import ConfigurationManager

    manager = ConfigurationManager()
    manager.load_configuration()
    with patch("src.service.pipeline_pool.require_config", manager.get_config_value):
        assert pp._configured_size() == 0


# ── visibility: start/finish logs, slots-in-use count, watchdog ──────────


def test_in_use_counts_reserved_and_running_slots():
    pool = pp.PipelinePool(2)
    release = threading.Event()
    assert pool.in_use == 0
    with pool.reserve("a", "sp-a") as slot:
        assert pool.in_use == 1  # reserved, not started yet
        slot.submit(release.wait, 5)
    assert pool.in_use == 1  # running
    release.set()
    assert _wait_until(lambda: pool.in_use == 0)


def test_job_logs_start_and_finish_with_target_outcome_and_slots(caplog):
    pool = pp.PipelinePool(2)
    with caplog.at_level(logging.INFO, logger=pp.__name__):
        with pool.reserve("analysis", "sp-ok") as slot:
            slot.submit(lambda: None)
        with pool.reserve("refresh", "sp-bad") as slot:
            slot.submit(lambda: 1 / 0)
        assert _wait_until(lambda: caplog.text.count("Pipeline finished") == 2)

    assert "Pipeline started: kind=analysis, target=sp-ok, slots in use" in caplog.text
    assert re.search(
        r"Pipeline finished: kind=analysis, target=sp-ok, outcome=ok, \d+s, slots in use \d/2",
        caplog.text,
    )
    assert "Pipeline finished: kind=refresh, target=sp-bad, outcome=raised" in caplog.text


def test_watchdog_warns_while_job_runs_and_stops_when_it_ends(caplog):
    pool = pp.PipelinePool(1, warn_after_s=0.05)
    release = threading.Event()
    with caplog.at_level(logging.WARNING, logger=pp.__name__):
        with pool.reserve("analysis", "sp-slow") as slot:
            slot.submit(release.wait, 5)
        assert _wait_until(lambda: caplog.text.count("Pipeline still running") >= 2)
        release.set()
        assert _wait_until(lambda: pool.in_use == 0)
        warnings_at_finish = caplog.text.count("Pipeline still running")
        time.sleep(0.2)

    assert "Pipeline still running: kind=analysis, target=sp-slow" in caplog.text
    assert caplog.text.count("Pipeline still running") == warnings_at_finish


def test_watchdog_off_when_threshold_is_zero(caplog):
    pool = pp.PipelinePool(1, warn_after_s=0)
    with caplog.at_level(logging.WARNING, logger=pp.__name__):
        with pool.reserve("analysis", "sp") as slot:
            slot.submit(time.sleep, 0.1)
        assert _wait_until(lambda: pool.in_use == 0)
    assert "Pipeline still running" not in caplog.text


def test_job_still_runs_and_slot_returns_if_watchdog_cannot_start(caplog):
    pool = pp.PipelinePool(1, warn_after_s=0.05)
    real_start = threading.Thread.start

    def start_except_watchdog(self):
        if self.name == "codekb-pipeline-watchdog":
            raise RuntimeError("can't start new thread")
        return real_start(self)

    ran = threading.Event()
    with patch.object(threading.Thread, "start", start_except_watchdog):
        with caplog.at_level(logging.WARNING, logger=pp.__name__):
            with pool.reserve("analysis", "sp") as slot:
                slot.submit(ran.set)
            assert ran.wait(5), "job did not run when its watchdog failed to start"
            assert _wait_until(lambda: pool.in_use == 0)
    assert "Pipeline watchdog could not start" in caplog.text


# ── every job-starting call site, with the pool full ────────────────────


def test_create_refused_writes_no_status(full_pool, caplog):
    svc = CodeKBService.instance()
    with (
        patch("src.service.codekb_service.validate_git_url", side_effect=lambda u: u),
        patch("src.service.codekb_service.lookup_repo_visibility", return_value="private"),
        patch.object(svc, "_run_pipeline") as run,
        patch.object(svc, "_write_status") as write,
    ):
        with pytest.raises(PipelineCapacityError):
            svc.start_analysis(GIT_URL, actor_sub="sub-1")
    write.assert_not_called()
    run.assert_not_called()
    # The refusal names the generated space ID, not "None".
    assert re.search(r"Pipeline refused: kind=analysis, target=(?!None)\S", caplog.text)


def test_upload_refused_leaves_status_and_zip_untouched(full_pool, caplog):
    svc = CodeKBService.instance()
    sid = svc.generate_upload_url("Up", created_by="u@example.com", actor_sub="sub-1")["space_id"]
    bucket = svc.kb_manager._get_s3_bucket()
    svc.kb_manager.s3_client.put_object(Bucket=bucket, Key=svc._upload_s3_key(sid), Body=b"zip")

    with patch.object(svc, "_run_pipeline") as run:
        with pytest.raises(PipelineCapacityError):
            svc.start_upload_analysis(space_id=sid, space_name="Up", actor_sub="sub-1")
    run.assert_not_called()
    assert svc._read_status(sid)["status"] == "PENDING_UPLOAD"
    svc.kb_manager.s3_client.head_object(Bucket=bucket, Key=svc._upload_s3_key(sid))
    assert f"kind=upload_analysis, target={sid}," in caplog.text


def test_refresh_refused_leaves_space_ready(full_pool, caplog):
    svc = CodeKBService.instance()
    _seed_status(
        svc, "sp-refresh", {"space_id": "sp-refresh", "status": "READY", "git_url": GIT_URL}
    )

    with patch.object(svc, "_run_refresh_pipeline") as run:
        with pytest.raises(PipelineCapacityError):
            svc.refresh_space("sp-refresh", actor_sub="sub-1")
    run.assert_not_called()
    status = svc._read_status("sp-refresh")
    assert status["status"] == "READY"
    assert "refresh_started_at" not in status
    assert "kind=refresh, target=sp-refresh," in caplog.text


def test_space_overview_refused_leaves_no_claim(full_pool, caplog):
    svc = CodeKBService.instance()
    _seed_status(svc, "sp-ov", {"space_id": "sp-ov", "status": "READY"})

    with patch.object(svc, "_run_space_overview") as run:
        with pytest.raises(PipelineCapacityError):
            svc.start_overview_generation("sp-ov")
    run.assert_not_called()
    assert "overview_status" not in svc._read_status("sp-ov")
    assert "kind=space_overview, target=sp-ov," in caplog.text


def test_hyperspace_overview_refused_leaves_no_claim(full_pool, caplog):
    svc = CodeKBService.instance()
    _seed_hyperspace(svc, "hs-ov")

    with patch.object(svc, "_run_hyperspace_overview") as run:
        with pytest.raises(PipelineCapacityError):
            svc.start_hyperspace_overview_generation("hs-ov")
    run.assert_not_called()
    assert "overview_status" not in svc.get_hyperspace("hs-ov")
    assert "kind=hyperspace_overview, target=hs-ov," in caplog.text


def test_job_starting_call_site_hands_its_job_to_the_pool(pool):
    svc = CodeKBService.instance()
    ran = threading.Event()
    with (
        patch("src.service.codekb_service.validate_git_url", side_effect=lambda u: u),
        patch("src.service.codekb_service.lookup_repo_visibility", return_value="private"),
        patch.object(svc, "_run_pipeline", side_effect=lambda *a, **k: ran.set()),
    ):
        res = svc.start_analysis(GIT_URL, actor_sub="sub-1")
    assert res["status"] == "SUBMITTED"
    assert ran.wait(5)
    assert _wait_until(lambda: _free_slots(pool) == 1)


def test_lost_overview_claim_returns_the_slot(pool, monkeypatch):
    svc = CodeKBService.instance()
    _seed_status(svc, "sp-race", {"space_id": "sp-race", "status": "READY"})
    real_read = svc._read_status_with_etag
    monkeypatch.setattr(
        svc, "_read_status_with_etag", lambda sid: (real_read(sid)[0], '"stale-etag"')
    )

    with pytest.raises(OverviewInProgressError):
        svc.start_overview_generation("sp-race")
    assert _free_slots(pool) == 1


# ── every job-starting call site, when the job's thread cannot start ─────
#
# The status write has already happened, so each call site must undo it rather
# than leave the record in progress with no worker behind it.


@pytest.fixture
def no_new_threads():
    with patch.object(
        pp.threading.Thread, "start", side_effect=RuntimeError("can't start new thread")
    ):
        yield


def test_create_thread_start_failure_ends_failed(pool, no_new_threads):
    svc = CodeKBService.instance()
    with (
        patch("src.service.codekb_service.validate_git_url", side_effect=lambda u: u),
        patch("src.service.codekb_service.lookup_repo_visibility", return_value="private"),
        patch.object(svc, "_generate_unique_space_id", return_value="sp-nothread"),
        patch.object(svc, "_run_pipeline") as run,
    ):
        with pytest.raises(RuntimeError, match="can't start new thread"):
            svc.start_analysis(GIT_URL, actor_sub="sub-1")
    run.assert_not_called()
    status = svc._read_status("sp-nothread")
    assert status["status"] == "FAILED"
    assert status["error"] == CodeKBService._JOB_START_FAILED
    assert status["created_by_sub"] == "sub-1"  # the owner key survives the undo
    assert _free_slots(pool) == 1


def test_upload_thread_start_failure_restores_pending_upload(pool, no_new_threads):
    svc = CodeKBService.instance()
    sid = svc.generate_upload_url("Up", created_by="u@example.com", actor_sub="sub-1")["space_id"]
    bucket = svc.kb_manager._get_s3_bucket()
    svc.kb_manager.s3_client.put_object(Bucket=bucket, Key=svc._upload_s3_key(sid), Body=b"zip")

    with patch.object(svc, "_run_pipeline") as run:
        with pytest.raises(RuntimeError, match="can't start new thread"):
            svc.start_upload_analysis(space_id=sid, space_name="Up", actor_sub="sub-1")
    run.assert_not_called()
    assert svc._read_status(sid)["status"] == "PENDING_UPLOAD"
    svc.kb_manager.s3_client.head_object(Bucket=bucket, Key=svc._upload_s3_key(sid))
    assert _free_slots(pool) == 1


def test_refresh_thread_start_failure_restores_ready(pool, no_new_threads):
    svc = CodeKBService.instance()
    prior = {"space_id": "sp-rt", "status": "READY", "git_url": GIT_URL}
    _seed_status(svc, "sp-rt", prior)

    with patch.object(svc, "_run_refresh_pipeline") as run:
        with pytest.raises(RuntimeError, match="can't start new thread"):
            svc.refresh_space("sp-rt", actor_sub="sub-1")
    run.assert_not_called()
    assert svc._read_status("sp-rt") == prior
    assert _free_slots(pool) == 1


def test_space_overview_thread_start_failure_ends_failed(pool, no_new_threads):
    svc = CodeKBService.instance()
    _seed_status(svc, "sp-ovt", {"space_id": "sp-ovt", "status": "READY"})

    with patch.object(svc, "_run_space_overview") as run:
        with pytest.raises(RuntimeError, match="can't start new thread"):
            svc.start_overview_generation("sp-ovt")
    run.assert_not_called()
    status = svc._read_status("sp-ovt")
    assert status["overview_status"] == "failed"
    assert status["overview_error"] == CodeKBService._JOB_START_FAILED
    assert not svc._overview_run_is_fresh(status)  # a retry is not refused with 409
    assert _free_slots(pool) == 1


def test_hyperspace_overview_thread_start_failure_ends_failed(pool, no_new_threads):
    svc = CodeKBService.instance()
    _seed_hyperspace(svc, "hs-ovt")

    with patch.object(svc, "_run_hyperspace_overview") as run:
        with pytest.raises(RuntimeError, match="can't start new thread"):
            svc.start_hyperspace_overview_generation("hs-ovt")
    run.assert_not_called()
    cfg = svc.get_hyperspace("hs-ovt")
    assert cfg["overview_status"] == "failed"
    assert cfg["overview_error"] == CodeKBService._JOB_START_FAILED
    assert _free_slots(pool) == 1


def _seed_prior_overview_doc(svc, key):
    svc.kb_manager.s3_client.put_object(
        Bucket=svc.kb_manager._get_s3_bucket(), Key=key, Body=b"# old"
    )
    return [{"key": "business_overview", "s3_path": key}]


@contextmanager
def _bucket_lookup_fails_once_after(svc, write_method_name):
    """Fail the first S3 bucket lookup made after the claim write.

    That lookup is the one before deleting the prior overview's documents, which
    sits between the claim write and the submit. Every other lookup, including
    the claim write's own and the undo write's, works.
    """
    real_write = getattr(svc, write_method_name)
    real_bucket = svc.kb_manager._get_s3_bucket
    state = {"armed": False, "fired": False}

    def write(*args, **kwargs):
        real_write(*args, **kwargs)
        state["armed"] = True

    def bucket():
        if state["armed"] and not state["fired"]:
            state["fired"] = True
            raise RuntimeError("no bucket")
        return real_bucket()

    with (
        patch.object(svc, write_method_name, side_effect=write),
        patch.object(svc.kb_manager, "_get_s3_bucket", side_effect=bucket),
    ):
        yield
    assert state["fired"], "the post-claim lookup never ran"


def test_space_overview_failure_while_deleting_prior_docs_ends_failed(pool):
    svc = CodeKBService.instance()
    docs = _seed_prior_overview_doc(svc, "spaces/sp-del/docs/business_overview.md")
    _seed_status(
        svc,
        "sp-del",
        {"space_id": "sp-del", "status": "READY", "space_overview_docs": docs},
    )

    with (
        _bucket_lookup_fails_once_after(svc, "_write_status"),
        patch.object(svc, "_run_space_overview") as run,
    ):
        with pytest.raises(RuntimeError, match="no bucket"):
            svc.start_overview_generation("sp-del")
    run.assert_not_called()
    status = svc._read_status("sp-del")
    assert status["overview_status"] == "failed"
    assert status["space_overview_docs"] == []
    assert _free_slots(pool) == 1


def test_hyperspace_overview_failure_while_deleting_prior_docs_ends_failed(pool):
    svc = CodeKBService.instance()
    _seed_hyperspace(svc, "hs-del")
    cfg = svc.get_hyperspace("hs-del")
    cfg["hyperspace_overview_docs"] = _seed_prior_overview_doc(
        svc, "hyperspaces/hs-del/docs/system_overview.md"
    )
    svc._write_hyperspace(cfg)

    with (
        _bucket_lookup_fails_once_after(svc, "_write_hyperspace"),
        patch.object(svc, "_run_hyperspace_overview") as run,
    ):
        with pytest.raises(RuntimeError, match="no bucket"):
            svc.start_hyperspace_overview_generation("hs-del")
    run.assert_not_called()
    cfg = svc.get_hyperspace("hs-del")
    assert cfg["overview_status"] == "failed"
    assert cfg["hyperspace_overview_docs"] == []
    assert _free_slots(pool) == 1


def test_failed_undo_still_raises_the_thread_start_error(pool, no_new_threads, caplog):
    svc = CodeKBService.instance()
    _seed_status(svc, "sp-undo", {"space_id": "sp-undo", "status": "READY", "git_url": GIT_URL})

    # The REFRESHING write succeeds; the undo write fails.
    with patch.object(svc, "_write_status", side_effect=[None, RuntimeError("s3 down")]):
        with pytest.raises(RuntimeError, match="can't start new thread"):
            svc.refresh_space("sp-undo", actor_sub="sub-1")
    assert "Could not undo the status write: kind=refresh, target=sp-undo" in caplog.text
    assert _free_slots(pool) == 1


# ── HTTP ─────────────────────────────────────────────────────────────────


def test_create_space_returns_429_with_retry_after(full_pool):
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from src.api.app import pipeline_capacity_exception_handler
    from src.api.routes import spaces

    app = FastAPI()
    app.include_router(spaces.router, prefix="/api/spaces")
    app.add_exception_handler(PipelineCapacityError, pipeline_capacity_exception_handler)
    client = TestClient(app, raise_server_exceptions=False)

    with (
        patch.object(auth_mod, "_REQUIRE_AUTH", False),
        patch("src.api.routes.spaces.gate_repo_read", return_value="private"),
        patch("src.service.codekb_service.validate_git_url", side_effect=lambda u: u),
    ):
        resp = client.post("/api/spaces", json={"git_url": GIT_URL})

    assert resp.status_code == 429, resp.text
    assert resp.headers["Retry-After"] == str(pp.RETRY_AFTER_SECONDS)
    assert resp.json()["error"] == "pipeline_capacity"
    assert "maximum of 1 background jobs" in resp.json()["detail"]


# The other four job-starting routes. Each catches ServiceError and maps it to
# its own status (generate-overview hard-codes 409); this checks none of them
# swallows the refusal. The service method itself raises, so the test exercises
# only the route's error handling.
_OTHER_JOB_ROUTES = [
    pytest.param(
        "/api/spaces/upload",
        {"space_id": "sp-up", "space_name": "Up"},
        "start_upload_analysis",
        id="upload",
    ),
    pytest.param("/api/spaces/sp-r/refresh", None, "refresh_space", id="refresh"),
    pytest.param(
        "/api/spaces/sp-o/generate-overview", None, "start_overview_generation", id="overview"
    ),
    pytest.param(
        "/api/hyperspaces/hs-o/generate-overview",
        None,
        "start_hyperspace_overview_generation",
        id="hyperspace_overview",
    ),
]


@pytest.mark.parametrize("path, body, service_method", _OTHER_JOB_ROUTES)
def test_other_job_routes_return_429(path, body, service_method):
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from src.api.app import pipeline_capacity_exception_handler
    from src.api.gates import gate_write
    from src.api.routes import analysis, hyperspaces, spaces

    app = FastAPI()
    app.include_router(spaces.router, prefix="/api/spaces")
    app.include_router(analysis.router, prefix="/api/spaces")
    app.include_router(hyperspaces.router, prefix="/api/hyperspaces")
    app.add_exception_handler(PipelineCapacityError, pipeline_capacity_exception_handler)
    app.dependency_overrides[gate_write] = lambda: None
    client = TestClient(app, raise_server_exceptions=False)

    svc = CodeKBService.instance()
    with (
        patch.object(auth_mod, "_REQUIRE_AUTH", False),
        patch("src.api.routes.spaces.zip_upload_enabled", return_value=True),
        patch("src.api.routes.analysis.overview_generation_enabled", return_value=True),
        patch("src.api.routes.hyperspaces.overview_generation_enabled", return_value=True),
        patch("src.api.routes.hyperspaces._gate_members"),
        patch.object(
            svc,
            service_method,
            side_effect=PipelineCapacityError(1, pp.RETRY_AFTER_SECONDS),
        ) as method,
    ):
        resp = client.post(path, json=body or {})

    method.assert_called_once()
    assert resp.status_code == 429, resp.text
    assert resp.headers["Retry-After"] == str(pp.RETRY_AFTER_SECONDS)
    assert resp.json()["error"] == "pipeline_capacity"


def test_real_app_registers_the_429_handler():
    # The test above mounts the handler on its own app. Without this check,
    # dropping the registration from api/app.py would go unnoticed: the
    # catch-all handler would turn every refusal into a 500.
    from src.api.app import app as asgi_app, pipeline_capacity_exception_handler

    # The module's `app` is the root-path wrapper; the FastAPI instance is inside.
    fastapi_app = asgi_app.app
    assert (
        fastapi_app.exception_handlers.get(PipelineCapacityError)
        is pipeline_capacity_exception_handler
    )


# ── config ───────────────────────────────────────────────────────────────


def test_default_pool_size_ships_in_default_yaml():
    from src.core.config import require_config

    assert int(require_config("service.pipeline_pool_size")) >= 1


@pytest.mark.parametrize("value", [7, "7"])
def test_configured_size_is_used(value):
    with patch.object(pp, "require_config", return_value=value):
        assert pp._configured_size() == 7


@pytest.mark.parametrize("value", [-1, "abc", None])
def test_configured_size_fails_closed(value):
    with patch.object(pp, "require_config", return_value=value):
        with pytest.raises(ConfigError):
            pp._configured_size()


@pytest.mark.parametrize("value", [0, "0"])
def test_configured_size_zero_is_the_off_switch(value):
    with patch.object(pp, "require_config", return_value=value):
        assert pp._configured_size() == 0


@pytest.mark.parametrize("value, expected", [(1800, 1800), ("60", 60), (0, 0), (-5, 0)])
def test_watchdog_threshold_comes_from_warn_after_stale_s(value, expected):
    with patch.object(pp, "require_config", return_value=value) as cfg:
        assert pp._configured_warn_after_s() == expected
    cfg.assert_called_once_with("summarization.warn_after_stale_s")


def test_watchdog_threshold_must_be_an_integer():
    with patch.object(pp, "require_config", return_value="soon"):
        with pytest.raises(ConfigError):
            pp._configured_warn_after_s()


def test_env_var_maps_to_the_pool_size_setting():
    from src.core.config import ConfigurationManager

    assert (
        ConfigurationManager._ENV_OVERRIDES["CODEKB_PIPELINE_POOL_SIZE"]
        == "service.pipeline_pool_size"
    )
    assert "CODEKB_PIPELINE_POOL_SIZE" in ConfigurationManager._INT_ENV_VARS


# ── guard against new unbounded spawn sites ──────────────────────────────


def test_no_bare_thread_or_timer_outside_pipeline_pool():
    """Background work must go through pipeline_pool(), not a bare Thread or Timer.

    Every per-request thread this fix replaced was a ``threading.Thread(...)``
    call. If this fails, run the new job via
    ``with pipeline_pool().reserve(kind) as slot: ... slot.submit(fn, ...)``.

    What it does not catch: ``ThreadPoolExecutor`` (used in ``with`` blocks
    that wait for their work before returning, so it never outlives the
    request), ``asyncio.create_task`` and FastAPI ``BackgroundTasks``. The
    space delete route runs its work with ``asyncio.create_task``; it is short
    and not one of the jobs this pool limits.
    """
    src_root = Path(__file__).resolve().parents[1] / "src"
    pool_module = src_root / "service" / "pipeline_pool.py"
    thread_call = re.compile(r"\b(Thread|Timer)\(")
    offenders = [
        f"{path.relative_to(src_root)}:{lineno}"
        for path in src_root.rglob("*.py")
        if path != pool_module
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if thread_call.search(line) and not line.lstrip().startswith("#")
    ]
    assert not offenders, f"Thread started outside the pipeline pool: {offenders}"
