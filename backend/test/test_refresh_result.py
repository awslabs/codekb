"""The final READY status carries the run's refresh_result (P504279533)."""

from pathlib import Path
from unittest.mock import MagicMock

from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.core.models import ChangeType, Component, ComponentType, FileChange, KnowledgeBase


def _comp(cid: str, file: str, content_hash: str) -> Component:
    return Component(
        id=cid,
        type=ComponentType.METHOD,
        file_path=Path(file),
        line_number=1,
        end_line_number=5,
        language="java",
        space_id="space",
        content_hash=content_hash,
    )


def test_the_ready_status_records_the_refresh_result():
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={"git_commit_hash": "old", "vector_count": 3},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch.codekb_service._read_status.return_value = {}
    orch.kb_manager.import_knowledge_base.return_value = KnowledgeBase(
        source_directory=Path("/repo"),
        space_id="space",
        components=[
            _comp("space::R.save", "R.java", "hs"),
            _comp("space::S.run", "S.java", "hr"),
            _comp("space::Old.stop", "Old.java", "ho"),
        ],
        call_graph={},
    )
    # R.java changes save and adds one method; Old.java is deleted.
    orch._file_changes = [
        FileChange("R.java", ChangeType.MODIFIED),
        FileChange("Old.java", ChangeType.REMOVED),
    ]
    orch._parsed_components = [
        _comp("space::R.save", "R.java", "hs2"),
        _comp("space::R.added", "R.java", "ha"),
    ]
    orch._step_merge()
    orch._new_commit_hash = "new"
    orch._vectors_upserted = 2
    orch._vectors_deleted = 1

    orch._step_mark_overviews_stale()

    last = orch.codekb_service._update_pipeline_status.call_args
    assert last.args[:2] == ("space", "READY")
    result = last.kwargs["extra"]["refresh_result"]
    assert (result["files_modified"], result["files_removed"], result["files_unchanged"]) == (1, 1, 1)
    assert (result["components_added"], result["components_removed"]) == (1, 1)
    assert (result["vectors_upserted"], result["vectors_deleted"]) == (2, 1)
    assert result["duration_s"] >= 0
