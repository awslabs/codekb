"""The local source switches with the code list, right after the KB swap."""

from pathlib import Path
from unittest.mock import MagicMock

from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.core.models import KnowledgeBase, RefreshPlan


def _orchestrator() -> RefreshOrchestrator:
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={"git_commit_hash": "old"},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch.kb_manager._commit_staging_kb.return_value = MagicMock(success=True, refresh_errors={})
    orch._prior_kb = KnowledgeBase(
        source_directory=Path("/repo"), space_id="space", components=[], call_graph={}
    )
    orch._refresh_plan = RefreshPlan()
    return orch


def _names(service: MagicMock) -> list[str]:
    return [name for name, _, _ in service.mock_calls]


def test_commit_clears_the_source_before_its_slower_bookkeeping():
    orch = _orchestrator()

    orch._step_commit_kb()

    calls = _names(orch.codekb_service)
    cleared = calls.index("source_provider.invalidate")
    assert calls.index("invalidate_context") < cleared
    assert cleared < calls.index("orchestrator._initialize_context")


def test_the_invalidate_step_does_not_clear_a_second_time():
    orch = _orchestrator()
    orch._step_commit_kb()
    orch.codekb_service.reset_mock()

    orch._step_invalidate_caches()

    assert "source_provider.invalidate" not in _names(orch.codekb_service)


def test_the_invalidate_step_retries_when_the_commit_could_not_clear():
    orch = _orchestrator()
    orch.codekb_service.source_provider.invalidate.side_effect = [OSError("busy"), None]
    orch._step_commit_kb()

    orch._step_invalidate_caches()

    assert orch.codekb_service.source_provider.invalidate.call_count == 2
    statuses = [c.args[2:4] for c in orch.codekb_service._update_pipeline_status.call_args_list]
    assert ("invalidate_caches", "completed") in statuses
