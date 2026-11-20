"""vector_count counts vectors, not re-descriptions (L3)."""

from pathlib import Path
from unittest.mock import MagicMock

from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.core.models import ChangeType, Component, ComponentType, FileChange, KnowledgeBase


def _comp(cid: str, file: str, content_hash: str, kind=ComponentType.METHOD) -> Component:
    return Component(
        id=cid,
        type=kind,
        file_path=Path(file),
        line_number=1,
        end_line_number=5,
        language="java",
        space_id="space",
        content_hash=content_hash,
    )


def test_overwrites_add_nothing_and_only_removed_functions_subtract():
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={"git_commit_hash": "old", "vector_count": 5},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    # Five functions and methods had vectors; the class Old never did.
    orch.kb_manager.import_knowledge_base.return_value = KnowledgeBase(
        source_directory=Path("/repo"),
        space_id="space",
        components=[
            _comp("space::R.save", "R.java", "hs"),
            _comp("space::R.count", "R.java", "hc"),
            _comp("space::S.doStuff", "S.java", "hd"),
            _comp("space::Old", "Old.java", "ho", kind=ComponentType.CLASS),
            _comp("space::Old.run", "Old.java", "hr"),
            _comp("space::Old.stop", "Old.java", "hp"),
        ],
        call_graph={"space::S.doStuff": [["space::R.save", 3]]},
    )
    # R.save changes, R.count is gone, R.added is new; Old.java is deleted.
    orch._file_changes = [
        FileChange("R.java", ChangeType.MODIFIED),
        FileChange("Old.java", ChangeType.REMOVED),
    ]
    orch._parsed_components = [
        _comp("space::R.save", "R.java", "hs2"),
        _comp("space::R.added", "R.java", "ha"),
    ]
    orch._step_merge()

    # R.save and its caller S.doStuff overwrite their vectors; R.added gets a new one.
    orch._summarized_descriptions = {
        "space::R.save": "d",
        "space::S.doStuff": "d",
        "space::R.added": "d",
    }
    # Old.stop's delete failed, so its vector is still there.
    orch._vector_errors = ["space::Old.stop"]

    # 5 + 1 (R.added) - 2 (R.count, Old.run); the class Old had no vector.
    assert orch._vector_count() == 4
