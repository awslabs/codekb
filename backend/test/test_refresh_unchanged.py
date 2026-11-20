"""A refresh at the already-indexed commit ends READY after the clone (L4)."""

from unittest.mock import MagicMock

import pytest

from src.analysis.errors import GitCommandError, RefreshFailed
from src.analysis.refresh_orchestrator import RefreshOrchestrator

LANGUAGE_SET_FIELD = RefreshOrchestrator.LANGUAGE_SET_FIELD


def _orchestrator(tmp_path, monkeypatch, **prior_status):
    monkeypatch.setattr("src.service.codekb_service.validate_git_url", lambda url: url)
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={
            "git_url": "https://github.com/o/r.git",
            "git_commit_hash": "abc",
            LANGUAGE_SET_FIELD: ["python"],
            **prior_status,
        },
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "app.py").write_text("def a():\n    return 1\n")
    orch.codekb_service.source_provider.work_path.return_value = clone
    orch._compute_current_language_set = lambda: ["python"]
    return orch


def _stop_at_parse(orch):
    def stop():
        raise RefreshFailed(step="parse_changed", reason="stop", detail="")

    orch._step_parse_changed = stop


@pytest.fixture
def orchestrator(tmp_path, monkeypatch):
    return _orchestrator(tmp_path, monkeypatch)


def test_same_commit_skips_everything_after_the_clone(orchestrator):
    orchestrator.git_ops.clone_shallow.return_value = "abc"

    result = orchestrator.run()

    assert result.final_error_step is None
    orchestrator.git_ops.fetch_specific_sha.assert_not_called()
    orchestrator.kb_manager.import_knowledge_base.assert_not_called()
    orchestrator.kb_manager._write_staging_kb.assert_not_called()
    last = orchestrator.codekb_service._update_pipeline_status.call_args
    assert last.args[:4] == ("space", "READY", "unchanged", "completed")
    assert last.kwargs["refresh_run"] is True
    # Zeros replace the previous refresh's summary.
    summary = last.kwargs["extra"]["refresh_result"]
    assert (summary["files_added"], summary["files_modified"], summary["vectors_upserted"]) == (0, 0, 0)


def test_a_new_commit_goes_on_to_fetch_the_prior_one(orchestrator):
    orchestrator.git_ops.clone_shallow.return_value = "def"
    # Stop the run at the diff; only the fetch before it matters here.
    orchestrator.git_ops.diff_name_status.side_effect = GitCommandError("git diff", 128, "stop")

    orchestrator.run()

    orchestrator.git_ops.fetch_specific_sha.assert_called_once()


def test_same_commit_with_a_new_grammar_indexes_its_files(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path, monkeypatch)
    orch._compute_current_language_set = lambda: ["dart", "python"]

    def clone(*args, **kwargs):
        (tmp_path / "clone" / "lib").mkdir(parents=True)
        (tmp_path / "clone" / "lib" / "main.dart").write_text("void main() {}\n")
        return "abc"

    orch.git_ops.clone_shallow.side_effect = clone
    _stop_at_parse(orch)

    result = orch.run()

    assert result.final_error_step == "parse_changed"
    assert orch._injected_added_paths == frozenset({"lib/main.dart"})
    orch.git_ops.fetch_specific_sha.assert_not_called()


def test_same_commit_after_a_failed_refresh_runs_in_full(tmp_path, monkeypatch):
    orch = _orchestrator(tmp_path, monkeypatch, status="REFRESH_FAILED")
    orch.git_ops.clone_shallow.return_value = "abc"
    _stop_at_parse(orch)

    result = orch.run()

    assert result.final_error_step == "parse_changed"
