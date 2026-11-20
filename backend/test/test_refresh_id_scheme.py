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


def test_a_new_index_of_a_legacy_space_is_all_v2(tmp_path):
    _write(tmp_path, SEED)
    legacy = _index(tmp_path, IdScheme.V1_STEM, "")

    reindexed = CodeAnalyzer(ComponentCatalog()).analyze_directory(tmp_path, "space")

    assert DEFAULT_SCHEME is IdScheme.V2_PATH
    assert resolve_id_scheme(reindexed.id_scheme) is IdScheme.V2_PATH
    # No legacy ID carries over, so a re-indexed Space never mixes the two schemes.
    assert not {c.id for c in legacy.components} & {c.id for c in reindexed.components}
