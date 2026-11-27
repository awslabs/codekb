"""Refresh keeps a Space on the ID scheme it was indexed with; only a new index moves it to v2."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.analysis.code_analyzer import CodeAnalyzer
from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.catalog.component_catalog import ComponentCatalog
from src.core.models import (
    DEFAULT_SCHEME,
    ChangeType,
    FileChange,
    IdScheme,
    KnowledgeBase,
    resolve_id_scheme,
)

SEED = {
    "billing/util.py": "def total():\n    return 1\n",
    "shipping/util.py": "def weigh():\n    return 2\n",
}
# A modified file and a new one: both must take the Space's scheme.
CHANGE = {
    "shipping/util.py": "def weigh():\n    return 3\n",
    "billing/tax.py": "def rate():\n    return 4\n",
}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)


def _index(root: Path, scheme: IdScheme, marker: str) -> KnowledgeBase:
    """The KB as persisted and read back. Marker "" is a Space indexed before schemes existed."""
    kb = CodeAnalyzer(ComponentCatalog()).analyze_directory(root, "space", id_scheme=scheme)
    for component in kb.components:
        component.file_path = Path(component.file_path).resolve().relative_to(root.resolve())
    kb.id_scheme = marker
    return KnowledgeBase.from_dict(kb.to_dict())


def _refresh(root: Path, prior: KnowledgeBase) -> RefreshOrchestrator:
    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={"git_commit_hash": "old"},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch.kb_manager.import_knowledge_base.return_value = prior
    orch._clone_dir = root
    orch._new_commit_hash = "new"
    orch._file_changes = [
        FileChange("shipping/util.py", ChangeType.MODIFIED),
        FileChange("billing/tax.py", ChangeType.ADDED),
    ]
    orch._step_parse_changed()
    orch._step_merge()
    orch._step_rebuild_call_graph()
    return orch


@pytest.mark.parametrize(
    "scheme, marker", [(IdScheme.V1_STEM, ""), (IdScheme.V2_PATH, "v2")], ids=["legacy", "v2"]
)
def test_refresh_keeps_the_space_on_its_scheme(tmp_path, scheme, marker):
    _write(tmp_path, SEED)
    prior = _index(tmp_path, scheme, marker)
    _write(tmp_path, CHANGE)

    orch = _refresh(tmp_path, prior)

    merged = {c.id for c in orch._prior_kb.components}
    assert merged == {c.id for c in _index(tmp_path, scheme, marker).components}
    # Existing IDs, and so their descriptions, survive.
    assert {c.id for c in prior.components} <= merged
    assert orch._refresh_plan.vectors_to_delete == frozenset()
    # The staged KB is prior_kb.to_dict(), so the next refresh resolves the same scheme.
    assert orch._prior_kb.to_dict()["id_scheme"] == marker
    assert resolve_id_scheme(orch._prior_kb.id_scheme) is scheme


def test_v2_refresh_reuses_legacy_underscore_ids_and_mints_bracket_for_new(tmp_path):
    """A V2 space indexed before the `<>`-prefix change holds legacy `stem_<hash>`
    ids. On refresh, a modified file's unchanged symbols must keep those prior
    `_` ids (and be hash-preserved, not re-summarized), while a newly-added
    symbol in the same file gets the new `<hash>` form.
    """
    import re
    from src.analysis.content_hash import backfill_component_hashes

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"
    )

    # Build the prior KB as a pre-change V2 space: index under V2 (which now
    # mints the `<>` form), give each component a content hash and a sentinel
    # description, then rewrite the prefixes back to the legacy `_` form.
    prior = _index(tmp_path, IdScheme.V2_PATH, "v2")
    backfill_component_hashes(tmp_path, prior.components)
    for c in prior.components:
        c.id = re.sub(r"<([0-9a-f]{12})>", r"_\1", c.id, count=1)
        if c.parent_component:
            c.parent_component = re.sub(r"<([0-9a-f]{12})>", r"_\1", c.parent_component, count=1)
        c.business_behavior.description = f"DESC::{c.get_simple_name()}"
    legacy_ids = {c.id for c in prior.components}
    assert legacy_ids  # sanity: the space had symbols
    assert all("<" not in cid for cid in legacy_ids)

    # Modify the file: alpha and beta are untouched; gamma is new.
    (tmp_path / "pkg" / "mod.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n\n\ndef gamma():\n    return 3\n"
    )

    orch = RefreshOrchestrator(
        space_id="space",
        prior_status={"git_commit_hash": "old"},
        kb_manager=MagicMock(),
        vectors_client=MagicMock(),
        codekb_service=MagicMock(),
        git_ops=MagicMock(),
    )
    orch.kb_manager.import_knowledge_base.return_value = prior
    orch._clone_dir = tmp_path
    orch._new_commit_hash = "new"
    orch._file_changes = [FileChange("pkg/mod.py", ChangeType.MODIFIED)]
    orch._step_parse_changed()
    orch._step_merge()

    parsed = {c.get_simple_name(): c for c in orch._parsed_components}

    # Unchanged symbols reuse their prior legacy `_` ids verbatim.
    assert parsed["alpha"].id in legacy_ids
    assert parsed["beta"].id in legacy_ids
    assert "<" not in parsed["alpha"].id and "<" not in parsed["beta"].id

    # The newly-added symbol gets the new `<hash>` form and is not a prior id.
    assert re.search(r"<[0-9a-f]{12}>", parsed["gamma"].id)
    assert parsed["gamma"].id not in legacy_ids

    plan = orch._refresh_plan
    # Hash preservation: unchanged symbols are not flagged for re-summarization
    # and carry their prior description forward; only gamma is "changed".
    assert parsed["alpha"].id not in plan.changed_component_ids
    assert parsed["beta"].id not in plan.changed_component_ids
    assert parsed["gamma"].id in plan.changed_component_ids
    assert parsed["alpha"].business_behavior.description == "DESC::alpha"
    assert parsed["beta"].business_behavior.description == "DESC::beta"
    # The reused ids are upserted in place, so nothing is deleted from the index.
    assert plan.vectors_to_delete == frozenset()


def test_a_new_index_of_a_legacy_space_is_all_v2(tmp_path):
    _write(tmp_path, SEED)
    legacy = _index(tmp_path, IdScheme.V1_STEM, "")

    reindexed = CodeAnalyzer(ComponentCatalog()).analyze_directory(tmp_path, "space")

    assert DEFAULT_SCHEME is IdScheme.V2_PATH
    assert resolve_id_scheme(reindexed.id_scheme) is IdScheme.V2_PATH
    # No legacy ID carries over, so a re-indexed Space never mixes the two schemes.
    assert not {c.id for c in legacy.components} & {c.id for c in reindexed.components}
