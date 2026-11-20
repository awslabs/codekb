"""Refresh stores one component per ID, as creation does (M13)."""

from pathlib import Path
from unittest.mock import MagicMock

from src.analysis.refresh_orchestrator import RefreshOrchestrator
from src.core.models import ChangeType, Component, ComponentType, FileChange, KnowledgeBase

UTIL = "def helper():\n    return {n}\n"
C_PY = "class C:\n    @property\n    def v(self):\n        return 1\n\n    @v.setter\n    def v(self, x):\n        self._v = x\n"


def _comp(cid: str, file: str, content_hash: str, line: int = 1) -> Component:
    return Component(
        id=cid,
        type=ComponentType.FUNCTION,
        file_path=Path(file),
        line_number=line,
        end_line_number=line + 1,
        language="python",
        space_id="space",
        content_hash=content_hash,
    )


def _orchestrator(tmp_path: Path, prior: list[Component], changed: list[str]) -> RefreshOrchestrator:
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
    orch._file_changes = [FileChange(path, ChangeType.MODIFIED) for path in changed]
    return orch


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)


def test_an_id_parsed_again_from_another_file_is_stored_once(tmp_path):
    _write(tmp_path, {"a/util.py": UTIL.format(n=2), "b/util.py": UTIL.format(n=1), "c.py": C_PY})
    orch = _orchestrator(
        tmp_path,
        prior=[_comp("space::util.helper", "b/util.py", "hb"), _comp("space::c.C.v", "c.py", "hv")],
        changed=["a/util.py", "c.py"],
    )
    orch._parsed_components = [
        _comp("space::util.helper", "a/util.py", "ha2"),
        _comp("space::c.C.v", "c.py", "hv-get", line=3),
        _comp("space::c.C.v", "c.py", "hv-set", line=7),
    ]

    orch._step_merge()
    orch._step_rebuild_call_graph()

    stored = {c.id: c for c in orch._prior_kb.components}
    assert len(orch._prior_kb.components) == len(stored) == 2
    assert stored["space::util.helper"].file_path == Path("a/util.py")
    assert stored["space::c.C.v"].content_hash == "hv-set"
