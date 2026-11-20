"""Dart split-library qualification on the space-refresh path.

``DartLibraryQualifier`` prefixes a ``part`` file's component IDs with its
parent library's path. It ran only on the full-index path, so a refresh
reparsed a part file and wrote its unprefixed ID back; the catalog is
last-write-wins on identity, so one of the two same-named components
disappeared silently.

Every test here serves one criterion: a full index and a refresh must produce
the same identity for the same file, from a real parse.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

try:  # pragma: no cover - import guard, mirrors test_dart_library_grouping.py
    import tree_sitter_dart  # noqa: F401

    DART_AVAILABLE = True
except ImportError:  # pragma: no cover
    DART_AVAILABLE = False

from src.analysis.code_analyzer import (
    DART_UNRESOLVED_PARTS_KEY,
    PARSE_FAILURE_SUMMARY_KEY,
    CodeAnalyzer,
)
from src.analysis.dart_libraries import REASON_ORPHANED_PART
from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.catalog.component_catalog import ComponentCatalog
from src.core.models import (
    ChangeType,
    Component,
    ComponentType,
    FileChange,
    IdScheme,
    KnowledgeBase,
)

SPACE_ID = "dartrefresh"

# A part file declaring one class with one method, parameterised by the parent
# library it names.
STORE_PART = "part of '{parent}';\n\nclass Store {{\n  void save() {{}}\n}}\n"


# ── Harness ─────────────────────────────────────────────────────────────────


def _write_tree(root: Path, files: dict) -> None:
    """Materialise a dict of repo-relative path -> body under ``root``."""
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


def _full_index_ids(tree: Path) -> dict:
    """Identities a full index produces, keyed by repo-relative file path.

    ``analyze_directory`` is where the qualifier has always been applied, so it is
    the reference. Its component paths are absolute, not repo-relative.
    """
    kb = CodeAnalyzer(ComponentCatalog()).analyze_directory(tree, space_id=SPACE_ID, id_scheme=IdScheme.V1_STEM)
    by_file: dict = {}
    for component in kb.components:
        try:
            key = str(Path(component.file_path).resolve().relative_to(tree.resolve()))
        except ValueError:  # pragma: no cover - every analysed file is under the tree
            key = str(component.file_path)
        by_file.setdefault(key, set()).add(component.id)
    return by_file


def _component(cid: str, file: str, language: str = "dart") -> Component:
    return Component(
        id=cid,
        type=ComponentType.CLASS,
        file_path=Path(file),
        line_number=1,
        end_line_number=5,
        language=language,
        space_id=SPACE_ID,
        content_hash=f"h-{cid}",
    )


def _orchestrator(tree: Path, changes: list, prior_kb: KnowledgeBase | None = None):
    """An orchestrator positioned as if clone and diff had already run."""
    orch = RefreshOrchestrator(
        space_id=SPACE_ID,
        prior_status={"git_commit_hash": "old"},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch.kb_manager.import_knowledge_base.return_value = prior_kb
    orch._clone_dir = tree
    orch._new_commit_hash = "new"
    orch._file_changes = list(changes)
    return orch


def _refresh_ids(orch) -> dict:
    """Identities the refresh produced, keyed by repo-relative file path."""
    by_file: dict = {}
    for component in orch._parsed_components:
        by_file.setdefault(str(component.file_path), set()).add(component.id)
    return by_file


def _parse_changed_extra(orch) -> dict:
    """The ``extra`` payload of the completed ``parse_changed`` status write."""
    for call in orch.codekb_service._update_pipeline_status.call_args_list:
        if call.args[2:4] == ("parse_changed", "completed"):
            return call.kwargs.get("extra", {})
    raise AssertionError("parse_changed never reported completion")


def _injected(orch) -> list:
    return sorted(orch._injected_part_paths)


# ── The library under test, its own shape ───────────────────────────────────


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_qualified_part_paths_names_the_resolved_parts_and_nothing_else(tmp_path):
    """The accessor reports what part resolution already settled.

    The parent library is not listed, because its own components are never
    prefixed, and neither is an orphaned part, which stays unprefixed.
    """
    from src.analysis.dart_libraries import DartLibraryQualifier

    _write_tree(
        tmp_path,
        {
            "lib/models.dart": "part 'state.dart';\n\nclass Models {}\n",
            "lib/state.dart": STORE_PART.format(parent="models.dart"),
            "lib/lonely.dart": "part of 'vanished.dart';\n\nclass Lonely {}\n",
        },
    )
    analyzer = CodeAnalyzer(ComponentCatalog())
    dart_files = [
        f for f in analyzer._file_scanner.scan_directory(tmp_path) if f.suffix.lower() == ".dart"
    ]
    qualifier = DartLibraryQualifier(tmp_path, dart_files)

    assert sorted(qualifier.qualified_part_paths) == ["lib/state.dart"]
    orphans = [r for r in qualifier.unresolved_records if r.reason == REASON_ORPHANED_PART]
    assert [r.part_path for r in orphans] == ["lib/lonely.dart"]


# ── The acceptance criterion ────────────────────────────────────────────────


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_refresh_of_a_part_file_produces_the_full_index_identity(tmp_path):
    """Identical identities, not merely prefixed ones.

    Before the fix the refresh wrote ``state.Store`` where the full index wrote
    ``lib$-models.state.Store``, and the catalog dropped one of the two silently.
    """
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "lib/models.dart": "part 'state.dart';\n\nclass Models {}\n",
            "lib/state.dart": STORE_PART.format(parent="models.dart")
            + "\nclass Extra {\n  void added() {}\n}\n",
        },
    )

    reference = _full_index_ids(tree)
    assert reference["lib/state.dart"] == {
        f"{SPACE_ID}::lib$-models.state.Store",
        f"{SPACE_ID}::lib$-models.state.Store.save",
        f"{SPACE_ID}::lib$-models.state.Extra",
        f"{SPACE_ID}::lib$-models.state.Extra.added",
    }

    orch = _orchestrator(tree, [FileChange("lib/state.dart", ChangeType.MODIFIED)])
    orch._step_parse_changed()

    assert _refresh_ids(orch)["lib/state.dart"] == reference["lib/state.dart"]


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_a_refresh_of_every_file_reproduces_the_whole_full_index_identity_set(tmp_path):
    """The full index is the expectation. The tree carries every condition the
    qualifier treats differently — prefixed part, never-prefixed parent, chain,
    orphan, unresolved — and no generated file, whose divergence is pinned apart.
    """
    tree = tmp_path / "clone"
    files = {
        "lib/models/models.dart": "part 'state.dart';\n\nclass Models {\n  int n() => 1;\n}\n",
        "lib/models/state.dart": STORE_PART.format(parent="models.dart"),
        "lib/widgets/widgets.dart": "part 'state.dart';\npart 'theme.g.dart';\n",
        "lib/widgets/state.dart": STORE_PART.format(parent="widgets.dart"),
        "lib/chain/outer.dart": "part 'middle.dart';\n",
        "lib/chain/middle.dart": "part of 'outer.dart';\npart 'inner.dart';\n\nclass Middle {}\n",
        "lib/chain/inner.dart": "part of 'middle.dart';\n\nclass Inner {}\n",
        "lib/lonely.dart": "part of 'vanished.dart';\n\nclass Lonely {}\n",
        "lib/plain.dart": "class Plain {\n  int go() => 3;\n}\n",
        "src/Service.java": "class Service {\n  int f() { return 1; }\n}\n",
    }
    _write_tree(tree, files)

    reference = _full_index_ids(tree)
    expected = {cid for ids in reference.values() for cid in ids}

    orch = _orchestrator(tree, [FileChange(path, ChangeType.MODIFIED) for path in sorted(files)])
    orch._step_parse_changed()

    assert {c.id for c in orch._parsed_components} == expected
    # Not a vacuous pass: the tree really does produce prefixed identities, and
    # the generated part really was excluded.
    assert any("$-" in cid for cid in expected)
    assert not any("theme" in cid for cid in expected)


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_a_generated_file_the_commit_touched_is_excluded_on_the_refresh_path_too(tmp_path):
    """A file outside the analysed set never resolves its ``part`` directives, so
    its IDs are never prefixed and two such files sharing a name collide. The
    refresh path therefore excludes generated files exactly as a full index does.
    """
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "lib/widgets.dart": "part 'theme.g.dart';\n\nclass Widgets {}\n",
            "lib/theme.g.dart": "part of 'widgets.dart';\n\nclass Theme {}\n",
        },
    )

    reference = _full_index_ids(tree)
    assert "lib/theme.g.dart" not in reference

    orch = _orchestrator(tree, [FileChange("lib/theme.g.dart", ChangeType.MODIFIED)])
    orch._step_parse_changed()

    # Excluded, exactly as the full index excludes it: no components at all,
    # where the prior release produced an unprefixed `theme.g.Theme`.
    assert "lib/theme.g.dart" not in _refresh_ids(orch)
    assert orch._parsed_components == []

    # Counted as excluded — not as a skip, which is a capability gap, and not
    # as a parse failure, which is neither.
    extra = _parse_changed_extra(orch)
    assert extra["files_excluded_generated"] == 1
    assert extra["generated_files_excluded"] == {"dart": 1}
    assert extra["files_skipped"] == 0
    assert extra["files_parsed"] == 0


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_two_generated_files_sharing_a_name_no_longer_collide_on_refresh(tmp_path):
    """``lib/models/state.g.dart`` beside ``lib/widgets/state.g.dart`` is the
    ordinary ``build_runner`` shape: both declare ``class Store``, and unexcluded
    both yielded the same ids. Asserted as a count, because the loss is a count.
    """
    tree = tmp_path / "clone"
    files = {
        "lib/models/state.g.dart": "class Store {\n  void save() {}\n}\n",
        "lib/widgets/state.g.dart": "class Store {\n  void save() {}\n}\n",
    }
    _write_tree(tree, files)

    assert _full_index_ids(tree) == {}

    orch = _orchestrator(tree, [FileChange(p, ChangeType.MODIFIED) for p in sorted(files)])
    orch._step_parse_changed()

    ids = [c.id for c in orch._parsed_components]
    assert ids == [], f"generated files still analysed on the refresh path: {sorted(set(ids))}"
    assert len(ids) == len(set(ids))
    assert _parse_changed_extra(orch)["generated_files_excluded"] == {"dart": 2}


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_a_hand_written_file_beside_a_generated_one_is_still_parsed(tmp_path):
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "lib/models/models.dart": "part 'state.dart';\n\nclass Models {}\n",
            "lib/models/state.dart": STORE_PART.format(parent="models.dart"),
            "lib/models/models.g.dart": "class Generated {\n  void gen() {}\n}\n",
        },
    )

    reference = _full_index_ids(tree)
    orch = _orchestrator(
        tree,
        [
            FileChange("lib/models/state.dart", ChangeType.MODIFIED),
            FileChange("lib/models/models.g.dart", ChangeType.MODIFIED),
        ],
    )
    orch._step_parse_changed()

    refreshed = _refresh_ids(orch)
    assert refreshed["lib/models/state.dart"] == reference["lib/models/state.dart"]
    assert any("$-" in cid for cid in refreshed["lib/models/state.dart"])
    assert "lib/models/models.g.dart" not in refreshed
    assert _parse_changed_extra(orch)["files_parsed"] == 1


def test_a_language_with_no_generated_patterns_is_untouched_by_the_exclusion(tmp_path):
    """Dart ships the only non-empty generated-pattern list, so no other language
    changes. A Java file named the way a Dart generator names its output proves
    the list is consulted per language rather than globally.
    """
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "src/Thing.g.java": "class Thing {\n  int f() { return 1; }\n}\n",
            "src/Plain.java": "class Plain {\n  int g() { return 2; }\n}\n",
        },
    )

    orch = _orchestrator(
        tree,
        [
            FileChange("src/Thing.g.java", ChangeType.MODIFIED),
            FileChange("src/Plain.java", ChangeType.MODIFIED),
        ],
    )
    orch._step_parse_changed()

    extra = _parse_changed_extra(orch)
    assert extra["files_parsed"] == 2
    assert "files_excluded_generated" not in extra
    assert "generated_files_excluded" not in extra
    assert {str(c.file_path) for c in orch._parsed_components} == {
        "src/Thing.g.java",
        "src/Plain.java",
    }


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_an_indexed_generated_file_the_commit_touches_is_evicted_with_no_replacement(tmp_path):
    """A generated file already indexed is evicted when the diff names it and
    nothing is parsed back, so the space converges on what a full index holds. One
    the diff does not name is left until it is next touched or re-indexed.
    """
    tree = tmp_path / "clone"
    _write_tree(tree, {"lib/thing.g.dart": "class Thing {\n  void go() {}\n}\n"})

    stale = _component(f"{SPACE_ID}::thing.g.Thing", "lib/thing.g.dart")
    untouched = _component(f"{SPACE_ID}::other.g.Other", "lib/other.g.dart")
    prior = KnowledgeBase(
        source_directory=Path("/repo"),
        space_id=SPACE_ID,
        components=[stale, untouched],
        call_graph={},
        analysis_metadata={},
    )

    orch = _orchestrator(
        tree, [FileChange("lib/thing.g.dart", ChangeType.MODIFIED)], prior_kb=prior
    )
    orch._step_parse_changed()
    orch._step_merge()

    plan = orch._refresh_plan
    assert stale.id in plan.evicted_component_ids
    assert stale.id in plan.vectors_to_delete
    assert stale.id not in plan.changed_component_ids
    # Untouched by this commit, so untouched by this refresh.
    assert untouched.id in plan.preserved_component_ids


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_a_widened_file_that_never_parses_fails_the_refresh(tmp_path):
    """An injected part file is in the run only to be re-keyed, and ``_step_merge``
    evicts its prior components on that basis. If it never reaches the parser the
    eviction lands with no replacement, so the refresh fails instead of warning.
    """
    from src.analysis.errors import RefreshFailed

    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "lib/models.dart": "part 'state.dart';\n\nclass Models {}\n",
            "lib/state.dart": STORE_PART.format(parent="models.dart"),
        },
    )

    orch = _orchestrator(tree, [FileChange("lib/models.dart", ChangeType.MODIFIED)])
    real_prepare = orch._prepare_dart_library_qualification

    def _prepare_then_lose_the_part_file(clone_dir):
        report = real_prepare(clone_dir)
        (tree / "lib/state.dart").unlink()
        return report

    orch._prepare_dart_library_qualification = _prepare_then_lose_the_part_file

    with pytest.raises(RefreshFailed) as excinfo:
        orch._step_parse_changed()

    assert excinfo.value.step == "parse_changed"
    assert excinfo.value.reason == "widened_file_unparsed"
    assert "lib/state.dart" in excinfo.value.detail
    # The widening really did select it — otherwise this would pass vacuously.
    assert "lib/state.dart" in orch._injected_part_paths


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_a_part_file_with_no_declarations_does_not_fail_the_refresh(tmp_path):
    """A file holding only its ``part of`` directive is valid Dart and legitimately
    yields no components, so the check asks whether the file reached the parser,
    not whether it produced anything.
    """
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "lib/models.dart": "part 'empty.dart';\n\nclass Models {}\n",
            "lib/empty.dart": "part of 'models.dart';\n",
        },
    )

    orch = _orchestrator(tree, [FileChange("lib/models.dart", ChangeType.MODIFIED)])
    orch._step_parse_changed()

    assert "lib/empty.dart" in orch._injected_part_paths
    assert "lib/empty.dart" not in _refresh_ids(orch)
    assert _parse_changed_extra(orch)["files_parsed"] == 2


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_the_generated_exclusion_is_reported_on_the_refresh_result(tmp_path):
    """A refresh that excluded nothing serializes without the field, so no existing
    consumer sees a new key.
    """
    from src.core.models import RefreshResult

    excluded = RefreshResult(files_modified=1, files_excluded_generated=2)
    assert excluded.to_dict()["files_excluded_generated"] == 2
    assert RefreshResult.from_dict(excluded.to_dict()).files_excluded_generated == 2

    assert "files_excluded_generated" not in RefreshResult(files_modified=1).to_dict()


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_refresh_keeps_two_libraries_sharing_a_part_name_apart(tmp_path):
    """Both part files declare ``class Store``; unqualified they both yield
    ``state.Store`` and the catalog keeps one. The count is asserted as well as
    the ids, because the loss shows up as a count.
    """
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "lib/models/models.dart": "part 'state.dart';\n",
            "lib/models/state.dart": STORE_PART.format(parent="models.dart"),
            "lib/widgets/widgets.dart": "part 'state.dart';\n",
            "lib/widgets/state.dart": STORE_PART.format(parent="widgets.dart"),
        },
    )

    orch = _orchestrator(
        tree,
        [
            FileChange("lib/models/state.dart", ChangeType.MODIFIED),
            FileChange("lib/widgets/state.dart", ChangeType.MODIFIED),
        ],
    )
    orch._step_parse_changed()

    ids = {c.id for c in orch._parsed_components}
    assert f"{SPACE_ID}::lib$-models$-models.state.Store" in ids
    assert f"{SPACE_ID}::lib$-widgets$-widgets.state.Store" in ids
    assert f"{SPACE_ID}::state.Store" not in ids
    assert len([i for i in ids if i.endswith("state.Store")]) == 2


# ── The regression guard ────────────────────────────────────────────────────


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_refresh_without_part_directives_changes_no_identity(tmp_path):
    """Concrete identities, not a count: a qualifier active where no ``part``
    directive exists would re-key every id while the count still matched.
    """
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "lib/widget.dart": "class Widget {\n  int build() => 1;\n}\n",
            "lib/model.dart": "class Model {\n  int value() => 2;\n}\n",
        },
    )

    reference = _full_index_ids(tree)
    orch = _orchestrator(tree, [FileChange("lib/widget.dart", ChangeType.MODIFIED)])
    orch._step_parse_changed()

    assert _refresh_ids(orch)["lib/widget.dart"] == {
        f"{SPACE_ID}::widget.Widget",
        f"{SPACE_ID}::widget.Widget.build",
    }
    assert _refresh_ids(orch)["lib/widget.dart"] == reference["lib/widget.dart"]
    assert _injected(orch) == []
    assert orch._dart_qualifier is not None
    assert orch._dart_qualifier.active is False
    assert [fc.path for fc in orch._file_changes] == ["lib/widget.dart"]


# ── Problem B: a changed parent re-keys files that did not change ───────────


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_a_parent_gaining_a_part_directive_rekeys_the_unchanged_part_file(tmp_path):
    """The diff names only the parent, but the part file's identity changed. It is
    injected as MODIFIED because eviction is keyed on file path, so its prior
    components must be evicted for the re-keyed ones to replace them.
    """
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "lib/models.dart": "part 'state.dart';\n\nclass Models {}\n",
            "lib/state.dart": STORE_PART.format(parent="models.dart"),
        },
    )

    reference = _full_index_ids(tree)
    orch = _orchestrator(tree, [FileChange("lib/models.dart", ChangeType.MODIFIED)])
    orch._step_parse_changed()

    assert _injected(orch) == ["lib/state.dart"]
    refreshed = _refresh_ids(orch)
    assert refreshed["lib/state.dart"] == reference["lib/state.dart"]
    assert refreshed["lib/state.dart"] == {
        f"{SPACE_ID}::lib$-models.state.Store",
        f"{SPACE_ID}::lib$-models.state.Store.save",
    }
    # The parent library's own components are never prefixed.
    assert refreshed["lib/models.dart"] == {f"{SPACE_ID}::models.Models"}
    assert _parse_changed_extra(orch)["dart_libraries"]["files_reprocessed_for_part_libraries"] == 1


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_a_parent_dropping_its_part_directive_unqualifies_the_orphan(tmp_path):
    """The parent no longer claims the file, which still declares ``part of``, so it
    is an orphan and stays unprefixed: the identity has to come back down.
    """
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "lib/models.dart": "class Models {}\n",
            "lib/state.dart": STORE_PART.format(parent="models.dart"),
        },
    )

    reference = _full_index_ids(tree)
    orch = _orchestrator(tree, [FileChange("lib/models.dart", ChangeType.MODIFIED)])
    orch._step_parse_changed()

    assert _injected(orch) == ["lib/state.dart"]
    refreshed = _refresh_ids(orch)
    assert refreshed["lib/state.dart"] == {
        f"{SPACE_ID}::state.Store",
        f"{SPACE_ID}::state.Store.save",
    }
    assert refreshed["lib/state.dart"] == reference["lib/state.dart"]


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_a_removed_parent_still_widens_to_its_former_part_file(tmp_path):
    """A removal alone left ``files_to_parse`` empty and returned early. The part
    file it claimed is now an orphan that must lose its prefix, so widening sees
    removals too and the early return comes after it.
    """
    tree = tmp_path / "clone"
    _write_tree(tree, {"lib/state.dart": STORE_PART.format(parent="models.dart")})

    orch = _orchestrator(tree, [FileChange("lib/models.dart", ChangeType.REMOVED)])
    orch._step_parse_changed()

    assert _injected(orch) == ["lib/state.dart"]
    assert _refresh_ids(orch)["lib/state.dart"] == {
        f"{SPACE_ID}::state.Store",
        f"{SPACE_ID}::state.Store.save",
    }


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_widening_keeps_the_merge_file_sets_disjoint(tmp_path):
    """``RefreshPlan.__post_init__`` raises on overlapping file sets, so a file the
    diff already selected must not be injected a second time.
    """
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "lib/models.dart": "part 'state.dart';\n\nclass Models {}\n",
            "lib/state.dart": STORE_PART.format(parent="models.dart"),
        },
    )

    prior = KnowledgeBase(
        source_directory=Path("/repo"),
        space_id=SPACE_ID,
        components=[
            _component(f"{SPACE_ID}::models.Models", "lib/models.dart"),
            # What the space held before the parent declared a part: the
            # unprefixed identity this refresh has to replace.
            _component(f"{SPACE_ID}::state.Store", "lib/state.dart"),
        ],
        call_graph={},
        analysis_metadata={},
    )

    orch = _orchestrator(tree, [FileChange("lib/models.dart", ChangeType.MODIFIED)], prior_kb=prior)
    orch._step_parse_changed()
    orch._step_merge()

    plan = orch._refresh_plan
    assert plan is not None
    assert "lib/state.dart" in plan.modified_files
    assert "lib/state.dart" not in plan.unchanged_files
    assert f"{SPACE_ID}::state.Store" in plan.evicted_component_ids
    assert f"{SPACE_ID}::state.Store" in plan.vectors_to_delete
    # Only functions and methods are re-described; the re-keyed class is still stored.
    assert f"{SPACE_ID}::lib$-models.state.Store.save" in plan.changed_component_ids
    assert f"{SPACE_ID}::lib$-models.state.Store" in {c.id for c in orch._parsed_components}


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_widened_files_are_counted_apart_from_the_diffs_own_modifications(tmp_path):
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "lib/models.dart": "part 'state.dart';\n\nclass Models {}\n",
            "lib/state.dart": STORE_PART.format(parent="models.dart"),
        },
    )
    prior = KnowledgeBase(
        source_directory=Path("/repo"),
        space_id=SPACE_ID,
        components=[_component(f"{SPACE_ID}::models.Models", "lib/models.dart")],
        call_graph={},
        analysis_metadata={},
    )

    orch = _orchestrator(tree, [FileChange("lib/models.dart", ChangeType.MODIFIED)], prior_kb=prior)
    orch._step_parse_changed()
    orch._step_merge()

    merge_extra = next(
        call.kwargs["extra"]
        for call in orch.codekb_service._update_pipeline_status.call_args_list
        if call.args[2:4] == ("merge", "completed")
    )
    assert merge_extra["refresh_diff"]["modified_files"] == 1
    assert merge_extra["refresh_diff"]["modified_files_for_part_libraries"] == 1
    assert merge_extra["refresh_diff"]["total_changed_files"] == 1


# ── Not making anyone else pay ──────────────────────────────────────────────


def test_a_refresh_with_no_dart_file_in_the_diff_builds_no_qualifier(tmp_path):
    """The clone does hold Dart, so the gate is the changed set rather than the
    repository.
    """
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "src/Service.java": "class Service { int f() { return 1; } }\n",
            "lib/models.dart": "part 'state.dart';\n\nclass Models {}\n",
            "lib/state.dart": STORE_PART.format(parent="models.dart"),
        },
    )

    orch = _orchestrator(tree, [FileChange("src/Service.java", ChangeType.MODIFIED)])
    orch._step_parse_changed()

    assert orch._dart_qualifier is None
    assert _injected(orch) == []
    assert [fc.path for fc in orch._file_changes] == ["src/Service.java"]
    assert "dart_libraries" not in _parse_changed_extra(orch)


def test_a_repository_with_no_dart_at_all_is_untouched(tmp_path):
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {"src/Service.java": "class Service {\n  int compute() { return 1; }\n}\n"},
    )

    orch = _orchestrator(tree, [FileChange("src/Service.java", ChangeType.MODIFIED)])
    orch._step_parse_changed()

    assert orch._dart_qualifier is None
    assert _injected(orch) == []
    assert "dart_libraries" not in _parse_changed_extra(orch)
    ids = {c.id for c in orch._parsed_components}
    assert f"{SPACE_ID}::Service.Service.compute" in ids


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_the_widened_set_respects_the_scanners_own_exclusions(tmp_path):
    """``part 'state.g.dart'`` is the ordinary Flutter shape — a generated file the
    scanner excludes — so it must not resolve, be prefixed, or be injected.
    Vendored and build-output Dart is likewise out.
    """
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "lib/models.dart": "part 'state.g.dart';\n\nclass Models {}\n",
            "lib/state.g.dart": STORE_PART.format(parent="models.dart"),
            "build/out.dart": "part of 'models.dart';\n\nclass Built {}\n",
            "node_modules/dep.dart": "part of 'models.dart';\n\nclass Dep {}\n",
        },
    )

    orch = _orchestrator(tree, [FileChange("lib/models.dart", ChangeType.MODIFIED)])
    orch._step_parse_changed()

    assert _injected(orch) == []
    assert orch._dart_qualifier is not None
    assert orch._dart_qualifier.active is False
    # Recorded, not treated as an error.
    reasons = {r.reason for r in orch._dart_qualifier.unresolved_records}
    assert "not-in-analysed-set" in reasons
    assert _refresh_ids(orch)["lib/models.dart"] == {f"{SPACE_ID}::models.Models"}


# ── Reporting ───────────────────────────────────────────────────────────────


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_parse_failure_summary_scope_stays_changed_files_for_a_refresh(tmp_path):
    """Under ``changed-files`` the defect counts describe the whole knowledge base
    while the file tallies cover what this run parsed. Widening the changed set
    for a split library must not turn a refresh into a ``full-run``.
    """
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "lib/models.dart": "part 'state.dart';\n\nclass Models {}\n",
            "lib/state.dart": STORE_PART.format(parent="models.dart"),
        },
    )
    prior = KnowledgeBase(
        source_directory=Path("/repo"),
        space_id=SPACE_ID,
        components=[_component(f"{SPACE_ID}::models.Models", "lib/models.dart")],
        call_graph={},
        analysis_metadata={},
    )

    orch = _orchestrator(tree, [FileChange("lib/models.dart", ChangeType.MODIFIED)], prior_kb=prior)
    orch._step_parse_changed()
    orch._step_merge()
    orch._step_rebuild_call_graph()
    orch._step_stage_kb()

    staged = orch.kb_manager._write_staging_kb.call_args.args[1]
    assert staged["analysis_metadata"][PARSE_FAILURE_SUMMARY_KEY]["scope"] == "changed-files"


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_the_staged_kb_carries_this_runs_unresolved_part_records(tmp_path):
    """The qualifier was built over the whole clone, so its records describe the
    committed tree completely and replace the prior run's rather than merge.
    """
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "lib/models.dart": "part 'state.dart';\n\nclass Models {}\n",
            "lib/state.dart": STORE_PART.format(parent="models.dart"),
            "lib/lonely.dart": "part of 'vanished.dart';\n\nclass Lonely {}\n",
        },
    )
    prior = KnowledgeBase(
        source_directory=Path("/repo"),
        space_id=SPACE_ID,
        components=[_component(f"{SPACE_ID}::models.Models", "lib/models.dart")],
        call_graph={},
        analysis_metadata={DART_UNRESOLVED_PARTS_KEY: [{"part_path": "stale.dart"}]},
    )

    orch = _orchestrator(tree, [FileChange("lib/models.dart", ChangeType.MODIFIED)], prior_kb=prior)
    orch._step_parse_changed()
    orch._step_merge()
    orch._step_rebuild_call_graph()
    orch._step_stage_kb()

    staged = orch.kb_manager._write_staging_kb.call_args.args[1]
    records = staged["analysis_metadata"][DART_UNRESOLVED_PARTS_KEY]
    assert [row["part_path"] for row in records] == ["lib/lonely.dart"]
    assert records[0]["reason"] == REASON_ORPHANED_PART
    assert records[0]["declared_parent_path"] == "vanished.dart"


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_a_scan_that_cannot_run_leaves_the_refresh_an_ordinary_delta(tmp_path):
    """A scan that raises leaves the qualifier unbuilt and the changed set
    untouched, so the refresh completes as an ordinary delta. The part files it
    would have re-keyed keep their prior identities, so it is reported.
    """
    tree = tmp_path / "clone"
    _write_tree(tree, {"lib/widget.dart": "class Widget {\n  int build() => 1;\n}\n"})

    orch = _orchestrator(tree, [FileChange("lib/widget.dart", ChangeType.MODIFIED)])

    from src.core import file_utils

    def _explode(self, directory_path):
        raise OSError("scan refused")

    original = file_utils.FileScanner.scan_directory
    file_utils.FileScanner.scan_directory = _explode
    try:
        orch._step_parse_changed()
    finally:
        file_utils.FileScanner.scan_directory = original

    assert orch._dart_qualifier is None
    assert _injected(orch) == []
    assert _parse_changed_extra(orch)["dart_libraries"] == {
        "scanned": False,
        "reason": "scan_failed",
    }
    assert {c.id for c in orch._parsed_components} == {
        f"{SPACE_ID}::widget.Widget",
        f"{SPACE_ID}::widget.Widget.build",
    }


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_a_removal_only_refresh_still_reports_the_dart_pass(tmp_path):
    """The step returns early with nothing to parse, and the report has to travel
    that branch too, or a reader cannot tell "no part files" from "never ran".
    """
    tree = tmp_path / "clone"
    _write_tree(tree, {"lib/widget.dart": "class Widget {\n  int build() => 1;\n}\n"})

    orch = _orchestrator(tree, [FileChange("lib/gone.dart", ChangeType.REMOVED)])
    orch._step_parse_changed()

    assert orch._parsed_components == []
    assert _injected(orch) == []
    report = _parse_changed_extra(orch)["dart_libraries"]
    assert report["active"] is False
    assert report["files_reprocessed_for_part_libraries"] == 0


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_a_listed_file_outside_the_clone_is_left_out_of_the_widened_set(tmp_path):
    """Injected paths are repo-relative, the only form the diff, the merge's file
    classification and the prior knowledge base all agree on. A listing outside
    the clone has no such form. Driven by substitution; the real scanner cannot.
    """
    tree = tmp_path / "clone"
    outside = tmp_path / "outside"
    _write_tree(
        tree,
        {
            "lib/models.dart": "part 'state.dart';\n\nclass Models {}\n",
            "lib/state.dart": STORE_PART.format(parent="models.dart"),
        },
    )
    _write_tree(outside, {"stray.dart": "class Stray {}\n"})

    orch = _orchestrator(tree, [FileChange("lib/models.dart", ChangeType.MODIFIED)])

    from src.core import file_utils

    original = file_utils.FileScanner.scan_directory

    def _with_stray(self, directory_path):
        return original(self, directory_path) + [outside / "stray.dart"]

    file_utils.FileScanner.scan_directory = _with_stray
    try:
        orch._step_parse_changed()
    finally:
        file_utils.FileScanner.scan_directory = original

    # The part file inside the clone is still widened; the stray one is not,
    # and no absolute path leaked into the changed set.
    assert _injected(orch) == ["lib/state.dart"]
    assert all(not Path(fc.path).is_absolute() for fc in orch._file_changes)


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_a_clone_whose_dart_files_are_all_excluded_builds_no_qualifier(tmp_path):
    """The diff names a Dart file but the scan selects none, so there is nothing to
    qualify.
    """
    tree = tmp_path / "clone"
    _write_tree(tree, {"lib/state.g.dart": "class Generated {}\n"})

    orch = _orchestrator(tree, [FileChange("lib/state.g.dart", ChangeType.MODIFIED)])
    orch._step_parse_changed()

    assert orch._dart_qualifier is None
    assert _parse_changed_extra(orch)["dart_libraries"] == {"scanned": True, "dart_files": 0}


# ── The space's own exclusion patterns ──────────────────────────────────────


def _orchestrator_with_excludes(tree: Path, changes: list, excludes: list):
    """An orchestrator for a space created with its own exclusion patterns."""
    orch = RefreshOrchestrator(
        space_id=SPACE_ID,
        prior_status={"git_commit_hash": "old", "exclude_patterns": excludes},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch._clone_dir = tree
    orch._new_commit_hash = "new"
    orch._file_changes = list(changes)
    return orch


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_part_resolution_honours_the_spaces_own_exclusions(tmp_path):
    """A space keeps the patterns it was created with, so its scans must too.

    The part map decides every part file's identity. Built over a different file
    set than the committed KB, it re-keys components the space never analysed —
    the divergence the refresh-path qualifier exists to prevent.
    """
    tree = tmp_path / "clone"
    _write_tree(
        tree,
        {
            "legacy/models.dart": "part 'state.dart';\n\nclass Models {}\n",
            "legacy/state.dart": STORE_PART.format(parent="models.dart"),
        },
    )
    orch = _orchestrator_with_excludes(
        tree, [FileChange("legacy/models.dart", ChangeType.MODIFIED)], ["legacy/"]
    )

    report = orch._prepare_dart_library_qualification(tree)

    assert report == {"scanned": True, "dart_files": 0}


@pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart grammar not installed")
def test_a_newly_readable_languages_listing_honours_them_too(tmp_path):
    """The upgrade listing injects files as ADDED, so it must not reach excluded ones."""
    tree = tmp_path / "clone"
    _write_tree(tree, {"legacy/thing.dart": "class Thing {}\n"})
    orch = _orchestrator_with_excludes(tree, [], ["legacy/"])
    orch._current_language_set = ["dart"]

    selected, counts = orch._select_language_upgrade_files(tree, ["dart"])

    assert selected == []
    assert counts == {"dart": 0}
