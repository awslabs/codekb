"""A refresh persists what a full index of the same tree would."""

import copy
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.analysis.code_analyzer import CodeAnalyzer
from src.analysis.orchestrator import AnalysisOrchestrator
from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.catalog.component_catalog import ComponentCatalog
from src.core.file_utils import LEGACY_DEFAULT_EXCLUDE_PATTERNS
from src.core.models import ChangeType, FileChange, KnowledgeBase, RefreshPlan

CALLER = "from r import helper\n\n\ndef caller():\n    return helper()\n"
HELPER = "def helper():\n    return 1\n"


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)


def _full_index(root: Path) -> KnowledgeBase:
    analyzer = CodeAnalyzer(ComponentCatalog(None), exclude_patterns=LEGACY_DEFAULT_EXCLUDE_PATTERNS)
    return analyzer.analyze_directory(root, "space")


def _orchestrator(tmp_path: Path, clone: Path, prior_kb: KnowledgeBase) -> RefreshOrchestrator:
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={"git_commit_hash": "old", "statistics": {"directory": "/work/space"}},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch.kb_manager.import_knowledge_base.return_value = prior_kb
    orch.codekb_service.source_provider.cache_path.return_value = tmp_path / "cache" / "space"
    orch._clone_dir = clone
    orch._prior_kb = prior_kb
    return orch


def _prior_kb(root: Path, *, analysis_metadata: dict | None = None) -> KnowledgeBase:
    kb = _full_index(root)
    for component in kb.components:
        component.file_path = component.file_path.relative_to(root)
    kb.call_graph = {}
    if analysis_metadata is not None:
        kb.analysis_metadata = analysis_metadata
    return kb


@pytest.fixture
def clone(tmp_path):
    root = tmp_path / "clone"
    _write(
        root,
        {
            "s.py": CALLER,
            "pkg/back\\slash.py": "def tally():\n    return 2\n",
            "node_modules/vendored.py": "def vendored():\n    pass\n",
        },
    )
    return root


def test_staged_paths_sit_under_the_source_root_as_creation_stores_them(tmp_path, clone):
    prior_kb = _prior_kb(clone)
    orch = _orchestrator(tmp_path, clone, prior_kb)

    orch._step_stage_kb()

    staged = orch.kb_manager._write_staging_kb.call_args.args[1]
    root = tmp_path / "cache" / "space"
    assert sorted(c["file_path"] for c in staged["components"]) == sorted(
        str(root / c.file_path) for c in prior_kb.components
    )
    # Later steps still see repo-relative paths.
    assert all(not c.file_path.is_absolute() for c in prior_kb.components)


def test_staged_totals_match_a_full_index_of_the_clone(tmp_path, clone):
    stale = {
        "files_analyzed": 99,
        "total_components": 99,
        "total_lines": 999,
        "languages_detected": ["javascript", "python"],
        "skipped_files": {"cobol": 3},
        "skipped_file_count": 3,
    }
    prior_kb = _prior_kb(clone, analysis_metadata=dict(stale))
    orch = _orchestrator(tmp_path, clone, prior_kb)

    orch._step_stage_kb()

    staged = orch.kb_manager._write_staging_kb.call_args.args[1]["analysis_metadata"]
    fresh = _full_index(clone).analysis_metadata
    for key in ("files_analyzed", "total_components", "total_lines", "skipped_files", "skipped_file_count"):
        assert staged[key] == fresh[key], key
    assert sorted(staged["languages_detected"]) == sorted(fresh["languages_detected"])


def test_staged_totals_leave_out_files_a_full_index_cannot_parse_or_read(tmp_path, clone, monkeypatch):
    from src.core.logging_utils import ParseError
    from src.core.tree_sitter_wrapper import TreeSitterWrapper

    parse, read_text = TreeSitterWrapper.parse_file, Path.read_text

    def failing_parse(self, file_path, language):
        if file_path.name == "broken.py":
            raise ParseError("Tree-sitter parsing failed: boom", file_path)
        if file_path.name == "locked.py":
            raise ParseError("Failed to read file: permission denied", file_path)
        return parse(self, file_path, language)

    # Faked rather than chmod(0), which doesn't stop root (CI containers) from reading.
    def failing_read_text(self, *args, **kwargs):
        if self.name == "locked.py":
            raise PermissionError(13, "Permission denied", str(self))
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(TreeSitterWrapper, "parse_file", failing_parse)
    monkeypatch.setattr(Path, "read_text", failing_read_text)
    _write(clone, {"broken.py": "def broken():\n    pass\n", "locked.py": "def locked():\n    pass\n"})
    prior_kb = _prior_kb(clone)
    orch = _orchestrator(tmp_path, clone, prior_kb)
    orch._file_changes = [FileChange("broken.py", ChangeType.ADDED)]
    orch._step_parse_changed()

    staged = orch._whole_tree_totals(clone, prior_kb.components)
    fresh = _full_index(clone).analysis_metadata

    assert fresh["files_analyzed"] == 2  # s.py and the backslash file
    for key in ("files_analyzed", "total_lines"):
        assert staged[key] == fresh[key], key
    assert sorted(staged["languages_detected"]) == sorted(fresh["languages_detected"])


def test_an_unchanged_file_creation_could_not_parse_stays_out_until_it_parses(tmp_path, clone, monkeypatch):
    from src.core.logging_utils import ParseError
    from src.core.tree_sitter_wrapper import TreeSitterWrapper

    parse, failing = TreeSitterWrapper.parse_file, {"bad.py"}

    def failing_parse(self, file_path, language):
        if file_path.name in failing:
            raise ParseError("Tree-sitter parsing failed: boom", file_path)
        return parse(self, file_path, language)

    def refresh(prior_kb, changed):
        orch = _orchestrator(tmp_path, clone, prior_kb)
        orch._file_changes = [FileChange(changed, ChangeType.MODIFIED)]
        orch._step_parse_changed()
        orch._step_stage_kb()
        return orch.kb_manager._write_staging_kb.call_args.args[1]["analysis_metadata"]

    def assert_matches_full_index(staged):
        fresh = _full_index(clone).analysis_metadata
        for key in ("files_analyzed", "total_lines", "unparsed_files"):
            assert staged[key] == fresh[key], key

    monkeypatch.setattr(TreeSitterWrapper, "parse_file", failing_parse)
    _write(clone, {"bad.py": "def bad():\n    pass\n"})
    prior_kb = _prior_kb(clone)
    assert prior_kb.analysis_metadata["unparsed_files"] == ["bad.py"]

    _write(clone, {"s.py": CALLER + "\n\ndef more():\n    return 3\n"})
    staged = refresh(prior_kb, "s.py")
    assert staged["unparsed_files"] == ["bad.py"]
    assert_matches_full_index(staged)

    # Once the file parses, a refresh of it counts it again.
    failing.clear()
    staged = refresh(_prior_kb(clone, analysis_metadata=dict(staged)), "bad.py")
    assert staged["unparsed_files"] == []
    assert_matches_full_index(staged)


def test_commit_writes_the_statistics_creation_would_show(tmp_path, clone):
    prior_kb = _prior_kb(clone)
    orch = _orchestrator(tmp_path, clone, prior_kb)
    orch.codekb_service.orchestrator = AnalysisOrchestrator(MagicMock())
    orch.kb_manager._commit_staging_kb.return_value = MagicMock(success=True, refresh_errors={})
    orch._refresh_plan = RefreshPlan()

    orch._step_commit_kb()

    completed = [
        call
        for call in orch.codekb_service._update_pipeline_status.call_args_list
        if call.args[2:4] == ("commit_kb", "completed")
    ]
    statistics = completed[-1].kwargs["extra"]["statistics"]
    expected = AnalysisOrchestrator(MagicMock())._initialize_context(copy.deepcopy(prior_kb)).statistics
    assert statistics == {"directory": "/work/space", **expected}


def test_a_caller_whose_call_only_resolves_now_is_re_described(tmp_path, clone):
    """An unchanged caller of a newly defined function had no prior edge to follow."""
    prior_kb = _prior_kb(clone)
    caller_id = next(c.id for c in prior_kb.components if c.id.endswith(".caller"))
    _write(clone, {"r.py": HELPER})
    orch = _orchestrator(tmp_path, clone, prior_kb)
    orch._file_changes = [FileChange("r.py", ChangeType.ADDED)]

    orch._step_parse_changed()
    orch._step_merge()
    assert caller_id not in orch._refresh_plan.stale_caller_ids

    orch._step_rebuild_call_graph()

    assert any(caller_id == caller for caller in orch._prior_kb.call_graph)
    assert caller_id in orch._refresh_plan.stale_caller_ids


CONTEXT = {"business_terms": ["tapir"], "business_modules": ["rates"], "technical_terms": ["fmt"]}
RATES = "def base_tapir_rate():\n    return 1\n\n\ndef quote_tapir_rate():\n    return base_tapir_rate() + 1\n"
BILLING = "from rates import base_tapir_rate\n\n\ndef charge_order():\n    return base_tapir_rate()\n"


def _create(directory: Path) -> KnowledgeBase:
    kb_manager = MagicMock()
    kb_manager.import_knowledge_base.return_value = None
    ctx = AnalysisOrchestrator(kb_manager).analyze_space(
        directory, "space", business_behavior_enabled=True
    )
    return ctx.knowledge_base


def _relevance(components) -> dict:
    return {
        c.id: (c.business_behavior.business_relevance_tier, c.business_behavior.business_relevance_score)
        for c in components
        if c.business_behavior.business_relevance_tier
    }


def test_relevance_after_a_refresh_matches_creation_of_the_same_tree(tmp_path, monkeypatch):
    from src.analysis.repo_business_context_extractor import RepoBusinessContextExtractor

    monkeypatch.setattr(RepoBusinessContextExtractor, "__init__", lambda self: None)
    monkeypatch.setattr(RepoBusinessContextExtractor, "extract", lambda self, *a, **k: CONTEXT)
    monkeypatch.setattr(RepoBusinessContextExtractor, "usage_part", lambda self: {})
    analysis, clone = tmp_path / "space-analysis", tmp_path / "space-refresh"
    _write(analysis, {"rates.py": RATES})
    prior_kb = _create(analysis)
    for component in prior_kb.components:
        component.file_path = component.file_path.relative_to(analysis)
    _write(analysis, {"billing.py": BILLING})
    _write(clone, {"rates.py": RATES, "billing.py": BILLING})

    orch = _orchestrator(tmp_path, clone, prior_kb)
    orch.prior_status["omit_low_business_relevance"] = True
    orch._business_behavior_enabled = True
    orch.codekb_service.source_provider.work_path.side_effect = lambda sid, kind: tmp_path / f"{sid}-{kind}"
    orch._file_changes = [FileChange("billing.py", ChangeType.ADDED)]
    orch._step_parse_changed()
    orch._step_merge()
    orch._step_rebuild_call_graph()

    refreshed = _relevance(orch._prior_kb.components)
    assert any(cid.endswith(".charge_order") for cid in refreshed)
    assert refreshed == _relevance(_create(analysis).components)


def test_relevance_tier_changes_add_and_remove_descriptions_as_a_fresh_index_would(
    tmp_path, clone, monkeypatch
):
    from src.analysis.business_relevance_analyzer import BusinessRelevanceAnalyzer
    from src.core.models import BusinessBehavior, Component, ComponentType

    def comp(cid: str, tier: str | None) -> Component:
        return Component(
            id=cid, type=ComponentType.FUNCTION, file_path=Path("s.py"), line_number=1,
            end_line_number=2, language="python", space_id="space",
            business_behavior=BusinessBehavior(business_relevance_tier=tier),
        )

    # (prior tier, tier after re-scoring); b and e already have a vector.
    tiers = {
        "space::a": ("low", "heuristic"),  # promoted, undescribed: described
        "space::b": ("low", "heuristic"),  # promoted, already described: left alone
        "space::c": ("low", "low"),
        "space::d": (None, "medium"),
        "space::e": ("heuristic", "low"),  # demoted with a vector: deleted
        "space::f": ("medium", "low"),  # demoted, no vector: nothing to delete
    }
    with_vectors = {"space::b", "space::e"}

    def score(self, components, call_graph):
        for c in components:
            c.business_behavior.business_relevance_tier = tiers[c.id][1]

    monkeypatch.setattr(BusinessRelevanceAnalyzer, "score_components", score)
    prior_kb = KnowledgeBase(
        source_directory=clone, space_id="space", call_graph={},
        components=[comp(cid, prior) for cid, (prior, _) in tiers.items()],
    )
    orch = _orchestrator(tmp_path, clone, prior_kb)
    orch.prior_status["vector_count"] = 10
    orch._business_behavior_enabled = True
    orch._prior_tiers = {cid: prior for cid, (prior, _) in tiers.items()}
    orch._stale_candidates = frozenset(tiers)
    orch.vectors_client.get_descriptions.side_effect = lambda keys: {
        k: "described" for k in keys if k in with_vectors
    }

    orch._score_business_relevance()
    orch._refresh_plan = orch._add_undescribed_promoted(
        RefreshPlan(preserved_component_ids=frozenset(tiers))
    )
    orch._step_vector_deletes()

    assert orch._refresh_plan.undescribed_component_ids == {"space::a"}
    assert "space::a" in orch._refresh_plan.components_to_summarize()
    deleted = {c.args[0] for c in orch.vectors_client.delete_vector.call_args_list}
    assert deleted == {"space::e"}
    orch._summarized_descriptions = {"space::a": "new description"}
    assert orch._vector_count() == 10
