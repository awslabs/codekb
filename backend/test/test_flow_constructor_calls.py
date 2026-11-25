"""A constructor is a call-graph node: its calls get edges, and flows can start from it."""

from pathlib import Path

import pytest

from src.analysis.code_analyzer import CodeAnalyzer
from src.catalog.component_catalog import ComponentCatalog
from src.core.models import ComponentType
from src.flow.flow_analyzer import FlowAnalyzer

# One file per language whose parser emits ComponentType.CONSTRUCTOR. Each file
# holds one constructor and one other callable that the constructor calls, in a
# call shape CodeKB already resolves from a method body. The callable names
# differ per file: a PHP `$this->x()` call still resolves through a global
# `self.x` key (#33), so a same-named method in another file would take the edge.
FILES = {
    "OrderService.php": (
        "<?php\nclass OrderService {\n"
        "    public function __construct() {\n        $this->resetItems();\n    }\n"
        "    public function resetItems() {\n    }\n}\n"
    ),
    "OrderService.java": (
        "public class OrderService {\n"
        "    public OrderService() {\n        resetState();\n    }\n"
        "    public void resetState() {\n    }\n}\n"
    ),
    "OrderService.cs": (
        "public class OrderService {\n"
        "    public OrderService() {\n        ResetState();\n    }\n"
        "    public void ResetState() {\n    }\n}\n"
    ),
    "order_service.dart": (
        "class OrderService {\n  OrderService() {\n    resetCounters();\n  }\n"
        "  void resetCounters() {}\n}\n"
    ),
    "order_service.js": (
        "function configure() {\n  return 1;\n}\n"
        "export class OrderService {\n  constructor() {\n    configure();\n  }\n}\n"
    ),
    "order_service.rb": (
        "class OrderService\n  def initialize\n    reset_queue()\n  end\n\n"
        "  def reset_queue\n  end\nend\n"
    ),
    "order_service.hpp": (
        "class OrderService {\npublic:\n"
        "    OrderService() {\n        resetBuffer();\n    }\n"
        "    void resetBuffer() {}\n};\n"
    ),
}

CALLEE_TYPES = (ComponentType.METHOD, ComponentType.FUNCTION)


def _analyze(root: Path, *, reverse: bool) -> FlowAnalyzer:
    for rel, body in FILES.items():
        (root / rel).write_text(body)
    catalog = ComponentCatalog(None)
    kb = CodeAnalyzer(catalog).analyze_directory(root, "space")
    components = sorted(kb.components, key=lambda c: str(c.file_path), reverse=reverse)
    analyzer = FlowAnalyzer(catalog)
    analyzer.build_call_graph(components)
    return analyzer


@pytest.mark.parametrize("reverse", [False, True])
def test_a_constructor_has_an_edge_to_the_callable_it_calls(tmp_path, reverse):
    analyzer = _analyze(tmp_path, reverse=reverse)
    components = analyzer._component_repository.get_all_components()
    by_id = {c.id: c for c in components}

    constructors = [c for c in components if c.type == ComponentType.CONSTRUCTOR]
    assert len(constructors) == len(FILES)

    for constructor in constructors:
        callees = [by_id[callee] for callee, _ in analyzer._call_graph[constructor.id]]
        # The one other callable in the constructor's file.
        expected = [
            c for c in components if c.file_path == constructor.file_path and c.type in CALLEE_TYPES
        ]
        assert [c.id for c in callees] == [c.id for c in expected], constructor.id

    # A flow can start at a constructor, and a callee lists the constructor as a caller.
    constructor = constructors[0]
    assert analyzer.trace_execution_flow(constructor.id).get_called_components()
    callee = analyzer._call_graph[constructor.id][0][0]
    assert constructor.id in analyzer.analyze_dependencies(callee).dependents
