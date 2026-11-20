"""Tests that both pipelines reclaim their working tree on every exit path.

A pipeline clones into ``SOURCE_CACHE_ROOT/<space_id>-analysis`` or
``-refresh``. Those suffixes deliberately place the tree *outside* the source
cache's eviction namespace, because eviction decides what is disposable purely
from the directory name and a tree being written must never be chosen. The cost
of that safety is that nothing else will ever reclaim it: eviction won't, and the
space is typically left in a non-terminal state on failure, so the next run's
pre-clean is unreachable. The owning pipeline's ``finally`` is the only thing
standing between a failed run and a permanently leaked tree.

The tests exercise every way out of ``run()`` and ``_run_pipeline`` — success, a
step writing FAILED and aborting, an unexpected exception escaping, and a run
that died before the clone directory was ever recorded. They also pin the
ordering inside the clone step: the path has to be recorded *before* the clone is
attempted, or a clone that fails halfway leaves a populated tree that the
``finally`` cannot see.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.analysis.errors import RefreshFailed
from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.service.source_provider import (
    WORK_DIR_ANALYSIS,
    WORK_DIR_REFRESH,
    SourceCodeProvider,
    is_scratch_entry,
)


@pytest.fixture(autouse=True)
def _skip_git_url_validation():
    """Neutralize the refresh-clone URL re-validation for this module.

    These tests pin the clone step's path-recording and cleanup, not URL
    validation (that lives in test_git_url_validation.py). _step_clone now
    re-runs validate_git_url before cloning, which would otherwise reject the
    bare example.com fixture URL (no allowlist set, no DNS), so stub it to an
    identity pass-through.
    """
    with patch("src.service.codekb_service.validate_git_url", side_effect=lambda u: u):
        yield


def _populated(path: Path) -> Path:
    """Stand in for a clone: a directory with something in it."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("# cloned\n")
    (path / "pkg").mkdir(exist_ok=True)
    (path / "pkg" / "mod.py").write_text("def f():\n    return 1\n")
    return path


@pytest.fixture
def provider(tmp_path):
    """A provider rooted in tmp_path, so no test touches the real /tmp/codekb."""
    kb_manager = MagicMock()
    kb_manager._get_s3_bucket.return_value = "codekb-knowledgebase"
    return SourceCodeProvider(kb_manager, cache_root=tmp_path, max_size_gb=0)


@pytest.fixture
def codekb_service(provider):
    """A codekb_service stub exposing only what the refresh pipeline reaches for."""
    codekb_service = MagicMock()
    codekb_service.source_provider = provider
    return codekb_service


def _orchestrator(space_id, codekb_service):
    return RefreshOrchestrator(
        space_id,
        {"git_url": "https://example.com/repo.git", "git_commit_hash": "abc123"},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=codekb_service,
        git_ops=MagicMock(),
    )


class TestRunReclaimsTheCloneOnEveryExitPath:
    def test_gone_after_a_successful_refresh(self, tmp_path, codekb_service):
        orch = _orchestrator("space-a", codekb_service)
        clone_dir = tmp_path / f"space-a-{WORK_DIR_REFRESH}"

        def fake_steps():
            orch._clone_dir = _populated(clone_dir)

        orch._run_steps = fake_steps
        orch.run()

        # The success path is the easy one to get wrong: a cleanup placed only in
        # the exception handler leaks on every healthy refresh.
        assert not clone_dir.exists()

    def test_gone_after_a_failed_step(self, tmp_path, codekb_service):
        orch = _orchestrator("space-a", codekb_service)
        clone_dir = tmp_path / f"space-a-{WORK_DIR_REFRESH}"

        def fake_steps():
            orch._clone_dir = _populated(clone_dir)
            raise RefreshFailed("refresh_diff", "could not diff")

        orch._run_steps = fake_steps
        orch.run()

        assert not clone_dir.exists()

    def test_gone_when_an_unexpected_exception_escapes(self, tmp_path, codekb_service):
        orch = _orchestrator("space-a", codekb_service)
        clone_dir = tmp_path / f"space-a-{WORK_DIR_REFRESH}"

        def fake_steps():
            orch._clone_dir = _populated(clone_dir)
            raise RuntimeError("something nobody predicted")

        orch._run_steps = fake_steps

        result = orch.run()

        assert result.final_error_reason == "unexpected_error"
        assert not clone_dir.exists()

    def test_tolerates_a_run_that_never_reached_the_clone_step(self, tmp_path, codekb_service):
        orch = _orchestrator("space-a", codekb_service)

        def fake_steps():
            # _clone_dir is still None: the run died before _step_clone.
            raise RefreshFailed("clone", "git unavailable")

        orch._run_steps = fake_steps

        # Cleanup must not turn a reported pipeline failure into an unhandled
        # TypeError from passing None to a path function.
        orch.run()

        assert orch._clone_dir is None


class TestStepCloneRecordsThePathBeforeCloning:
    def test_a_successful_clone_records_the_same_path(self, tmp_path, codekb_service):
        orch = _orchestrator("space-a", codekb_service)
        expected = tmp_path / f"space-a-{WORK_DIR_REFRESH}"
        orch.git_ops.clone_shallow = lambda url, branch, path, env=None: "newsha"

        orch._step_clone()

        assert orch._clone_dir == expected
        # A working tree, not a cache entry — otherwise eviction could take it
        # while the refresh is still running.
        assert is_scratch_entry(orch._clone_dir.name)

    def test_a_clone_that_fails_partway_is_still_reclaimable(self, tmp_path, codekb_service):
        orch = _orchestrator("space-a", codekb_service)
        clone_dir = tmp_path / f"space-a-{WORK_DIR_REFRESH}"

        def clone_then_die(url, branch, path, env=None):
            # git leaves a partial checkout behind when it fails mid-transfer.
            _populated(path)
            raise RuntimeError("connection reset")

        orch.git_ops.clone_shallow = clone_then_die

        with pytest.raises(RuntimeError):
            orch._step_clone()

        # The assignment has to happen before the clone attempt. If it came
        # after, this partial tree would be invisible to run()'s finally and
        # leak the full size of a half-finished clone.
        assert orch._clone_dir == clone_dir
        assert clone_dir.exists()


class TestAnalysisPipelineReclaimsItsWorkingTree:
    """The analysis pipeline gained the same guarantee as refresh.

    ``_run_pipeline`` is driven directly rather than through ``start_analysis``,
    which would spawn a daemon thread the test cannot join.
    """

    @pytest.fixture
    def codekb_service(self, tmp_path, monkeypatch):
        from src.service.codekb_service import CodeKBService

        codekb_service = object.__new__(CodeKBService)  # skip __init__ and its S3 work
        kb_manager = MagicMock()
        kb_manager._get_s3_bucket.return_value = "codekb-knowledgebase"
        codekb_service.kb_manager = kb_manager
        codekb_service.source_provider = SourceCodeProvider(kb_manager, cache_root=tmp_path, max_size_gb=0)
        codekb_service._update_pipeline_status = MagicMock()
        codekb_service._write_status = MagicMock()
        codekb_service._read_status = MagicMock(return_value={})
        codekb_service.invalidate_context = MagicMock()
        return codekb_service

    def test_gone_after_a_successful_analysis(self, tmp_path, codekb_service):
        work_dir = tmp_path / f"space-a-{WORK_DIR_ANALYSIS}"
        codekb_service._finalize_pipeline = MagicMock()
        codekb_service.orchestrator = MagicMock()
        codekb_service.orchestrator.analyze_space.return_value = MagicMock(
            knowledge_base=MagicMock(get_component_count=lambda: 3)
        )

        codekb_service._run_pipeline(
            "space-a",
            work_dir,
            summarize=False,
            omit_low_business_relevance=False,
            acquire_source=lambda: {"source_type": "git"},
        )

        # Non-vacuity guard. Every exit path reclaims the tree, so without
        # proving the run actually reached the end this test would also pass on
        # the exception path and tell us nothing about the success path.
        codekb_service._finalize_pipeline.assert_called_once()
        assert not work_dir.exists()

    def test_gone_when_source_acquisition_fails(self, tmp_path, codekb_service):
        work_dir = tmp_path / f"space-a-{WORK_DIR_ANALYSIS}"
        codekb_service.orchestrator = MagicMock()

        # acquire_source returning None makes _run_pipeline return early, after
        # the directory has already been created. That early return is a path a
        # cleanup in the except block would miss entirely.
        codekb_service._run_pipeline(
            "space-a",
            work_dir,
            summarize=False,
            omit_low_business_relevance=False,
            acquire_source=lambda: None,
        )

        assert not work_dir.exists()

    def test_gone_when_the_pipeline_raises(self, tmp_path, codekb_service):
        work_dir = tmp_path / f"space-a-{WORK_DIR_ANALYSIS}"
        codekb_service.orchestrator = MagicMock()
        codekb_service.orchestrator.analyze_space.side_effect = RuntimeError("parser exploded")

        codekb_service._run_pipeline(
            "space-a",
            work_dir,
            summarize=False,
            omit_low_business_relevance=False,
            acquire_source=lambda: {"source_type": "git"},
        )

        # A failed analysis leaves a half-populated tree that is useless to
        # anyone, so keeping it would hold disk a working space could use.
        assert not work_dir.exists()
        codekb_service._write_status.assert_called()

    def test_the_stale_cache_entry_is_dropped_when_analysis_starts(self, tmp_path, codekb_service):
        # Re-analysing a space replaces its source, so a cached tree from the
        # previous revision must not survive to serve stale reads.
        stale_cache = _populated(tmp_path / "space-a")
        work_dir = tmp_path / f"space-a-{WORK_DIR_ANALYSIS}"
        codekb_service.orchestrator = MagicMock()

        codekb_service._run_pipeline(
            "space-a",
            work_dir,
            summarize=False,
            omit_low_business_relevance=False,
            acquire_source=lambda: None,
        )

        assert not stale_cache.exists()

    def test_the_working_tree_is_not_the_cache_entry(self, tmp_path, codekb_service):
        # The bug this whole split fixes: one directory serving as both a
        # disposable cache entry and a running pipeline's working tree.
        assert codekb_service._analysis_work_path("space-a") != codekb_service._source_local_path("space-a")
        assert is_scratch_entry(codekb_service._analysis_work_path("space-a").name)
        assert not is_scratch_entry(codekb_service._source_local_path("space-a").name)
