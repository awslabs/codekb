"""Unit tests for FlowAnalyzer public API using synthetic components.

These tests sidestep tree-sitter entirely: we build Component objects by hand,
stuff them into a ComponentCatalog, and then manually populate the
FlowAnalyzer's internal call graph to exercise graph-only algorithms
(trace_execution_flow, analyze_dependencies, find_dependency_path,
detect_circular_dependencies, get_max_depth).
"""

from pathlib import Path

from src.catalog.component_catalog import ComponentCatalog
from src.core.models import (
    Component,
    ComponentAttributes,
    ComponentType,
    Visibility,
)
from src.flow.flow_analyzer import FlowAnalyzer


def _make_function(comp_id: str, *, line: int = 1, end_line: int = 10) -> Component:
    return Component(
        id=comp_id,
        type=ComponentType.FUNCTION,
        file_path=Path("synthetic.py"),
        line_number=line,
        end_line_number=end_line,
        language="python",
        attributes=ComponentAttributes(
            signature=f"def {comp_id}()",
            visibility=Visibility.PUBLIC,
            line_count=end_line - line + 1,
        ),
    )


def _make_variable(comp_id: str) -> Component:
    return Component(
        id=comp_id,
        type=ComponentType.VARIABLE,
        file_path=Path("synthetic.py"),
        line_number=1,
        end_line_number=1,
        language="python",
    )


def _build_analyzer_with_graph(edges: dict) -> FlowAnalyzer:
    """Create a FlowAnalyzer with a hand-rolled call graph.

    `edges` is a dict of caller_id -> list of callee_ids. We register every
    participating id as a function in the catalog and populate the analyzer's
    internal call-graph directly with (callee, line_number) tuples.
    """
    catalog = ComponentCatalog()
    all_ids = set(edges.keys())
    for callees in edges.values():
        all_ids.update(callees)
    for cid in all_ids:
        catalog.add_component(_make_function(cid))

    analyzer = FlowAnalyzer(catalog)
    analyzer._call_graph = {
        caller: [(callee, 0) for callee in callees] for caller, callees in edges.items()
    }
    analyzer._call_graph_built = True

    # Build the reverse graph so dependency analysis works.
    reverse = {}
    for caller, callees in edges.items():
        for callee in callees:
            reverse.setdefault(callee, []).append(caller)
    analyzer._reverse_call_graph = reverse

    return analyzer


class TestTraceExecutionFlow:
    def test_traces_linear_call_chain(self):
        analyzer = _build_analyzer_with_graph(
            {
                "a": ["b"],
                "b": ["c"],
                "c": [],
            }
        )

        flow = analyzer.trace_execution_flow("a", depth=5)
        called = flow.get_called_components()

        # a → b → c, so at minimum "b" and "c" should appear in the traced flow.
        assert "b" in called
        assert "c" in called


class TestAnalyzeDependencies:
    def test_direct_and_dependents_are_symmetric(self):
        analyzer = _build_analyzer_with_graph(
            {
                "caller1": ["target"],
                "caller2": ["target"],
                "target": ["downstream"],
            }
        )

        deps_target = analyzer.analyze_dependencies("target")
        assert "downstream" in deps_target.direct_dependencies
        assert set(deps_target.dependents) == {"caller1", "caller2"}

    def test_deduplicates_repeated_dependency_edges(self):
        analyzer = _build_analyzer_with_graph(
            {
                "caller": ["target", "target", "target"],
                "target": ["downstream", "downstream"],
                "downstream": [],
            }
        )

        deps_caller = analyzer.analyze_dependencies("caller")
        deps_target = analyzer.analyze_dependencies("target")

        assert deps_caller.direct_dependencies == ["target"]
        assert deps_target.direct_dependencies == ["downstream"]
        assert deps_target.dependents == ["caller"]


class TestFindDependencyPath:
    def test_returns_shortest_path(self):
        analyzer = _build_analyzer_with_graph(
            {
                "a": ["b", "c"],
                "b": ["d"],
                "c": ["d"],
                "d": [],
            }
        )

        path = analyzer.find_dependency_path("a", "d")
        assert path is not None
        assert path[0] == "a"
        assert path[-1] == "d"
        # Shortest path is length 3 regardless of whether it routes via b or c.
        assert len(path) == 3

    def test_returns_none_when_no_path(self):
        analyzer = _build_analyzer_with_graph(
            {
                "a": ["b"],
                "b": [],
                "c": ["d"],
                "d": [],
            }
        )

        assert analyzer.find_dependency_path("a", "d") is None


class TestDetectCircularDependencies:
    def test_finds_simple_cycle(self):
        analyzer = _build_analyzer_with_graph(
            {
                "a": ["b"],
                "b": ["c"],
                "c": ["a"],
            }
        )

        cycles = analyzer.detect_circular_dependencies()
        assert cycles, "expected at least one cycle"
        flat = {node for cycle in cycles for node in cycle}
        assert {"a", "b", "c"}.issubset(flat)

    def test_no_cycle_in_dag(self):
        analyzer = _build_analyzer_with_graph(
            {
                "a": ["b", "c"],
                "b": ["d"],
                "c": ["d"],
                "d": [],
            }
        )
        assert analyzer.detect_circular_dependencies() == []


class TestCallDepth:
    def test_max_depth_reflects_longest_chain(self):
        analyzer = _build_analyzer_with_graph(
            {
                "a": ["b"],
                "b": ["c"],
                "c": ["d"],
                "d": [],
            }
        )

        # get_max_depth walks the call graph to find the longest chain.
        # "a" is an entry point (not a callee), so the chain a→b→c→d has depth >= 3.
        assert analyzer.get_max_depth() >= 3

    def test_depths_ignore_non_callable_call_graph_keys(self):
        catalog = ComponentCatalog()
        catalog.add_component(_make_function("callable"))
        catalog.add_component(_make_variable("not_callable"))
        analyzer = FlowAnalyzer(catalog)
        analyzer._call_graph = {
            "callable": [],
            "not_callable": ["callable"],
        }
        analyzer._call_graph_built = True

        depths = analyzer.get_call_depths()

        assert set(depths) == {"callable"}


class TestBuildCallGraphWithSyntheticComponents:
    def test_empty_components_returns_empty_graph(self):
        analyzer = FlowAnalyzer(ComponentCatalog())
        result = analyzer.build_call_graph([])
        assert result == {}


class _CountingComponents(list):
    """List that records iteration work performed by lookup construction."""

    def __init__(self, values):
        super().__init__(values)
        self.items_yielded = 0

    def __iter__(self):
        for item in super().__iter__():
            self.items_yielded += 1
            yield item


def _make_method(comp_id: str, parent_id: str) -> Component:
    return Component(
        id=comp_id,
        type=ComponentType.METHOD,
        file_path=Path("synthetic.py"),
        line_number=1,
        end_line_number=2,
        language="python",
        parent_component=parent_id,
    )


class TestComponentLookupConstruction:
    def test_nested_scope_and_no_coincidental_name_alias(self):
        parent = _make_function("pkg.outer")
        nested = Component(
            id="pkg.outer.inner",
            type=ComponentType.FUNCTION,
            file_path=Path("synthetic.py"),
            line_number=2,
            end_line_number=3,
            language="python",
            parent_component=parent.id,
        )
        method = _make_method("pkg.api.OrderService.save", "pkg.api.OrderService")
        variable = _make_variable("pkg.orderservice")
        constant = Component(
            id="pkg.ORDERSERVICE",
            type=ComponentType.CONSTANT,
            file_path=Path("synthetic.py"),
            line_number=1,
            end_line_number=1,
            language="python",
        )

        analyzer = FlowAnalyzer(ComponentCatalog())
        lookup = analyzer._build_component_lookup([parent, nested, method, variable, constant])

        assert lookup[method.id] == method.id
        assert lookup["method:save"] == method.id
        assert lookup["save"] == method.id
        # Legitimate key: the exact-case, class-qualified call. A Python `self.m`
        # call resolves through the caller's class, so it has no global key.
        assert "self.save" not in lookup
        assert lookup["OrderService.save"] == method.id
        # Coincidental receiver-name → class guesses must NOT resolve. Neither a
        # variable/constant that merely matches the class name case-insensitively
        # (`ORDERSERVICE`), nor the lowercased class name itself (`orderservice`),
        # may create a `<name>.method` alias — both are global, unscoped guesses
        # that mis-bind a same-named receiver (a `CheckoutItem` lambda parameter
        # named `item` → `Item.getTotalCost`). Precision-first: a qualified call
        # with an unknown receiver type is left unresolved.
        assert "ORDERSERVICE.save" not in lookup
        assert "orderservice.save" not in lookup
        assert lookup["pkg.outer::inner"] == nested.id
        assert (
            analyzer._resolve_call_with_types(
                "service.save", {"service": "api.OrderService"}, lookup
            )
            == method.id
        )

    def test_component_iterations_scale_linearly(self):
        count = 250
        components = _CountingComponents(
            [_make_method(f"pkg.Class{i}.method{i}", f"pkg.Class{i}") for i in range(count)]
            + [_make_variable(f"pkg.class{i}") for i in range(count)]
        )

        analyzer = FlowAnalyzer(ComponentCatalog())
        analyzer._build_component_lookup(components)

        # Lookup construction should use only a small fixed number of complete
        # passes. The prior implementation yielded O(N^2) items here through
        # method-to-component and parent-to-child nested scans.
        assert components.items_yielded <= len(components) * 5


def test_call_graph_with_source_reads_relative_paths_from_the_source_root(tmp_path):
    """Spaces refreshed by older releases stored repo-relative paths."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("def a():\n    return b()\n\n\ndef b():\n    return 1\n")
    catalog = ComponentCatalog()
    for cid, line, end in (("a", 1, 2), ("b", 5, 6)):
        comp = _make_function(cid, line=line, end_line=end)
        comp.file_path = Path("pkg/m.py")
        catalog.add_component(comp)
    analyzer = FlowAnalyzer(catalog)
    analyzer._call_graph = {"a": [("b", 2)]}

    graph = analyzer.build_call_graph_with_source("a", 3, source_root=tmp_path)

    assert graph["source"] == "def a():\n    return b()\n"
    assert graph["calls"][0]["source"] == "def b():\n    return 1\n"
