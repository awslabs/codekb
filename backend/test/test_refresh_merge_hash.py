"""Integration tests for method-level change detection in the refresh merge step.

These drive RefreshOrchestrator._step_merge directly with a synthetic prior KB
and freshly-parsed components, asserting which components are treated as changed
vs preserved and how the stale-caller closure is seeded — the core behavior that
makes a small edit cheap.
"""

from pathlib import Path
from unittest.mock import MagicMock

from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.core.models import (
    BusinessBehavior,
    ChangeType,
    Component,
    ComponentType,
    FileChange,
    KnowledgeBase,
)


def _comp(
    cid: str,
    file: str,
    *,
    content_hash,
    desc: str = "",
    comp_type: ComponentType = ComponentType.METHOD,
) -> Component:
    return Component(
        id=cid,
        type=comp_type,
        file_path=Path(file),
        line_number=1,
        end_line_number=5,
        language="java",
        space_id="space",
        business_behavior=BusinessBehavior(description=desc),
        content_hash=content_hash,
    )


def _make_orchestrator(prior_kb: KnowledgeBase) -> RefreshOrchestrator:
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={"git_commit_hash": "old"},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch.kb_manager.import_knowledge_base.return_value = prior_kb
    return orch


def _prior_kb() -> KnowledgeBase:
    # R.java (will be modified): save, findAll, count
    # S.java (unchanged): doStuff, which calls R.save
    return KnowledgeBase(
        source_directory=Path("/repo"),
        space_id="space",
        components=[
            _comp("space::R.save", "R.java", content_hash="hs", desc="saves a user"),
            _comp("space::R.findAll", "R.java", content_hash="hf", desc="finds all users"),
            _comp("space::R.count", "R.java", content_hash="hc", desc="counts users"),
            _comp("space::S.doStuff", "S.java", content_hash="hd", desc="calls save"),
        ],
        call_graph={"space::S.doStuff": [["space::R.save", 5]]},
    )


def test_only_changed_method_and_its_caller_are_reprocessed():
    """Editing R.save re-summarizes save + its caller; unchanged siblings are preserved."""
    orch = _make_orchestrator(_prior_kb())
    orch._file_changes = [FileChange("R.java", ChangeType.MODIFIED)]
    # Fresh parse of R.java: save changed (hs -> hs2); findAll/count identical.
    fresh_save = _comp("space::R.save", "R.java", content_hash="hs2")
    fresh_find = _comp("space::R.findAll", "R.java", content_hash="hf")
    fresh_count = _comp("space::R.count", "R.java", content_hash="hc")
    orch._parsed_components = [fresh_save, fresh_find, fresh_count]

    orch._step_merge()
    plan = orch._refresh_plan

    # Only R.save is "changed"; findAll/count are hash-preserved (not changed).
    assert plan.changed_component_ids == frozenset({"space::R.save"})
    # S.doStuff calls R.save -> flagged stale so its description is refreshed.
    assert plan.stale_caller_ids == frozenset({"space::S.doStuff"})
    # The work set is just the changed method + its caller.
    assert plan.components_to_summarize() == frozenset({"space::R.save", "space::S.doStuff"})
    # Crucially, the untouched siblings are NOT re-summarized.
    assert "space::R.findAll" not in plan.components_to_summarize()
    assert "space::R.count" not in plan.components_to_summarize()
    # No vectors deleted (all component ids reappear in the fresh parse).
    assert plan.vectors_to_delete == frozenset()


def test_preserved_siblings_keep_prior_descriptions():
    """Hash-matched components carry their prior AI description forward (not blanked)."""
    orch = _make_orchestrator(_prior_kb())
    orch._file_changes = [FileChange("R.java", ChangeType.MODIFIED)]
    fresh_find = _comp("space::R.findAll", "R.java", content_hash="hf")  # empty desc
    fresh_count = _comp("space::R.count", "R.java", content_hash="hc")  # empty desc
    fresh_save = _comp("space::R.save", "R.java", content_hash="hs2")
    orch._parsed_components = [fresh_save, fresh_find, fresh_count]

    orch._step_merge()

    # The freshly-parsed (empty) siblings now hold the prior descriptions.
    assert fresh_find.business_behavior.description == "finds all users"
    assert fresh_count.business_behavior.description == "counts users"


def test_new_component_is_treated_as_changed():
    orch = _make_orchestrator(_prior_kb())
    orch._file_changes = [FileChange("R.java", ChangeType.MODIFIED)]
    fresh_save = _comp("space::R.save", "R.java", content_hash="hs")  # unchanged
    fresh_find = _comp("space::R.findAll", "R.java", content_hash="hf")  # unchanged
    fresh_count = _comp("space::R.count", "R.java", content_hash="hc")  # unchanged
    fresh_new = _comp("space::R.newMethod", "R.java", content_hash="hn")  # brand new
    orch._parsed_components = [fresh_save, fresh_find, fresh_count, fresh_new]

    orch._step_merge()
    plan = orch._refresh_plan

    assert plan.changed_component_ids == frozenset({"space::R.newMethod"})


def test_legacy_component_without_prior_hash_is_conservatively_changed():
    """A prior component with no content_hash (legacy) must be re-summarized."""
    kb = _prior_kb()
    # Wipe the prior hash on findAll to simulate a legacy (pre-feature) KB entry.
    for c in kb.components:
        if c.id == "space::R.findAll":
            c.content_hash = None
    orch = _make_orchestrator(kb)
    orch._file_changes = [FileChange("R.java", ChangeType.MODIFIED)]
    fresh_save = _comp("space::R.save", "R.java", content_hash="hs")  # unchanged hash
    fresh_find = _comp("space::R.findAll", "R.java", content_hash="hf")  # has hash now
    fresh_count = _comp("space::R.count", "R.java", content_hash="hc")  # unchanged
    orch._parsed_components = [fresh_save, fresh_find, fresh_count]

    orch._step_merge()
    plan = orch._refresh_plan

    # findAll has no prior hash to compare against -> treated as changed.
    assert "space::R.findAll" in plan.changed_component_ids
    # save/count still preserved.
    assert "space::R.save" not in plan.changed_component_ids
    assert "space::R.count" not in plan.changed_component_ids


def test_removed_component_deletes_vector_and_seeds_closure():
    """A method deleted from a modified file is removed (vector deleted) and seeds staleness."""
    orch = _make_orchestrator(_prior_kb())
    orch._file_changes = [FileChange("R.java", ChangeType.MODIFIED)]
    # Fresh parse drops R.save entirely (deleted), keeps findAll/count unchanged.
    fresh_find = _comp("space::R.findAll", "R.java", content_hash="hf")
    fresh_count = _comp("space::R.count", "R.java", content_hash="hc")
    orch._parsed_components = [fresh_find, fresh_count]

    orch._step_merge()
    plan = orch._refresh_plan

    # R.save vanished -> its vector must be deleted.
    assert plan.vectors_to_delete == frozenset({"space::R.save"})
    # Its caller S.doStuff becomes stale (the callee it referenced is gone).
    assert "space::S.doStuff" in plan.stale_caller_ids
    # Nothing freshly changed content-wise.
    assert plan.changed_component_ids == frozenset()


# ── Seed restricted to summarized (callable) components ──────────────────────
# Classes/interfaces/enums/fields are parsed but never summarized, so a change
# to one must not cascade into re-summarizing everything that uses it.


def _kb_with_class_caller() -> KnowledgeBase:
    """S.doStuff (in S.java) instantiates class R (in R.java); R has a method R.save."""
    return KnowledgeBase(
        source_directory=Path("/repo"),
        space_id="space",
        components=[
            _comp("space::R", "R.java", content_hash="hR", comp_type=ComponentType.CLASS),
            _comp("space::R.save", "R.java", content_hash="hs", desc="saves a user"),
            _comp("space::S.doStuff", "S.java", content_hash="hd", desc="uses R"),
        ],
        # S.doStuff "calls" the class R (e.g. instantiation edge).
        call_graph={"space::S.doStuff": [["space::R", 5]]},
    )


def test_non_callable_class_change_does_not_seed_closure():
    """Editing the class (seed data / annotation flips its hash) must NOT re-summarize its users."""
    orch = _make_orchestrator(_kb_with_class_caller())
    orch._file_changes = [FileChange("R.java", ChangeType.MODIFIED)]
    # Class hash changed; the method inside is unchanged.
    fresh_class = _comp("space::R", "R.java", content_hash="hR2", comp_type=ComponentType.CLASS)
    fresh_save = _comp("space::R.save", "R.java", content_hash="hs")
    orch._parsed_components = [fresh_class, fresh_save]

    orch._step_merge()
    plan = orch._refresh_plan

    # A class is never summarized, as at creation, so it is not a target...
    assert plan.components_to_summarize() == frozenset()
    # ...and, being non-callable, it does NOT seed the closure:
    assert plan.stale_caller_ids == frozenset()


def test_callable_change_still_seeds_closure():
    """Contrast: when a METHOD changes, its caller IS re-summarized."""
    kb = _kb_with_class_caller()
    # Point the call edge at the method instead of the class.
    kb.call_graph = {"space::S.doStuff": [["space::R.save", 5]]}
    orch = _make_orchestrator(kb)
    orch._file_changes = [FileChange("R.java", ChangeType.MODIFIED)]
    fresh_class = _comp("space::R", "R.java", content_hash="hR", comp_type=ComponentType.CLASS)
    fresh_save = _comp("space::R.save", "R.java", content_hash="hs2")  # method changed
    orch._parsed_components = [fresh_class, fresh_save]

    orch._step_merge()
    plan = orch._refresh_plan

    assert "space::R.save" in plan.changed_component_ids
    assert plan.stale_caller_ids == frozenset({"space::S.doStuff"})


def test_removed_non_callable_does_not_seed_closure():
    """Deleting a field (non-callable) must not cascade to its referencers."""
    kb = KnowledgeBase(
        source_directory=Path("/repo"),
        space_id="space",
        components=[
            _comp("space::R.cache", "R.java", content_hash="hc", comp_type=ComponentType.VARIABLE),
            _comp("space::R.save", "R.java", content_hash="hs"),
            _comp("space::S.doStuff", "S.java", content_hash="hd", desc="reads cache"),
        ],
        call_graph={"space::S.doStuff": [["space::R.cache", 5]]},
    )
    orch = _make_orchestrator(kb)
    orch._file_changes = [FileChange("R.java", ChangeType.MODIFIED)]
    # Fresh parse of R.java drops the field, keeps the method unchanged.
    fresh_save = _comp("space::R.save", "R.java", content_hash="hs")
    orch._parsed_components = [fresh_save]

    orch._step_merge()
    plan = orch._refresh_plan

    # The field's vector is scheduled for deletion...
    assert "space::R.cache" in plan.vectors_to_delete
    # ...but being non-callable, it does not seed the closure.
    assert plan.stale_caller_ids == frozenset()


# ── Deleted files must actually leave the knowledge base ─────────────────────
# Classification compares the prior KB's file paths against the repo-relative
# ones git reports, so a deletion is only recognised when the stored paths are
# relative too. Nothing pinned that before, which is how a whole class of
# eviction failures went unnoticed.
#
# Known gap, deliberately not worked around here: a knowledge base analysed
# before paths were persisted relative holds absolute paths, and this
# classification does not recognise a deletion in one at all. Carried into the
# space-refresh redesign (see P511230924).


def test_removed_file_evicts_its_components():
    """A file git reports as deleted has its components evicted, not preserved."""
    orch = _make_orchestrator(_prior_kb())
    orch._file_changes = [FileChange("S.java", ChangeType.REMOVED)]
    # S.java is gone, and R.java was untouched, so there is nothing to re-parse.
    orch._parsed_components = []

    orch._step_merge()
    plan = orch._refresh_plan

    assert plan.evicted_component_ids == frozenset({"space::S.doStuff"})
    # Nothing will replace it, so its vector has to go too.
    assert plan.vectors_to_delete == frozenset({"space::S.doStuff"})
    assert "space::S.doStuff" not in plan.preserved_component_ids
    # R.java's components are untouched and carried forward.
    assert plan.preserved_component_ids == frozenset(
        {"space::R.save", "space::R.findAll", "space::R.count"}
    )


# ── Stale callers stop at the prompt's call depth ────────────────────────────
# A caller's description was written from a prompt showing its callees only a
# few levels down; callers further up never saw the changed code.


def test_stale_callers_stop_at_the_prompt_hop_limit():
    """S.a -> S.b -> S.c -> R.save; with 2 prompt hops, only S.c and S.b go stale."""
    kb = KnowledgeBase(
        source_directory=Path("/repo"),
        space_id="space",
        components=[
            _comp("space::R.save", "R.java", content_hash="hs"),
            _comp("space::S.c", "S.java", content_hash="hc"),
            _comp("space::S.b", "S.java", content_hash="hb"),
            _comp("space::S.a", "S.java", content_hash="ha"),
        ],
        call_graph={
            "space::S.c": [["space::R.save", 1]],
            "space::S.b": [["space::S.c", 1]],
            "space::S.a": [["space::S.b", 1]],
        },
    )
    orch = _make_orchestrator(kb)
    orch._prompt_hops = lambda: 2
    orch._file_changes = [FileChange("R.java", ChangeType.MODIFIED)]
    orch._parsed_components = [_comp("space::R.save", "R.java", content_hash="hs2")]

    orch._step_merge()

    assert orch._refresh_plan.stale_caller_ids == frozenset({"space::S.c", "space::S.b"})
