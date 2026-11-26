"""A PHP parent:: call resolves to the member of the base class named in `extends`."""

from pathlib import Path

import pytest

from src.analysis.code_analyzer import CodeAnalyzer
from src.catalog.component_catalog import ComponentCatalog
from src.flow.flow_analyzer import FlowAnalyzer

FILES = {
    # Sorts first: the first-component-wins lookup would pick these members.
    "Audit.php": (
        "<?php\nclass Audit {\n"
        "    public function __construct() {\n    }\n"
        "    public function reset() {\n    }\n}\n"
    ),
    "Base.php": (
        "<?php\nabstract class Base {\n"
        "    public function __construct() {\n    }\n"
        "    public function reset() {\n    }\n}\n"
    ),
    "Child.php": (
        "<?php\nclass Child extends Base {\n"
        "    public function __construct() {\n        parent::__construct();\n    }\n"
        "    public function reset() {\n        parent::reset();\n    }\n}\n"
    ),
    # A trait has no base clause, so its parent:: call has no target.
    "Loose.php": "<?php\ntrait Loose {\n    public function reset() {\n        parent::reset();\n    }\n}\n",
}


def _call_graph(root: Path, *, reverse: bool) -> dict[str, list[str]]:
    for rel, body in FILES.items():
        (root / rel).write_text(body)
    catalog = ComponentCatalog(None)
    kb = CodeAnalyzer(catalog).analyze_directory(root, "space")
    components = sorted(kb.components, key=lambda c: str(c.file_path), reverse=reverse)
    analyzer = FlowAnalyzer(catalog)
    analyzer.build_call_graph(components)
    by_id = {c.id: c for c in components}

    def where(cid: str) -> str:
        comp = by_id[cid]
        return f"{comp.file_path.relative_to(root)}:{cid.split('.', 1)[1]}"

    return {
        where(caller): sorted(where(callee) for callee, _ in calls)
        for caller, calls in analyzer._call_graph.items()
        if calls
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_parent_calls_reach_the_base_class_in_any_order(tmp_path, reverse):
    graph = _call_graph(tmp_path, reverse=reverse)

    assert graph["Child.php:Child.__construct"] == ["Base.php:Base.__construct"]
    assert graph["Child.php:Child.reset"] == ["Base.php:Base.reset"]
    assert "Loose.php:Loose.reset" not in graph
    assert not any("Audit.php" in callee for callees in graph.values() for callee in callees)
