"""Tests for exclusion filtering in the refresh diff step.

A space keeps the exclusion patterns it was created with for its whole life, so
refresh filters the git diff with the patterns recorded in the space's status.
Historical spaces, which have none recorded, use the legacy default.yaml list.
"""

from pathlib import Path
from unittest.mock import MagicMock

from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.core.models import ChangeType, FileChange

CHANGES = [
    FileChange("src/app.py", ChangeType.ADDED),
    FileChange("node_modules/pkg/index.js", ChangeType.ADDED),
    FileChange(".claude/notes.py", ChangeType.MODIFIED),
    FileChange("tests/test_app.py", ChangeType.REMOVED),
]


def _run_diff(prior_status: dict) -> RefreshOrchestrator:
    git_ops = MagicMock()
    git_ops.diff_name_status.return_value = list(CHANGES)
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={"git_commit_hash": "old", **prior_status},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=git_ops,
    )
    orch._clone_dir = Path("/clone")
    orch._new_commit_hash = "new"
    orch._step_refresh_diff()
    return orch


def test_applies_patterns_recorded_at_creation():
    orch = _run_diff({"exclude_patterns": ["node_modules", ".claude/"]})
    assert [fc.path for fc in orch._file_changes] == ["src/app.py", "tests/test_app.py"]


def test_historical_space_uses_legacy_default_patterns():
    orch = _run_diff({})
    # The legacy list excludes node_modules and test*, but not .claude/.
    assert [fc.path for fc in orch._file_changes] == ["src/app.py", ".claude/notes.py"]


def test_recorded_empty_patterns_exclude_nothing():
    orch = _run_diff({"exclude_patterns": []})
    assert [fc.path for fc in orch._file_changes] == [fc.path for fc in CHANGES]


def test_counts_reflect_filtered_changes():
    orch = _run_diff({"exclude_patterns": ["node_modules", ".claude/"]})
    completed = orch.codekb_service._update_pipeline_status.call_args_list[-1]
    counts = completed.kwargs["extra"]["refresh_diff"]
    assert (counts["added_files"], counts["modified_files"], counts["removed_files"]) == (1, 0, 1)
