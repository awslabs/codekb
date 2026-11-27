"""Refresh of a V1 (stem-id) Space where two files share a stem.

Under IdScheme.V1_STEM, a/server.py and b/server.py both yield ids like
"space::server.run". When one file changes and the other does not, the id used
to land in both preserved_component_ids and evicted_component_ids, and
RefreshPlan raised RefreshFailed(step="merge", reason="invariant_violation").
"""

from pathlib import Path
from unittest.mock import MagicMock

from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.core.models import ChangeType, Component, ComponentType, FileChange, KnowledgeBase


def _comp(cid: str, file: str, content_hash: str = "h0") -> Component:
    return Component(
        id=cid,
        type=ComponentType.FUNCTION,
        file_path=Path(file),
        line_number=1,
        end_line_number=2,
        language="python",
        space_id="space",
        content_hash=content_hash,
    )


def _orchestrator(tmp_path: Path, prior: list[Component], parsed: list[Component], changes):
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={"git_commit_hash": "old"},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch.kb_manager.import_knowledge_base.return_value = KnowledgeBase(
        source_directory=tmp_path, space_id="space", components=prior, call_graph={}
    )
    orch._clone_dir = tmp_path
    orch._file_changes = changes
    orch._parsed_components = parsed
    return orch


def test_shared_stem_one_file_modified_does_not_fail(tmp_path):
    # The prior KB keeps one component per id; here it came from a/server.py.
    prior = [
        _comp("space::server.run", "a/server.py"),
        _comp("space::server.other", "b/server.py"),
    ]
    # b/server.py changed and now also defines run().
    parsed = [_comp("space::server.run", "b/server.py", content_hash="h1")]
    orch = _orchestrator(tmp_path, prior, parsed, [FileChange("b/server.py", ChangeType.MODIFIED)])

    orch._step_merge()  # raised RefreshFailed(invariant_violation) before the fix

    plan = orch._refresh_plan
    assert not (plan.preserved_component_ids & plan.evicted_component_ids)
    # The fresh parse wins the shared id.
    assert "space::server.run" not in plan.preserved_component_ids
    assert "space::server.run" in plan.changed_component_ids
    # The component that disappeared from b/server.py is still removed.
    assert "space::server.other" in plan.vectors_to_delete


def test_shared_stem_id_kept_when_modified_file_no_longer_parses_it(tmp_path):
    # The prior KB listed the id under both files (written before the
    # one-component-per-id rule); the modified file no longer defines it.
    prior = [
        _comp("space::server.run", "a/server.py"),
        _comp("space::server.run", "b/server.py"),
    ]
    orch = _orchestrator(tmp_path, prior, [], [FileChange("b/server.py", ChangeType.MODIFIED)])

    orch._step_merge()

    plan = orch._refresh_plan
    assert "space::server.run" in plan.preserved_component_ids
    assert "space::server.run" not in plan.evicted_component_ids
    assert "space::server.run" not in plan.vectors_to_delete
