"""A failed refresh ends REFRESH_FAILED and can be retried."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.service.codekb_service import CodeKBService, ServiceError


def _seed_status(svc: CodeKBService, space_id: str, doc: dict) -> None:
    svc.kb_manager.s3_client.put_object(
        Bucket=svc.kb_manager._get_s3_bucket(),
        Key=f"spaces/{space_id}/status.json",
        Body=json.dumps(doc).encode("utf-8"),
        ContentType="application/json",
    )



def test_a_failed_refresh_can_be_retried_and_the_retry_clears_its_error(monkeypatch):
    svc = CodeKBService.instance()
    _seed_status(
        svc,
        "retry-after-refresh-failed",
        {
            "status": "REFRESH_FAILED",
            "error": "[refresh] clone: boom",
            "git_url": "https://example.com/r.git",
            "git_commit_hash": "old",
        },
    )
    monkeypatch.setattr("src.service.codekb_service.build_git_env", lambda url, sub: {})
    monkeypatch.setattr(svc, "_run_refresh_pipeline", MagicMock())

    assert svc.refresh_space("retry-after-refresh-failed")["status"] == "SUBMITTED"

    status = svc._read_status("retry-after-refresh-failed")
    assert status["status"] == "REFRESHING"
    assert "error" not in status


def test_a_refresh_that_loses_the_claim_race_starts_nothing(monkeypatch):
    svc = CodeKBService.instance()
    _seed_status(
        svc,
        "refresh-claim-race",
        {"status": "READY", "git_url": "https://example.com/r.git", "git_commit_hash": "old"},
    )
    monkeypatch.setattr("src.service.codekb_service.build_git_env", lambda url, sub: {})
    worker = MagicMock()
    monkeypatch.setattr(svc, "_run_refresh_pipeline", worker)
    # Another request wrote status.json after our read, so our stamp is stale.
    real_read = svc._read_status_with_etag
    monkeypatch.setattr(
        svc,
        "_read_status_with_etag",
        lambda sid: (real_read(sid)[0], '"stale-etag-that-no-longer-matches"'),
    )

    with pytest.raises(ServiceError) as exc_info:
        svc.refresh_space("refresh-claim-race")

    assert exc_info.value.status_code == 409
    assert "is busy" in str(exc_info.value)
    worker.assert_not_called()
    assert svc._read_status("refresh-claim-race")["status"] == "READY"


def test_a_refresh_that_crashes_ends_refresh_failed():
    svc = CodeKBService.instance()
    _seed_status(
        svc,
        "refresh-crash",
        {
            "status": "REFRESHING",
            "git_commit_hash": "old",
            "refresh_pipeline_runs": [
                {"steps": [{"step": "clone", "status": "completed"}]},
                {
                    "steps": [
                        {"step": "clone", "status": "completed"},
                        {"step": "merge", "status": "in_progress"},
                    ]
                },
            ],
        },
    )
    with (
        patch("src.vector.s3_vectors_client.S3VectorsClient"),
        patch(
            "src.analysis.refresh_orchestrator.RefreshOrchestrator",
            side_effect=RuntimeError("boom"),
        ),
    ):
        svc._run_refresh_pipeline("refresh-crash", {})

    status = svc._read_status("refresh-crash")
    assert status["status"] == "REFRESH_FAILED"
    assert "boom" in status["error"]
    assert status["git_commit_hash"] == "old"
    # The step the crash interrupted shows failed in the latest run.
    assert [(s["step"], s["status"]) for s in status["refresh_pipeline_runs"][-1]["steps"]] == [
        ("clone", "completed"),
        ("merge", "failed"),
    ]


def test_a_stalled_refresh_is_flipped_to_refresh_failed():
    svc = CodeKBService.instance()
    long_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    _seed_status(
        svc,
        "refresh-stalled",
        {
            "status": "REFRESHING",
            "current_step": "summarize_changed",
            "updated_at": long_ago,
            "refresh_pipeline_runs": [
                {"started_at": long_ago, "steps": [{"step": "summarize_changed", "status": "completed"}]},
                {
                    "started_at": long_ago,
                    "steps": [
                        {"step": "merge", "status": "completed"},
                        {"step": "summarize_changed", "status": "in_progress"},
                    ],
                },
            ],
        },
    )

    assert svc.get_analysis_status("refresh-stalled")["status"] == "REFRESH_FAILED"

    status = svc._read_status("refresh-stalled")
    assert status["status"] == "REFRESH_FAILED"
    assert status["error"].startswith("Refresh failed: step 'summarize_changed'")
    # The interrupted step shows failed in the latest run; earlier runs are untouched.
    older, latest = status["refresh_pipeline_runs"]
    assert [(s["step"], s["status"]) for s in latest["steps"]] == [
        ("merge", "completed"),
        ("summarize_changed", "failed"),
    ]
    assert older["steps"][0]["status"] == "completed"


def _orchestrator():
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={"git_url": "https://example.com/r.git", "git_commit_hash": "old"},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch.codekb_service._read_status.return_value = {"current_step": "merge"}
    return orch


def test_an_unexpected_error_in_a_step_ends_like_a_refresh_failure():
    orch = _orchestrator()
    orch._refresh_token_usage = {"totals": {"input": 5, "output": 1, "total": 6}}

    def boom():
        raise KeyError("missing")

    orch._run_steps = boom

    result = orch.run()

    assert (result.final_error_step, result.final_error_reason) == ("merge", "unexpected_error")
    last = orch.codekb_service._update_pipeline_status.call_args
    assert last.args[:4] == ("space", "REFRESH_FAILED", "merge", "failed")
    assert last.kwargs["refresh_run"] is True
    assert "unexpected error" in last.kwargs["extra"]["error"]
    # The tokens the run spent are still recorded.
    assert "token_usage" in last.kwargs["extra"]


def test_a_failed_refresh_deletes_vectors_it_wrote_for_new_functions():
    orch = _orchestrator()
    orch._refresh_plan = MagicMock(
        preserved_component_ids=frozenset({"space::a.kept"}),
        evicted_component_ids=frozenset({"space::a.changed"}),
    )
    orch._summarized_descriptions = {
        "space::a.kept": "x",
        "space::a.changed": "y",
        "space::b.new": "z",
    }

    orch._finalize_failed_refresh("stage_kb", "[refresh] stage_kb: failed")

    orch.vectors_client.delete_vector.assert_called_once_with("space::b.new")


def test_a_refresh_that_fails_after_the_kb_swap_keeps_new_vectors():
    orch = _orchestrator()
    orch._refresh_plan = MagicMock(
        preserved_component_ids=frozenset(), evicted_component_ids=frozenset()
    )
    orch._summarized_descriptions = {"space::b.new": "z"}
    # The swapped KB holds the new function, and the retry won't re-describe it.
    orch._kb_committed = True

    orch._finalize_failed_refresh("mark_overviews_stale", "[refresh] failed")

    orch.vectors_client.delete_vector.assert_not_called()
