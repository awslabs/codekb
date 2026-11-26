"""A component nested in a function is visible only in that function and its nested functions."""

from pathlib import Path

import pytest

from src.analysis.code_analyzer import CodeAnalyzer
from src.catalog.component_catalog import ComponentCatalog
from src.flow.flow_analyzer import FlowAnalyzer

FILES = {
    "tools.py": "def make_mapping():\n    cls = dict\n    return cls()\n",
    "widget.py": "class Widget:\n    @classmethod\n    def create(cls):\n        return cls()\n",
    "runner.py": "def run(cb):\n    return cb()\n",
    "nested.py": (
        "def outer():\n"
        "    def helper():\n        return 1\n\n"
        "    def inner():\n"
        "        def deeper():\n            return helper()\n\n"
        "        return deeper()\n\n"
        "    return inner()\n"
    ),
    # Same-named definitions get a suffix on their id (CodeAnalyzer._disambiguate_overloads).
    "over.py": (
        "from typing import overload\n\n\n"
        "class Core:\n"
        "    @overload\n    def helper(self, x: int) -> int: ...\n"
        "    @overload\n    def helper(self, x: str) -> str: ...\n"
        "    def helper(self, x=None):\n"
        "        def inner():\n            return 1\n\n"
        "        return inner()\n"
    ),
    # Ruby: a `def` inside a method defines a method of the class.
    "service.rb": (
        "class Service\n"
        "  def run\n    def warm_up\n      1\n    end\n    warm_up\n  end\n\n"
        "  def other\n    warm_up\n  end\n"
        "end\n"
    ),
    # Kotlin: the class and the factory function share an id, and the catalog keeps
    # the function. The methods then name a parent whose lines do not hold them.
    "Thing.kt": (
        "class Thing {\n"
        "    fun parse(text: String, radix: Int): Int = text.toInt(radix)\n"
        "    fun parse(text: String): Int = parse(text, 10)\n"
        "}\n\n"
        "fun Thing(seed: Int): Thing = Thing()\n"
    ),
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
def test_locals_are_visible_in_their_own_function_only(tmp_path, reverse):
    graph = _call_graph(tmp_path, reverse=reverse)

    # The local `cls` of make_mapping stays reachable from make_mapping itself.
    assert graph["tools.py:make_mapping"] == ["tools.py:make_mapping.cls"]
    # A parameter named like that local does not link to it.
    assert "widget.py:Widget.create" not in graph
    assert "runner.py:run" not in graph
    # A nested function sees the functions that enclose it.
    assert graph["nested.py:outer.inner.deeper"] == ["nested.py:outer.helper"]
    # The implementation of an @overload group finds the function nested in it.
    implementation = next(k for k in graph if k.startswith("over.py:Core.helper_"))
    assert graph[implementation] == ["over.py:Core.helper.inner"]
    # Ruby: the nested `def` is a method of the class, so another method reaches it.
    assert graph["service.rb:Service.other"] == ["service.rb:Service.run.warm_up"]
    # Kotlin: the methods are not locals of the same-named factory function.
    caller = next(k for k in graph if k.startswith("Thing.kt:Thing.parse_"))
    assert graph[caller][0].startswith("Thing.kt:Thing.parse_")
