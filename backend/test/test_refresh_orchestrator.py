"""Tests for the refresh language-set upgrade.

These drive `RefreshOrchestrator._step_language_set`, `_step_merge` and the
commit step's record write directly, with a synthetic prior status and prior
knowledge base built on MagicMock collaborators. Assertions stay on the
orchestrator's own decisions: which files it selected, what it recorded.

Four refresh cases (unchanged, grown, no record, shrunken set), plus the
guardrail and failed-swap paths where nothing may be recorded as upgraded.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.analysis.errors import RefreshFailed
from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.core.models import (
    ChangeType,
    Component,
    ComponentType,
    FileChange,
    KnowledgeBase,
)

LANGUAGE_SET_FIELD = RefreshOrchestrator.LANGUAGE_SET_FIELD


def _comp(cid: str, file: str, language: str) -> Component:
    return Component(
        id=cid,
        type=ComponentType.METHOD,
        file_path=Path(file),
        line_number=1,
        end_line_number=5,
        language=language,
        space_id="space",
        content_hash=f"h-{cid}",
    )


def _prior_kb(*components: Component) -> KnowledgeBase:
    return KnowledgeBase(
        source_directory=Path("/repo"),
        space_id="space",
        components=list(components),
        call_graph={},
        analysis_metadata={},
    )


def _orchestrator(prior_status: dict, prior_kb: KnowledgeBase) -> RefreshOrchestrator:
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={"git_commit_hash": "old", **prior_status},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch.kb_manager.import_knowledge_base.return_value = prior_kb
    return orch


@pytest.fixture
def clone(tmp_path):
    """A clone directory holding one Java file, one Dart file and build output.

    The build-output file proves the listing goes through the scanner: a raw
    directory walk would pick it up, the scanner's exclusion list does not.
    """
    root = tmp_path / "clone"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Service.java").write_text("class Service { int f() { return 1; } }\n")
    (root / "src" / "widget.dart").write_text("class Widget {\n  int f() => 1;\n}\n")
    (root / "src" / "model.dart").write_text("class Model {\n  int g() => 2;\n}\n")
    (root / "build").mkdir()
    (root / "build" / "generated.dart").write_text("class Generated {}\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "vendored.dart").write_text("class Vendored {}\n")
    return root


def _with_languages(orch, languages):
    """Pin the current analysable set without touching the real grammar loader."""
    orch._compute_current_language_set = lambda: sorted(languages)


# ── Case 1: unchanged recorded set ──────────────────────────────────────────


def test_unchanged_language_set_reprocesses_exactly_the_changed_files(clone):
    orch = _orchestrator(
        {LANGUAGE_SET_FIELD: ["dart", "java"]},
        _prior_kb(_comp("space::Service.f", "src/Service.java", "java")),
    )
    _with_languages(orch, ["java", "dart"])
    orch._clone_dir = clone
    orch._file_changes = [FileChange("src/Service.java", ChangeType.MODIFIED)]

    orch._step_language_set()

    assert orch._language_set_case == "unchanged"
    assert orch._injected_added_paths == frozenset()
    assert [fc.path for fc in orch._file_changes] == ["src/Service.java"]
    # Nothing was loaded early: the unchanged case needs no prior KB.
    assert orch.kb_manager.import_knowledge_base.call_count == 0


# ── Case 2: grown set ───────────────────────────────────────────────────────


def test_grown_language_set_upgrade_injects_new_language_files_disjointly(clone):
    """The injected files join as ADDED and the merge's file sets stay disjoint."""
    orch = _orchestrator(
        {LANGUAGE_SET_FIELD: ["java"]},
        _prior_kb(_comp("space::Service.f", "src/Service.java", "java")),
    )
    _with_languages(orch, ["java", "dart"])
    orch._clone_dir = clone
    orch._file_changes = [FileChange("src/Service.java", ChangeType.MODIFIED)]

    orch._step_language_set()

    assert orch._language_set_case == "larger"
    assert orch._language_upgrade_languages == ["dart"]
    injected = sorted(orch._injected_added_paths)
    assert injected == ["src/model.dart", "src/widget.dart"]
    # The scanner's exclusion list kept build output and vendored code out.
    assert not any("build/" in p or "node_modules" in p for p in injected)

    added = [fc.path for fc in orch._file_changes if fc.change_type == ChangeType.ADDED]
    assert sorted(added) == injected

    # Drive the merge and assert the invariant an upgrade refresh would break.
    orch._parsed_components = [
        _comp("space::Widget.f", "src/widget.dart", "dart"),
        _comp("space::Model.g", "src/model.dart", "dart"),
    ]
    orch._step_merge()
    plan = orch._refresh_plan

    file_sets = {
        "added": plan.added_files,
        "modified": plan.modified_files,
        "removed": plan.removed_files,
        "unchanged": plan.unchanged_files,
    }
    for name_a, set_a in file_sets.items():
        for name_b, set_b in file_sets.items():
            if name_a < name_b:
                assert not (set_a & set_b), f"{name_a} and {name_b} overlap"
    assert plan.added_files == frozenset(injected)
    assert plan.modified_files == frozenset({"src/Service.java"})


def test_upgrade_never_double_lists_a_file_the_diff_already_selected(clone):
    orch = _orchestrator(
        {LANGUAGE_SET_FIELD: ["java"]},
        _prior_kb(_comp("space::Service.f", "src/Service.java", "java")),
    )
    _with_languages(orch, ["java", "dart"])
    orch._clone_dir = clone
    # The diff already touched one of the Dart files.
    orch._file_changes = [FileChange("src/widget.dart", ChangeType.MODIFIED)]

    orch._step_language_set()

    assert sorted(orch._injected_added_paths) == ["src/model.dart"]
    paths = [fc.path for fc in orch._file_changes]
    assert len(paths) == len(set(paths))


# ── Case 3: no record at all (the reconciling path) ─────────────────────────


def test_no_record_reconciles_only_languages_absent_from_the_prior_kb(clone):
    """A language that already has components is left alone."""
    orch = _orchestrator(
        {},  # no recorded language set
        _prior_kb(
            _comp("space::Service.f", "src/Service.java", "java"),
            _comp("space::Widget.f", "src/widget.dart", "dart"),
        ),
    )
    _with_languages(orch, ["java", "dart", "go"])
    orch._clone_dir = clone
    orch._file_changes = [FileChange("src/Service.java", ChangeType.MODIFIED)]

    orch._step_language_set()

    assert orch._language_set_case == "no_record"
    # Dart already has components, so it is not reconciled even though
    # src/model.dart was never indexed; Go is absent, and has no files here.
    assert orch._language_upgrade_languages == ["go"]
    assert orch._injected_added_paths == frozenset()
    # The prior KB is loaded at selection time, not at merge.
    assert orch.kb_manager.import_knowledge_base.call_count == 1
    assert orch._preloaded_prior_kb is not None

    # And the merge reuses that object rather than importing the same bytes twice.
    orch._parsed_components = []
    orch._step_merge()
    assert orch.kb_manager.import_knowledge_base.call_count == 1
    assert orch._prior_kb is orch._preloaded_prior_kb


def test_no_record_reconciles_a_language_entirely_absent_from_the_prior_kb(clone):
    orch = _orchestrator(
        {},
        _prior_kb(_comp("space::Service.f", "src/Service.java", "java")),
    )
    _with_languages(orch, ["java", "dart"])
    orch._clone_dir = clone
    orch._file_changes = []

    orch._step_language_set()

    assert orch._language_upgrade_languages == ["dart"]
    assert sorted(orch._injected_added_paths) == ["src/model.dart", "src/widget.dart"]


# ── Case 4: shrunken set ────────────────────────────────────────────────────


def test_shrunken_language_set_reprocesses_nothing_and_records_nothing(clone):
    """A transient grammar failure never becomes an upgrade obligation."""
    orch = _orchestrator(
        {LANGUAGE_SET_FIELD: ["dart", "java"]},
        _prior_kb(
            _comp("space::Service.f", "src/Service.java", "java"),
            _comp("space::Widget.f", "src/widget.dart", "dart"),
        ),
    )
    _with_languages(orch, ["java"])
    orch._clone_dir = clone
    orch._file_changes = [FileChange("src/Service.java", ChangeType.MODIFIED)]

    orch._step_language_set()

    assert orch._language_set_case == "smaller"
    assert orch._injected_added_paths == frozenset()
    assert [fc.path for fc in orch._file_changes] == ["src/Service.java"]
    # Nothing pruned: the prior KB is handed to the merge untouched.
    orch._parsed_components = []
    orch._step_merge()
    assert len(orch._prior_kb.components) == 2

    # And nothing is recorded even though the swap succeeded.
    assert orch._language_set_record_fields(swap_succeeded=True) == {}


# ── The two non-committing paths ────────────────────────────────────────────


def test_guardrail_path_leaves_the_language_set_record_untouched(clone):
    """A guardrail stop fails the refresh before the write phase."""
    orch = _orchestrator(
        {LANGUAGE_SET_FIELD: ["java"]},
        _prior_kb(_comp("space::Service.f", "src/Service.java", "java")),
    )
    _with_languages(orch, ["java", "dart"])
    orch._clone_dir = clone
    orch._file_changes = [FileChange("src/Service.java", ChangeType.MODIFIED)]
    orch._step_language_set()
    assert orch._language_set_case == "larger"

    # Stopping early raises before the write phase, so commit_kb never runs and
    # no status write can carry the record.
    def stopped_early():
        raise RefreshFailed(step="summarize_changed", reason="stopped_early", detail="stopped")

    orch._step_clone = lambda: None
    orch._step_remediate_depth = lambda: None
    orch._step_refresh_diff = lambda: None
    orch._step_language_set = lambda: None
    orch._step_parse_changed = lambda: None
    orch._step_merge = lambda: None
    orch._step_rebuild_call_graph = lambda: None
    orch._step_summarize_changed = stopped_early
    committed = []
    orch._step_commit_kb = lambda: committed.append(True)

    with pytest.raises(RefreshFailed):
        orch._run_steps()

    assert committed == []
    recorded_fields = [
        call.kwargs.get("extra") or {}
        for call in orch.codekb_service._update_pipeline_status.call_args_list
    ]
    assert not any(LANGUAGE_SET_FIELD in extra for extra in recorded_fields)


def test_failed_knowledge_base_swap_leaves_the_language_set_record_untouched(clone):
    """A failed swap fails the refresh and records nothing; a later good swap records it."""
    orch = _orchestrator(
        {LANGUAGE_SET_FIELD: ["java"]},
        _prior_kb(_comp("space::Service.f", "src/Service.java", "java")),
    )
    _with_languages(orch, ["java", "dart"])
    orch._clone_dir = clone
    orch._file_changes = [FileChange("src/Service.java", ChangeType.MODIFIED)]
    orch._step_language_set()
    orch._parsed_components = [_comp("space::Widget.f", "src/widget.dart", "dart")]
    orch._step_merge()

    failed = MagicMock()
    failed.success = False
    failed.refresh_errors = {"kb_swap": "CopyObject failed"}
    orch.kb_manager._commit_staging_kb.return_value = failed

    with pytest.raises(RefreshFailed, match="CopyObject failed"):
        orch._step_commit_kb()

    recorded_fields = [
        call.kwargs.get("extra") or {}
        for call in orch.codekb_service._update_pipeline_status.call_args_list
    ]
    assert not any(LANGUAGE_SET_FIELD in extra for extra in recorded_fields)

    # The same orchestrator records the set when the swap did succeed.
    succeeded = MagicMock()
    succeeded.success = True
    succeeded.refresh_errors = {}
    orch.kb_manager._commit_staging_kb.return_value = succeeded

    orch._step_commit_kb()

    completed = [
        call
        for call in orch.codekb_service._update_pipeline_status.call_args_list
        if call.args[2:4] == ("commit_kb", "completed")
    ]
    extra = completed[-1].kwargs.get("extra") or {}
    assert extra[LANGUAGE_SET_FIELD] == ["dart", "java"]
    assert extra[RefreshOrchestrator.LANGUAGE_SET_RECORDED_AT_FIELD]


# ── Reading the two sets ────────────────────────────────────────────────────


def test_current_language_set_is_the_loaded_grammars(clone):
    """Configured AND backed by a grammar, which is what the wrapper holds."""
    orch = _orchestrator({}, _prior_kb())

    current = orch._compute_current_language_set()

    assert current == sorted(current)
    assert "python" in current


def test_current_language_set_is_empty_when_no_grammar_loads(clone, monkeypatch):
    """An unusable wrapper reads as "smaller", never as an upgrade obligation."""
    import src.core.tree_sitter_wrapper as wrapper_module

    def boom():
        raise ImportError("no grammars")

    monkeypatch.setattr(wrapper_module, "TreeSitterWrapper", boom)
    orch = _orchestrator({LANGUAGE_SET_FIELD: ["java"]}, _prior_kb())
    orch._clone_dir = clone
    orch._file_changes = []

    orch._step_language_set()

    assert orch._current_language_set == []
    assert orch._language_set_case == "smaller"
    assert orch._injected_added_paths == frozenset()


@pytest.mark.parametrize("recorded", ["java", 7, ["java", 3], ["java", ""]])
def test_malformed_language_set_record_reads_as_absent(clone, recorded):
    """A value that is not a list of language keys routes to the reconciling path."""
    orch = _orchestrator(
        {LANGUAGE_SET_FIELD: recorded},
        _prior_kb(_comp("space::Service.f", "src/Service.java", "java")),
    )
    _with_languages(orch, ["java"])
    orch._clone_dir = clone
    orch._file_changes = []

    orch._step_language_set()

    assert orch._recorded_language_set is None
    assert orch._language_set_case == "no_record"


def test_unavailable_prior_knowledge_base_leaves_the_reconciling_set_empty(clone):
    """The merge owns the kb-load failure; this step must not pre-empt it."""
    orch = _orchestrator({}, _prior_kb())
    _with_languages(orch, ["java", "dart"])
    orch._clone_dir = clone
    orch._file_changes = []
    orch.kb_manager.import_knowledge_base.side_effect = RuntimeError("S3 down")

    orch._step_language_set()

    assert orch._language_set_case == "no_record"
    # No prior languages are known, so nothing is reconciled and the merge is
    # left to raise its own kb_load_failed.
    assert orch._preloaded_prior_kb is None
    with pytest.raises(Exception) as excinfo:
        orch._step_merge()
    assert "kb_load_failed" in str(excinfo.value) or "Failed to load" in str(excinfo.value)


def test_missing_prior_knowledge_base_leaves_the_reconciling_set_empty(clone):
    orch = _orchestrator({}, _prior_kb())
    _with_languages(orch, ["java", "dart"])
    orch._clone_dir = clone
    orch._file_changes = []
    orch.kb_manager.import_knowledge_base.return_value = None

    orch._step_language_set()

    assert orch._language_upgrade_languages == []
    assert orch._preloaded_prior_kb is None


def test_failed_scan_injects_no_upgrade_files(clone):
    """Nothing in the ordinary delta depends on the listing, so a failed scan is not fatal."""
    orch = _orchestrator(
        {LANGUAGE_SET_FIELD: ["java"]},
        _prior_kb(_comp("space::Service.f", "src/Service.java", "java")),
    )
    _with_languages(orch, ["java", "dart"])
    orch._clone_dir = clone / "does-not-exist"
    orch._file_changes = [FileChange("src/Service.java", ChangeType.MODIFIED)]

    orch._step_language_set()

    assert orch._language_set_case == "larger"
    assert orch._injected_added_paths == frozenset()
    assert [fc.path for fc in orch._file_changes] == ["src/Service.java"]


def test_mixed_language_set_upgrades_the_gain_and_records_nothing(clone):
    """Something gained and something lost: upgrade the gain, record neither."""
    orch = _orchestrator(
        {LANGUAGE_SET_FIELD: ["go", "java"]},
        _prior_kb(_comp("space::Service.f", "src/Service.java", "java")),
    )
    _with_languages(orch, ["java", "dart"])
    orch._clone_dir = clone
    orch._file_changes = []

    orch._step_language_set()

    assert orch._language_set_case == "mixed"
    assert orch._language_upgrade_languages == ["dart"]
    assert sorted(orch._injected_added_paths) == ["src/model.dart", "src/widget.dart"]
    assert orch._language_set_record_fields(swap_succeeded=True) == {}


# ── The reports' carriage on the refresh path ───────────────────────────────


def test_parse_changed_collects_a_diagnostic_report_per_file(clone):
    (clone / "src" / "Broken.java").write_text("class B { void f() { int x = ; } }\n")
    orch = _orchestrator({LANGUAGE_SET_FIELD: ["dart", "java"]}, _prior_kb())
    orch._clone_dir = clone
    orch._file_changes = [
        FileChange("src/Service.java", ChangeType.MODIFIED),
        FileChange("src/Broken.java", ChangeType.ADDED),
    ]

    orch._step_parse_changed()

    reports = {str(r.file_path): r for r in orch._parse_failure_reports}
    assert sorted(reports) == ["src/Broken.java", "src/Service.java"]
    assert reports["src/Service.java"].is_structurally_clean() is True
    assert reports["src/Broken.java"].error_node_count == 1
    assert reports["src/Broken.java"].language == "java"

    completed = [
        call
        for call in orch.codekb_service._update_pipeline_status.call_args_list
        if call.args[2:4] == ("parse_changed", "completed")
    ]
    summary = (completed[-1].kwargs.get("extra") or {})["parse_failure_summary"]
    assert summary["files_reported"] == 2
    assert summary["files_with_defects"] == 1
    assert summary["error_node_count"] == 1


def test_stage_kb_carries_this_runs_diagnostic_reports(clone):
    """The staged KB no longer keeps the prior run's analysis metadata."""
    prior_kb = _prior_kb(_comp("space::Service.f", "src/Service.java", "java"))
    prior_kb.analysis_metadata = {
        "parse_failure_reports": [
            {
                "file_path": "src/untouched.java",
                "language": "java",
                "parse_error_count": 0,
                "error_node_count": 5,
                "missing_node_count": 0,
            },
            {
                "file_path": "src/Service.java",
                "language": "java",
                "parse_error_count": 0,
                "error_node_count": 7,
                "missing_node_count": 0,
            },
        ],
        "parse_failure_summary": {"scope": "full-run", "error_node_count": 12},
    }
    orch = _orchestrator({LANGUAGE_SET_FIELD: ["dart", "java"]}, prior_kb)
    orch._clone_dir = clone
    orch._prior_kb = prior_kb
    orch._file_changes = [FileChange("src/Service.java", ChangeType.MODIFIED)]
    orch._step_parse_changed()

    orch._step_stage_kb()

    metadata = prior_kb.analysis_metadata
    rows = {row["file_path"]: row for row in metadata["parse_failure_reports"]}
    # The reparsed file's stale row is superseded by this run's clean outcome;
    # the untouched file's row survives.
    assert sorted(rows) == ["src/untouched.java"]
    assert rows["src/untouched.java"]["error_node_count"] == 5
    assert metadata["parse_failure_summary"]["scope"] == "changed-files"
    assert metadata["parse_failure_summary"]["error_node_count"] == 5
    assert orch.kb_manager._write_staging_kb.call_count == 1


def test_refresh_result_reports_upgrade_files_apart_from_added_files(clone):
    """The injected files do not inflate files_added."""
    orch = _orchestrator(
        {LANGUAGE_SET_FIELD: ["java"]},
        _prior_kb(_comp("space::Service.f", "src/Service.java", "java")),
    )
    _with_languages(orch, ["java", "dart"])
    orch._clone_dir = clone
    orch._file_changes = [FileChange("src/Service.java", ChangeType.MODIFIED)]
    orch._step_language_set()
    orch._parsed_components = [
        _comp("space::Widget.f", "src/widget.dart", "dart"),
        _comp("space::Model.g", "src/model.dart", "dart"),
    ]
    orch._step_merge()

    # run() only needs the steps to have produced the plan; the pipeline itself
    # is covered by its own tests, so it is stubbed out here.
    plan = orch._refresh_plan
    orch._run_steps = lambda: None
    orch._refresh_plan = plan
    orch._clone_dir = None

    result = orch.run()

    assert result.files_added == 0  # the commit range added no files
    assert result.files_added_for_new_languages == 2
    assert result.files_modified == 1


def test_stage_kb_replaces_unusable_prior_diagnostic_metadata(clone):
    """A prior knowledge base whose metadata is not a mapping still stages."""
    prior_kb = _prior_kb(_comp("space::Service.f", "src/Service.java", "java"))
    prior_kb.analysis_metadata = None
    orch = _orchestrator({LANGUAGE_SET_FIELD: ["dart", "java"]}, prior_kb)
    orch._clone_dir = clone
    orch._prior_kb = prior_kb
    orch._file_changes = [FileChange("src/Service.java", ChangeType.MODIFIED)]
    orch._step_parse_changed()

    orch._step_stage_kb()

    assert prior_kb.analysis_metadata["parse_failure_summary"]["files_reported"] == 1
    assert prior_kb.analysis_metadata["parse_failure_reports"] == []
