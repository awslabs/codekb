"""Refresh summarization with AI descriptions stored in S3 Vectors only, never in the KB."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.analysis.errors import RefreshFailed
from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.core.llm_settings import Workload
from src.core.llm_settings import resolve as resolve_llm_settings
from src.core.models import ChangeType, Component, ComponentType, FileChange, KnowledgeBase
from src.core.token_usage import TokenUsage

SUMMARIZED_STATUS = {
    "git_commit_hash": "old",
    "pipeline_steps": [{"step": "summarize", "status": "completed"}],
    "vector_count": 4,
}


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


def _prior_kb() -> KnowledgeBase:
    # Default space: no descriptions in the KB. S.doStuff -> R.save -> R.helper.
    return KnowledgeBase(
        source_directory=Path("/repo"),
        space_id="space",
        components=[
            _comp("space::R.save", "R.java", "hs"),
            _comp("space::R.helper", "R.java", "hh"),
            _comp("space::S.doStuff", "S.java", "hd"),
            _comp("space::Q.unrelated", "Q.java", "hq"),
        ],
        call_graph={
            "space::S.doStuff": [["space::R.save", 3]],
            "space::R.save": [["space::R.helper", 4]],
        },
    )


class _FakeSummarizer:
    """Stands in for TopDownSummarizer: indexes every target, records its inputs.

    ``stop_reason`` makes it report a guardrail stop after indexing the targets.
    """

    instances: list["_FakeSummarizer"] = []
    stop_reason: str | None = None

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self._indexed_cache: set[str] = set()
        self._description_cache: dict[str, str] = {}
        self.token_usage = TokenUsage()
        self._bedrock_region = "us-east-1"
        self._llm = resolve_llm_settings(Workload.SUMMARIZATION)
        self.targets: set[str] | None = None
        self.prior_descriptions: dict[str, str] | None = None
        _FakeSummarizer.instances.append(self)

    async def summarize_all(self, targets=None, prior_descriptions=None):
        self.targets = set(targets)
        self.prior_descriptions = dict(prior_descriptions)
        for cid in targets:
            self._indexed_cache.add(cid)
            self._description_cache[cid] = f"AI text for {cid}"
        return {"total_analyzed": len(targets), "stopped_early": self.stop_reason}


@pytest.fixture(autouse=True)
def fake_summarizer(monkeypatch):
    _FakeSummarizer.instances = []
    _FakeSummarizer.stop_reason = None
    monkeypatch.setattr("src.vector.topdown_summarizer.TopDownSummarizer", _FakeSummarizer)
    return _FakeSummarizer


def _orchestrator(prior_status: dict, tmp_path: Path) -> RefreshOrchestrator:
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status=prior_status,
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch.kb_manager.import_knowledge_base.return_value = _prior_kb()
    orch.vectors_client.get_descriptions.return_value = {"space::R.helper": "Helps save."}
    orch._clone_dir = tmp_path
    # R.save changed; R.helper is unchanged in the same file.
    orch._file_changes = [FileChange("R.java", ChangeType.MODIFIED)]
    orch._parsed_components = [
        _comp("space::R.save", "R.java", "hs2"),
        _comp("space::R.helper", "R.java", "hh"),
    ]
    orch._step_merge()
    return orch


def test_summarizes_when_creation_did_even_though_kb_has_no_descriptions(tmp_path):
    orch = _orchestrator(SUMMARIZED_STATUS, tmp_path)

    orch._step_summarize_changed()

    assert orch._summarization_skipped is False
    (summarizer,) = _FakeSummarizer.instances
    assert summarizer.targets == {"space::R.save", "space::S.doStuff"}


def test_callee_context_comes_from_the_vector_index(tmp_path):
    orch = _orchestrator(SUMMARIZED_STATUS, tmp_path)

    orch._step_summarize_changed()

    # Only the unchanged callee is fetched: not targets, not unreachable components.
    orch.vectors_client.get_descriptions.assert_called_once_with(["space::R.helper"])
    (summarizer,) = _FakeSummarizer.instances
    assert summarizer.prior_descriptions == {"space::R.helper": "Helps save."}


def test_new_descriptions_stay_out_of_the_kb(tmp_path):
    orch = _orchestrator(SUMMARIZED_STATUS, tmp_path)

    orch._step_summarize_changed()

    assert set(orch._summarized_descriptions) == {"space::R.save", "space::S.doStuff"}
    kb_components = orch._prior_kb.components + orch._parsed_components
    assert all(not c.business_behavior.description for c in kb_components)


@pytest.mark.parametrize("omit", [True, False])
def test_summarizer_gets_the_space_business_relevance_setting(tmp_path, omit):
    orch = _orchestrator({**SUMMARIZED_STATUS, "omit_low_business_relevance": omit}, tmp_path)

    orch._step_summarize_changed()

    (summarizer,) = _FakeSummarizer.instances
    assert summarizer.kwargs["omit_low_business_relevance"] is omit


def test_heuristic_analyzer_stays_off_for_a_default_space(tmp_path):
    orch = _orchestrator(SUMMARIZED_STATUS, tmp_path)

    orch._step_summarize_changed()

    init = orch.codekb_service.orchestrator._initialize_context
    assert init.call_args.kwargs == {"business_behavior_enabled": False}


def test_space_whose_summarize_record_was_overwritten_by_old_refresh_is_summarized(tmp_path):
    # Seen in prod: older refresh code replaced pipeline_steps with its own steps.
    status = {
        "git_commit_hash": "old",
        "vector_count": 4,
        "pipeline_steps": [
            {"step": "clone", "status": "completed"},
            {"step": "summarize_changed", "status": "completed"},
        ],
    }
    orch = _orchestrator(status, tmp_path)

    orch._step_summarize_changed()

    assert orch._summarization_skipped is False


def test_space_created_without_summarization_is_skipped(tmp_path):
    orch = _orchestrator({"git_commit_hash": "old", "vector_count": 0}, tmp_path)

    orch._step_summarize_changed()

    assert orch._summarization_skipped is True
    assert _FakeSummarizer.instances == []
    orch.vectors_client.get_descriptions.assert_not_called()


def test_vector_read_failure_fails_the_step_before_any_llm_call(tmp_path):
    orch = _orchestrator(SUMMARIZED_STATUS, tmp_path)
    orch.vectors_client.get_descriptions.side_effect = RuntimeError("throttled")

    with pytest.raises(RefreshFailed) as exc_info:
        orch._step_summarize_changed()

    assert exc_info.value.reason == "prior_descriptions_unavailable"
    assert _FakeSummarizer.instances == []


def test_reachable_within_honours_the_hop_limit():
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    graph = {"a": [["b", 1]], "b": [["c", 1]], "c": [["d", 1]]}

    assert orch._reachable_within(graph, frozenset({"a"}), 2) == {"b", "c"}
    assert orch._reachable_within(graph, frozenset({"a"}), 10) == {"b", "c", "d"}


def test_prompt_hops_excludes_the_function_itself():
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )

    # default.yaml: call_graph_depth 10 = the function plus 9 levels of callees.
    assert orch._prompt_hops() == 9


# ── Vectors, then the KB, then git_commit_hash: a failed refresh is retried ──


def _bare_orchestrator(prior_status: dict | None = None) -> RefreshOrchestrator:
    return RefreshOrchestrator(
        space_id="space",
        prior_status=prior_status or {"git_commit_hash": "old"},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )


def _stub_steps(orch: RefreshOrchestrator, calls: list[str], fail_at: str | None = None):
    for name in [n for n in dir(orch) if n.startswith("_step_")]:

        def step(n=name):
            calls.append(n)
            if n == fail_at:
                raise RefreshFailed(step=n.removeprefix("_step_"), reason="boom", detail="test")

        setattr(orch, name, step)


def _hash_writes(orch: RefreshOrchestrator) -> list:
    return [
        c
        for c in orch.codekb_service._update_pipeline_status.call_args_list
        if "git_commit_hash" in (c.kwargs.get("extra") or {})
    ]


def test_vectors_are_written_before_the_kb_and_the_hash_last():
    orch = _bare_orchestrator()
    calls: list[str] = []
    _stub_steps(orch, calls)

    orch._run_steps()

    assert calls.index("_step_summarize_changed") < calls.index("_step_stage_kb")
    assert calls.index("_step_vector_deletes") < calls.index("_step_commit_kb")
    assert calls[-1] == "_step_mark_overviews_stale"


@pytest.mark.parametrize(
    "fail_at", ["_step_clone", "_step_summarize_changed", "_step_commit_kb"]
)
def test_failed_refresh_ends_refresh_failed_without_moving_the_hash(fail_at):
    orch = _bare_orchestrator()
    _stub_steps(orch, [], fail_at=fail_at)

    result = orch.run()

    step = fail_at.removeprefix("_step_")
    assert result.final_error_step == step
    final = orch.codekb_service._update_pipeline_status.call_args
    assert final.args[1] == "REFRESH_FAILED"
    assert final.args[3] == "failed"
    assert final.kwargs["extra"]["error"].startswith(f"[refresh] {step}:")
    # Routed into the refresh run even when the failed step is shared with creation.
    assert final.kwargs["refresh_run"] is True
    assert _hash_writes(orch) == []


def test_clone_keeps_the_new_hash_out_of_status(tmp_path, monkeypatch):
    monkeypatch.setattr("src.service.codekb_service.validate_git_url", lambda url: url)
    orch = _bare_orchestrator({"git_commit_hash": "old", "git_url": "https://github.com/o/r"})
    orch.codekb_service.source_provider.work_path.return_value = tmp_path / "clone"
    orch.git_ops.clone_shallow.return_value = "new"

    orch._step_clone()

    assert orch._new_commit_hash == "new"
    assert _hash_writes(orch) == []


def test_kb_swap_failure_fails_the_refresh():
    orch = _bare_orchestrator()
    orch.kb_manager._commit_staging_kb.return_value = MagicMock(
        success=False, refresh_errors={"kb_swap": "CopyObject failed"}
    )

    with pytest.raises(RefreshFailed) as exc_info:
        orch._step_commit_kb()

    assert exc_info.value.reason == "kb_swap_failed"


@pytest.mark.parametrize(
    "stop_reason, remedy",
    [
        # A retry stops at the same point, so the fix is the budget, not Refresh space alone.
        (
            "token_budget_reached",
            "Ask an administrator to raise the token budget, then use Refresh space.",
        ),
        ("circuit_breaker_open", "Use Refresh space to try again."),
    ],
)
def test_stopped_early_summarization_fails_the_refresh(tmp_path, stop_reason, remedy):
    _FakeSummarizer.stop_reason = stop_reason
    orch = _orchestrator(SUMMARIZED_STATUS, tmp_path)

    with pytest.raises(RefreshFailed) as exc_info:
        orch._step_summarize_changed()

    assert exc_info.value.reason == "stopped_early"
    assert exc_info.value.detail.endswith(remedy)
    # The user-facing text names no config key or figure.
    assert "token_budget" not in exc_info.value.detail
    # The tokens it spent are still recorded for the failure status.
    assert orch._refresh_token_usage is not None


def test_merged_context_is_cached_as_most_recently_used(tmp_path):
    import threading
    from collections import OrderedDict

    orch = _orchestrator(SUMMARIZED_STATUS, tmp_path)
    service = orch.codekb_service
    service._context_cache = OrderedDict([("space", "old"), ("other", "o")])
    service._context_cache_lock = threading.Lock()

    orch._inject_merged_context()

    assert list(service._context_cache) == ["other", "space"]
    assert service._context_cache["space"] is service.orchestrator._initialize_context.return_value
