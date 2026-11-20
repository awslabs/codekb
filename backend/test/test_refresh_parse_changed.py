"""Refresh parses changed files with creation's analyze_directory rules (M19 step 1)."""

from pathlib import Path
from unittest.mock import MagicMock

from src.analysis.code_analyzer import CodeAnalyzer
from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.catalog.component_catalog import ComponentCatalog
from src.core.models import ChangeType, FileChange, IdScheme


def _refresh(root: Path, changed: list[str]) -> RefreshOrchestrator:
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={"git_commit_hash": "old"},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch._clone_dir = root
    orch._file_changes = [FileChange(path, ChangeType.MODIFIED) for path in changed]
    orch._step_parse_changed()
    return orch


def test_a_changed_file_over_the_size_cap_is_skipped_as_at_creation(tmp_path):
    (tmp_path / "small.py").write_text("def small():\n    return 1\n")
    # One byte over the 10 MB analysis.max_file_size_mb default.
    (tmp_path / "big.py").write_text("def big():\n    return 1\n" + "#" * (10 * 1024 * 1024))

    orch = _refresh(tmp_path, ["small.py", "big.py"])

    refreshed = {c.id for c in orch._parsed_components}
    created = {c.id for c in CodeAnalyzer(ComponentCatalog()).analyze_directory(tmp_path, "space", id_scheme=IdScheme.V1_STEM).components}
    assert refreshed == created == {"space::small.small"}
    assert {c.file_path for c in orch._parsed_components} == {Path("small.py")}
