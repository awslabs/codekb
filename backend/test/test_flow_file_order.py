"""The call graph does not depend on the order in which files or components arrive.

Commit "Make the call graph independent of file order" sorts the scanned files and
the components before the lookup tables are built. Without these tests, the suite
stays green when that sort is removed.
"""

import os
from pathlib import Path

from src.analysis.code_analyzer import CodeAnalyzer
from src.catalog.component_catalog import ComponentCatalog
from src.core.file_utils import FileScanner
from src.flow.flow_analyzer import FlowAnalyzer

# Two top-level functions with the same name. A bare call can pick only one of them.
# "First component wins" must mean first in reading order, in any arrival order.
FILES = {
    "a.js": "function helper() {\n  return 1;\n}\n",
    "b.js": "function helper() {\n  return 2;\n}\n",
    "c.js": "function run() {\n  return helper();\n}\n",
}


def _edges(root: Path, *, reverse: bool) -> dict[str, list[str]]:
    catalog = ComponentCatalog(None)
    kb = CodeAnalyzer(catalog).analyze_directory(root, "space")
    components = sorted(kb.components, key=lambda c: str(c.file_path), reverse=reverse)
    analyzer = FlowAnalyzer(catalog)
    analyzer.build_call_graph(components)
    return {
        caller.split("::", 1)[1]: sorted(callee.split("::", 1)[1] for callee, _ in callees)
        for caller, callees in analyzer._call_graph.items()
        if callees
    }


def test_component_order_does_not_change_the_call_graph(tmp_path: Path) -> None:
    for rel, body in FILES.items():
        (tmp_path / rel).write_text(body)

    forward = _edges(tmp_path, reverse=False)
    backward = _edges(tmp_path, reverse=True)

    assert forward == backward
    (target,) = next(iter(forward.values()))
    assert target.startswith("a_"), target  # reading order: a.js before b.js


def test_scan_directory_yields_a_sorted_order(tmp_path: Path, monkeypatch) -> None:
    for rel in ("z.py", "m.py", "a.py"):
        (tmp_path / rel).write_text("x = 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("y = 2\n")

    real_walk = os.walk

    def reversed_walk(*args, **kwargs):
        # The file system can list entries in any order. The scanner must not pass it on.
        for root, dirs, files in real_walk(*args, **kwargs):
            dirs.reverse()
            yield root, dirs, list(reversed(sorted(files)))

    monkeypatch.setattr(os, "walk", reversed_walk)
    scanned = [p.relative_to(tmp_path).as_posix() for p in FileScanner().scan_directory(tmp_path)]

    assert scanned == ["a.py", "m.py", "z.py", "sub/b.py"]
