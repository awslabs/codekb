"""End-to-end: a TypeScript field receiver (``this.svc.method()``) resolves to
the field's type, so the caller gains a real call-graph edge.

The dominant NestJS/Angular idiom injects a service as a constructor parameter
property and calls it through ``this``. The resolver only types local variables,
so without typescript_flow_support the edge is dropped and the component's trace
flow comes back empty — this is the regression these tests guard.
"""

from pathlib import Path
from typing import Dict, List, Tuple

import pytest

try:  # pragma: no cover - import guard, mirrors the other TS/Dart modules
    import tree_sitter_typescript as tsts

    TS_AVAILABLE = True
except ImportError:  # pragma: no cover
    tsts = None
    TS_AVAILABLE = False

from tree_sitter import Language, Parser

from src.catalog.component_catalog import ComponentCatalog
from src.core.models import Component, IdScheme
from src.flow.flow_analyzer import FlowAnalyzer
from src.parsers import TypeScriptParser

pytestmark = pytest.mark.skipif(not TS_AVAILABLE, reason="tree_sitter_typescript not installed")


def _analyze(tmp_path: Path, files: Dict[str, str]) -> Tuple[FlowAnalyzer, Dict[str, List]]:
    """Write a tree of .ts files and build one real call graph over all of them."""
    components: List[Component] = []
    for relative, body in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

        source = path.read_bytes()
        tree = Parser(Language(tsts.language_typescript())).parse(source)
        # Assert readable, stable V1 ids; V2 id construction is covered by
        # test/test_id_scheme.py and is orthogonal to flow resolution.
        parser = TypeScriptParser()
        parser._scheme = IdScheme.V1_STEM
        components.extend(parser.extract_components(tree.root_node, source, path))

    catalog = ComponentCatalog()
    for component in components:
        catalog.add_component(component)

    analyzer = FlowAnalyzer(catalog)
    return analyzer, analyzer.build_call_graph(components)


def _targets(call_graph: Dict[str, List], caller_suffix: str) -> List[str]:
    for component_id, callees in call_graph.items():
        if component_id.split("::", 1)[-1] == caller_suffix:
            return sorted(callee.split("::", 1)[-1] for callee in callees)
    return []


_SERVICE = (
    "export class CheckoutService {\n"
    "  update(id: string, r: any) { return r; }\n"
    "  get(id: string) { return id; }\n"
    "}\n"
)

_CONTROLLER = (
    "import { CheckoutService } from './checkout.service';\n"
    "export class CheckoutController {\n"
    "  constructor(private readonly checkoutService: CheckoutService) {}\n"
    "  updateCheckout(id: string, r: any) {\n"
    "    return this.checkoutService.update(id, r);\n"
    "  }\n"
    "  getCheckout(id: string) {\n"
    "    return this.checkoutService.get(id);\n"
    "  }\n"
    "}\n"
)


def test_parameter_property_receiver_resolves_to_the_service(tmp_path):
    _, cg = _analyze(
        tmp_path,
        {"checkout.service.ts": _SERVICE, "checkout.controller.ts": _CONTROLLER},
    )
    assert "checkout.service.CheckoutService.update" in _targets(
        cg, "checkout.controller.CheckoutController.updateCheckout"
    )
    assert "checkout.service.CheckoutService.get" in _targets(
        cg, "checkout.controller.CheckoutController.getCheckout"
    )


def test_field_definition_receiver_resolves(tmp_path):
    controller = (
        "import { CheckoutService } from './checkout.service';\n"
        "export class CheckoutController {\n"
        "  private readonly checkoutService: CheckoutService;\n"
        "  constructor(svc: CheckoutService) { this.checkoutService = svc; }\n"
        "  updateCheckout(id: string, r: any) {\n"
        "    return this.checkoutService.update(id, r);\n"
        "  }\n"
        "}\n"
    )
    _, cg = _analyze(
        tmp_path,
        {"checkout.service.ts": _SERVICE, "checkout.controller.ts": controller},
    )
    assert "checkout.service.CheckoutService.update" in _targets(
        cg, "checkout.controller.CheckoutController.updateCheckout"
    )


def test_scoping_does_not_misroute_to_a_same_named_method_on_another_class(tmp_path):
    # A decoy class with its own update(); the field's declared type is
    # CheckoutService, so the edge must bind there and never to the decoy.
    decoy = "export class OtherService {\n  update(id: string) { return id; }\n}\n"
    _, cg = _analyze(
        tmp_path,
        {
            "checkout.service.ts": _SERVICE,
            "other.service.ts": decoy,
            "checkout.controller.ts": _CONTROLLER,
        },
    )
    targets = _targets(cg, "checkout.controller.CheckoutController.updateCheckout")
    assert "checkout.service.CheckoutService.update" in targets
    assert "other.service.OtherService.update" not in targets


def test_plain_untyped_parameter_yields_no_edge(tmp_path):
    # No modifier on the ctor param → not a field; this.svc stays unresolved.
    controller = (
        "import { CheckoutService } from './checkout.service';\n"
        "export class CheckoutController {\n"
        "  constructor(checkoutService: CheckoutService) {}\n"
        "  updateCheckout(id: string, r: any) {\n"
        "    return this.checkoutService.update(id, r);\n"
        "  }\n"
        "}\n"
    )
    _, cg = _analyze(
        tmp_path,
        {"checkout.service.ts": _SERVICE, "checkout.controller.ts": controller},
    )
    assert _targets(cg, "checkout.controller.CheckoutController.updateCheckout") == []
