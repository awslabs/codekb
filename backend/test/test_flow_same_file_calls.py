"""A bare call resolves to the caller's own file before a same-named function elsewhere."""

from pathlib import Path

import pytest

from src.analysis.code_analyzer import CodeAnalyzer
from src.catalog.component_catalog import ComponentCatalog
from src.flow.flow_analyzer import FlowAnalyzer

FILES = {
    "contract/rates.py": "def base_rate(weight):\n    return 10 + weight\n",
    "rates.py": (
        "def base_rate(weight):\n    return 12 + weight\n\n\n"
        "def quote_rate(weight):\n    return base_rate(weight) + 1\n\n\n"
        "class Desk:\n    def base_rate(self):\n        return 0\n\n"
        "    def quote(self):\n        return base_rate(1)\n"
    ),
    "contract/Rates.java": (
        "public class Rates {\n  static double baseRate(double w) { return 10 + w; }\n}\n"
    ),
    "Rates.java": (
        "public class Rates {\n  static double baseRate(double w) { return 12 + w; }\n"
        "  static double quoteRate(double w) { return baseRate(w) + 1; }\n}\n"
    ),
}


def _call_graph(root: Path, *, reverse: bool) -> dict[str, list[str]]:
    for rel, body in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
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
        where(caller): [where(callee) for callee, _ in calls]
        for caller, calls in analyzer._call_graph.items()
        if calls
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_bare_calls_prefer_the_callers_own_file_in_any_order(tmp_path, reverse):
    graph = _call_graph(tmp_path, reverse=reverse)

    assert graph["rates.py:quote_rate"] == ["rates.py:base_rate"]
    # Python: a bare call inside a method reaches the module function, not the sibling method.
    assert graph["rates.py:Desk.quote"] == ["rates.py:base_rate"]
    # Java: a bare call reaches the caller's own class.
    assert graph["Rates.java:Rates.quoteRate"] == ["Rates.java:Rates.baseRate"]
