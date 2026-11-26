"""Python self.x(), cls.x(), super().x() and bare calls resolve by the MRO and scope rules."""

from pathlib import Path

import pytest

from src.analysis.code_analyzer import CodeAnalyzer
from src.catalog.component_catalog import ComponentCatalog
from src.flow.flow_analyzer import FlowAnalyzer

FILES = {
    "shop/__init__.py": "",
    "shop/base.py": (
        "class Base:\n"
        "    def validate(self):\n        return 'base'\n\n"
        "    def check(self):\n        return self.validate()\n"
    ),
    "shop/orders.py": (
        "from shop.base import Base\n\n\n"
        "class OrderService(Base):\n"
        "    def place(self):\n        return self.validate()\n\n"
        "    def audit(self):\n        return self.check()\n\n"
        "    @classmethod\n"
        "    def build(cls):\n        return cls.describe()\n\n"
        "    @classmethod\n"
        "    def describe(cls):\n        return 'orders'\n"
    ),
    "shop/strict.py": (
        "from shop.base import Base\n\n\n"
        "class StrictService(Base):\n"
        "    def validate(self):\n        return super().validate() + '!'\n"
    ),
    "shop/payments.py": (
        "class PaymentService:\n"
        "    def validate(self):\n        return 'payment'\n\n"
        "    def check(self):\n        return 'payment'\n\n"
        "    def describe(self):\n        return 'payment'\n"
    ),
    "shop/report.py": (
        "from shop.orders import OrderService\n\n\n"
        "def summary():\n    return OrderService().place()\n"
    ),
    "java/PaymentService.java": (
        'public class PaymentService {\n  public String validate() { return "java"; }\n}\n'
    ),
}


EDGE_CASES = {
    "pkg/__init__.py": "from .mixins import *\n",
    "pkg/mixins.py": (
        "class LogMixin:\n    def log(self):\n        return 'log'\n\n\n"
        "class AuditMixin:\n    def log(self):\n        return 'audit'\n"
    ),
    "pkg/diamond.py": (
        "from pkg.mixins import LogMixin, AuditMixin\n\n\n"
        "class Meta(type):\n    pass\n\n\n"
        "class A(LogMixin, AuditMixin):\n    pass\n\n\n"
        "class B(AuditMixin, LogMixin):\n    pass\n\n\n"
        "class C(A, B, metaclass=Meta):\n    def go(self):\n        return self.log()\n\n\n"
        "class Loop(Loop):\n    def spin(self):\n        return self.turn()\n\n    def turn(self):\n        return 0\n"
    ),
    "pkg/compat.py": (
        "from typing import TYPE_CHECKING\n"
        "try:\n    from pkg.fast import loads\nexcept ImportError:\n    from pkg.slow import loads\n"
        "if TYPE_CHECKING:\n    from pkg.mixins import LogMixin\n\n\n"
        "def parse(text):\n    return loads(text)\n\n\n"
        "def size(text):\n    return len(text)\n"
    ),
    "pkg/fast.py": "def loads(text):\n    return 1\n",
    "pkg/slow.py": "def loads(text):\n    return 2\n",
    "pkg/len.py": "def len(x):\n    return 0\n",
    "pkg/star.py": "from pkg import *\n\n\ndef audit():\n    return AuditMixin()\n",
    "pkg/escape.py": "from ...outside import thing\n\n\ndef go():\n    return thing()\n",
}


def _call_graph(root: Path, *, reverse: bool, files: dict = FILES) -> dict[str, list[str]]:
    for rel, body in files.items():
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
        where(caller): sorted(where(callee) for callee, _ in calls)
        for caller, calls in analyzer._call_graph.items()
        if calls
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_python_calls_follow_the_mro_and_the_imports(tmp_path, reverse):
    graph = _call_graph(tmp_path, reverse=reverse)

    # Inherited method: the base class, not the same-named method of PaymentService.
    assert graph["shop/orders.py:OrderService.place"] == ["shop/base.py:Base.validate"]
    assert graph["shop/orders.py:OrderService.audit"] == ["shop/base.py:Base.check"]
    # A call inside the base class resolves inside the base class.
    assert graph["shop/base.py:Base.check"] == ["shop/base.py:Base.validate"]
    # cls.x() in a classmethod uses the same rule.
    assert graph["shop/orders.py:OrderService.build"] == ["shop/orders.py:OrderService.describe"]
    # super().x() skips the caller's own override.
    assert graph["shop/strict.py:StrictService.validate"] == ["shop/base.py:Base.validate"]
    # No Python receiver call reaches the unrelated PaymentService or the Java class.
    for callees in graph.values():
        assert "shop/payments.py:PaymentService.validate" not in callees
        assert "java/PaymentService.java:PaymentService.validate" not in callees
    # A bare name resolves through the module's imports.
    assert graph["shop/report.py:summary"] == ["shop/orders.py:OrderService"]


@pytest.mark.parametrize("reverse", [False, True])
def test_inheritance_and_import_edge_cases(tmp_path, reverse):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "__init__.py").write_text("def thing():\n    return 1\n")
    root = tmp_path / "repo"
    root.mkdir()
    graph = _call_graph(root, reverse=reverse, files=EDGE_CASES)

    # C3 order: A before B, so LogMixin.log wins; a metaclass keyword is not a base.
    assert graph["pkg/diamond.py:C.go"] == ["pkg/mixins.py:LogMixin.log"]
    # A class that names itself as its base does not loop and still finds its own methods.
    assert graph["pkg/diamond.py:Loop.spin"] == ["pkg/diamond.py:Loop.turn"]
    # Of two imports of the same name, the later one is bound, as at run time.
    assert graph["pkg/compat.py:parse"] == ["pkg/slow.py:loads"]
    # A builtin is not linked to a same-named function in the repository.
    assert "pkg/compat.py:size" not in graph
    # A star import through the package __init__ resolves a class name.
    assert graph["pkg/star.py:audit"] == ["pkg/mixins.py:AuditMixin"]
    # A relative import that leaves the analyzed tree resolves to nothing.
    assert "pkg/escape.py:go" not in graph
