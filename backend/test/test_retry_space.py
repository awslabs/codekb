"""Retrying a failed or stalled Git analysis: POST /api/spaces/{id}/retry.

A retry keeps the space ID and the commit, restores the source the earlier run
stored, and describes only the functions that run did not get to.
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.service import pipeline_pool as pp
from src.service.codekb_service import CodeKBService, ServiceError, SpaceNotFoundError

GIT_URL = "https://example.com/fruit.git"


@pytest.fixture
def svc():
    with patch("src.service.codekb_service.pipeline_pool", return_value=pp.PipelinePool(1)):
        yield CodeKBService.instance()


def _seed(svc, space_id, **fields):
    status = {
        "space_id": space_id,
        "status": "FAILED",
        "git_url": GIT_URL,
        "git_commit_hash": "abc123",
        "branch": "main",
        "summarize": True,
        "error": "boom",
        "pipeline_steps": [{"step": "summarize", "status": "failed"}],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    svc.kb_manager.s3_client.put_object(
        Bucket=svc.kb_manager._get_s3_bucket(),
        Key=f"spaces/{space_id}/status.json",
        Body=json.dumps(status).encode(),
    )


def _retry(svc, space_id):
    """Retry and return the arguments the pipeline was started with."""
    ran = threading.Event()
    with patch.object(svc, "_run_pipeline", side_effect=lambda *a: ran.set()) as run:
        result = svc.retry_space(space_id)
        assert ran.wait(5)
    assert result["status"] == "SUBMITTED"
    return run.call_args.args


def test_failed_space_is_rerun_under_the_same_id_and_commit(svc):
    _seed(svc, "rt-failed", omit_low_business_relevance=True)

    space_id, _source_dir, summarize, omit_low, _acquire = _retry(svc, "rt-failed")

    assert (space_id, summarize, omit_low) == ("rt-failed", True, True)
    status = svc._read_status("rt-failed")
    assert status["status"] == "ANALYZING"
    assert status["git_commit_hash"] == "abc123"
    assert status["retried_at"]
    assert "error" not in status and "pipeline_steps" not in status


def test_summarize_is_inferred_for_a_status_written_before_the_flag(svc):
    _seed(svc, "rt-legacy")
    status = svc._read_status("rt-legacy")
    del status["summarize"]
    svc._write_status("rt-legacy", status)

    assert _retry(svc, "rt-legacy")[2] is True


def test_stalled_analysis_can_be_retried_without_waiting_for_failed(svc):
    stale = datetime.now(timezone.utc) - timedelta(hours=1)
    _seed(svc, "rt-stalled", status="SUMMARIZING", updated_at=stale.isoformat())

    _retry(svc, "rt-stalled")


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"status": "SUMMARIZING"}, id="still-running"),
        pytest.param({"status": "READY"}, id="ready"),
        pytest.param({"git_url": None}, id="zip-upload"),
        pytest.param({"git_commit_hash": None}, id="no-stored-source"),
    ],
)
def test_other_spaces_are_refused_and_left_unchanged(svc, fields):
    _seed(svc, "rt-refused", **fields)
    before = svc._read_status("rt-refused")

    with patch.object(svc, "_run_pipeline") as run, pytest.raises(ServiceError) as err:
        svc.retry_space("rt-refused")

    assert err.value.status_code == 409
    run.assert_not_called()
    assert svc._read_status("rt-refused") == before


def test_unknown_space(svc):
    with pytest.raises(SpaceNotFoundError):
        svc.retry_space("rt-missing")


def test_a_retry_that_loses_the_claim_starts_no_job(svc, monkeypatch):
    _seed(svc, "rt-raced")
    real_read = svc._read_status_with_etag
    monkeypatch.setattr(
        svc, "_read_status_with_etag", lambda sid: (real_read(sid)[0], '"stale-etag"')
    )

    with patch.object(svc, "_run_pipeline") as run, pytest.raises(ServiceError) as err:
        svc.retry_space("rt-raced")

    assert err.value.status_code == 409
    run.assert_not_called()
    assert svc._read_status("rt-raced")["status"] == "FAILED"


def test_stored_source_is_restored_into_the_working_tree(svc, tmp_path):
    stored = tmp_path / "stored"
    stored.mkdir()
    (stored / "fruit.py").write_text("def apple(): ...\n")
    _seed(svc, "rt-source", status="ANALYZING")
    work = tmp_path / "work"

    with patch.object(svc.source_provider, "source_root", return_value=stored):
        metadata = svc._acquire_source_stored("rt-source", work, svc._read_status("rt-source"))

    assert (work / "fruit.py").exists()
    assert metadata == {"git_url": GIT_URL, "git_commit_hash": "abc123", "branch": "main"}


def test_missing_stored_source_fails_the_space(svc, tmp_path):
    _seed(svc, "rt-nosource", status="ANALYZING")

    with patch.object(svc.source_provider, "source_root", side_effect=RuntimeError("gone")):
        metadata = svc._acquire_source_stored("rt-nosource", tmp_path / "w", {})

    assert metadata is None
    assert svc._read_status("rt-nosource")["status"] == "FAILED"


class _RecordingSummarizer:
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def summarize_all(self, targets=None, prior_descriptions=None):
        self.calls.append((targets, prior_descriptions))
        fresh = len(targets) if targets is not None else 3
        return {"total_analyzed": fresh, "total_cached": 0, "coverage": "ok"}


@pytest.mark.parametrize(
    "retried, expected_call, expected_count",
    [
        pytest.param(True, ({"f::apple", "f::banana"}, {"f::cherry": "Cherry."}), 3, id="retry"),
        pytest.param(False, (None, None), 3, id="first-run"),
    ],
)
def test_a_retry_describes_only_functions_without_a_stored_description(
    svc, mock_s3vectors, retried, expected_call, expected_count
):
    _seed(svc, "rt-reuse", status="ANALYZING", **({"retried_at": "now"} if retried else {}))
    mock_s3vectors._vectors_store["f::cherry"] = {
        "key": "f::cherry",
        "metadata": {"description": "Cherry."},
    }
    _RecordingSummarizer.calls = []
    depths = {"f::apple": 0, "f::banana": 0, "f::cherry": 1}

    try:
        with (
            patch("src.vector.topdown_summarizer.TopDownSummarizer", _RecordingSummarizer),
            patch.object(svc, "get_call_depths", return_value=depths),
        ):
            stats = svc._run_summarization("rt-reuse", Path("/tmp/rt-reuse"), MagicMock())
    finally:
        mock_s3vectors._vectors_store.pop("f::cherry", None)

    assert _RecordingSummarizer.calls == [expected_call]
    assert stats["total_analyzed"] == expected_count


class TestTheWholePipeline:
    """A run that stored one description, then a retry, through the real pipeline."""

    SPACE = "rt-fruit"
    FUNCTIONS = ("apple", "banana", "cherry")

    @pytest.fixture
    def stored_space(self, svc, tmp_path, monkeypatch):
        from src.service.source_provider import SourceCodeProvider

        monkeypatch.setattr(
            svc,
            "source_provider",
            SourceCodeProvider(svc.kb_manager, cache_root=tmp_path / "cache", max_size_gb=0),
        )
        source = tmp_path / "source"
        source.mkdir()
        (source / "fruit.py").write_text(
            "".join(f"def {name}(x):\n    return x + {i}\n\n\n" for i, name in enumerate(self.FUNCTIONS))
        )
        svc._upload_source_archive(self.SPACE, source)
        _seed(svc, self.SPACE)
        return svc

    def _run(self, svc, monkeypatch, describe):
        """Retry with a model that describes only ``describe``. Returns the names it was asked for."""
        import re

        from src.vector.topdown_summarizer import TopDownSummarizer

        asked: set[str] = set()

        def model(_self, _prompt, input_text):
            ids = set(re.findall(r"[\w:.<>-]+\.(?:%s)\b" % "|".join(self.FUNCTIONS), input_text))
            asked.update(i.rsplit(".", 1)[-1] for i in ids)
            answer = {i: f"Describes {i}." for i in ids if i.rsplit(".", 1)[-1] in describe}
            return json.dumps(answer), "end_turn"

        monkeypatch.setattr(TopDownSummarizer, "_call_bedrock", model)
        done = threading.Event()
        real_run = svc._run_pipeline

        def run_and_signal(*args):
            try:
                real_run(*args)
            finally:
                done.set()

        with patch.object(svc, "_run_pipeline", side_effect=run_and_signal):
            svc.retry_space(self.SPACE)
            assert done.wait(60)
        return asked

    def test_a_retry_asks_the_model_only_for_the_missing_descriptions(
        self, stored_space, monkeypatch
    ):
        svc = stored_space

        self._run(svc, monkeypatch, describe={"cherry"})
        interrupted = svc._read_status(self.SPACE)
        asked = self._run(svc, monkeypatch, describe=set(self.FUNCTIONS))

        assert interrupted["status"] == "FAILED"
        assert asked == {"apple", "banana"}
        status = svc._read_status(self.SPACE)
        assert status["status"] == "READY", status.get("error")
        assert status["git_commit_hash"] == "abc123"
        assert status["vector_count"] == 3
