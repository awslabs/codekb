"""A composite Dart argument must bind nothing rather than bind its receiver.

Dart's grammar is flat, so the `argument` wrapper around `repo.clone()` holds
`identifier(repo)` followed by selectors. Reading the first identifier reports
the RECEIVER's type as the type of the whole expression, which is wrong: the
argument's value is the call's result, not the object it was called on.

A wrong inferred parameter type is worse than none. It reaches the callee's
positional map and can resolve a call inside the callee to a real component that
is not the one meant, which no diagnostic would show.
"""

import tempfile
from pathlib import Path

import pytest

try:  # pragma: no cover - import guard, mirrors the other Dart modules
    import tree_sitter_dart  # noqa: F401

    DART_AVAILABLE = True
except ImportError:  # pragma: no cover
    DART_AVAILABLE = False

from src.analysis.code_analyzer import CodeAnalyzer
from src.catalog.component_catalog import ComponentCatalog, SimpleBusinessBehaviorAnalyzer
from src.core.models import IdScheme
from src.flow.flow_analyzer import FlowAnalyzer

pytestmark = pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart not installed")


def _inferred_named(source: str) -> dict[str, dict[str, str]]:
    """Named parameter types the inter-procedural pass inferred, by callee."""
    directory = Path(tempfile.mkdtemp())
    (directory / "a.dart").write_text(source)
    catalog = ComponentCatalog(SimpleBusinessBehaviorAnalyzer())
    knowledge_base = CodeAnalyzer(catalog).analyze_directory(directory, "space", id_scheme=IdScheme.V1_STEM)
    analyzer = FlowAnalyzer(catalog)
    analyzer.build_call_graph(knowledge_base.components)
    return {
        callee.split("::")[-1]: types
        for callee, types in analyzer._dart_inferred_named_param_types.items()
    }


def _inferred(source: str) -> dict[str, dict[int, str]]:
    """Positional parameter types the inter-procedural pass inferred, by callee."""
    directory = Path(tempfile.mkdtemp())
    (directory / "a.dart").write_text(source)
    catalog = ComponentCatalog(SimpleBusinessBehaviorAnalyzer())
    knowledge_base = CodeAnalyzer(catalog).analyze_directory(directory, "space", id_scheme=IdScheme.V1_STEM)
    analyzer = FlowAnalyzer(catalog)
    analyzer.build_call_graph(knowledge_base.components)
    return {
        callee.split("::")[-1]: types for callee, types in analyzer._inferred_param_types.items()
    }


PRELUDE = """class Box { void shelve() {} }
class Repo {
  Box clone() { return Box(); }
  Box field = Box();
}
void helper(x) { x.shelve(); }
"""


def _caller(expression: str) -> str:
    return (
        PRELUDE + "class Caller {\n"
        "  void go() {\n"
        "    var repo = Repo();\n"
        f"    helper({expression});\n"
        "  }\n"
        "}\n"
    )


class TestACompositeArgumentBindsNothing:
    @pytest.mark.parametrize(
        ("expression", "why"),
        [
            ("repo.clone()", "the value is the call's result, not the receiver"),
            ("repo.field", "the value is the field, not the object holding it"),
            ("repo.clone().clone()", "a chain is no more its leading identifier"),
        ],
    )
    def test_the_receivers_type_is_not_claimed_for_the_expression(self, expression, why):
        inferred = _inferred(_caller(expression))

        assert inferred.get("a.helper", {}).get(0) != "Repo", why

    def test_specifically_nothing_is_recorded_for_it(self):
        inferred = _inferred(_caller("repo.clone()"))

        assert "a.helper" not in inferred or 0 not in inferred["a.helper"]


class TestABareArgumentStillBinds:
    def test_a_bare_identifier_of_known_type_is_still_read(self):
        """The capability must survive the fix, or it bought safety by doing nothing."""
        inferred = _inferred(_caller("repo"))

        assert inferred["a.helper"][0] == "Repo"

    def test_a_second_positional_argument_keeps_its_own_index(self):
        source = (
            PRELUDE + "void pair(a, b) {}\n"
            "class Caller {\n"
            "  void go() {\n"
            "    var repo = Repo();\n"
            "    var box = Box();\n"
            "    pair(repo, box);\n"
            "  }\n"
            "}\n"
        )
        inferred = _inferred(source)

        assert inferred["a.pair"][0] == "Repo"
        assert inferred["a.pair"][1] == "Box"

    def test_a_composite_does_not_shift_the_index_of_a_later_bare_argument(self):
        """The composite still occupies its position; it simply carries no type."""
        source = (
            PRELUDE + "void pair(a, b) {}\n"
            "class Caller {\n"
            "  void go() {\n"
            "    var repo = Repo();\n"
            "    var box = Box();\n"
            "    pair(repo.clone(), box);\n"
            "  }\n"
            "}\n"
        )
        inferred = _inferred(source)

        assert inferred.get("a.pair", {}).get(0) != "Repo"
        assert inferred["a.pair"][1] == "Box"


# ── the same hazard on the named-argument path ──────────────────────────────

NAMED_PRELUDE = """class Box { void shelve() {} }
class Repo {
  Box clone() { return Box(); }
  Box field = Box();
}
void helper({handler}) { handler.shelve(); }
"""


def _named_caller(expression: str) -> str:
    return (
        NAMED_PRELUDE + "class Caller {\n"
        "  void go() {\n"
        "    var repo = Repo();\n"
        f"    helper(handler: {expression});\n"
        "  }\n"
        "}\n"
    )


class TestACompositeNamedArgumentBindsNothing:
    """A named argument's value is read the same way a positional one is.

    `named_argument` holds `label` then the value's nodes, so a composite value
    puts its leading identifier directly under the wrapper exactly as the
    positional path does.
    """

    @pytest.mark.parametrize(
        "expression",
        ["repo.clone()", "repo.field", "repo.clone().clone()"],
    )
    def test_the_receivers_type_is_not_claimed_for_the_expression(self, expression):
        inferred = _inferred_named(_named_caller(expression))

        assert inferred.get("a.helper", {}).get("handler") != "Repo"

    def test_specifically_nothing_is_recorded_for_it(self):
        inferred = _inferred_named(_named_caller("repo.clone()"))

        assert "handler" not in inferred.get("a.helper", {})


class TestABareNamedArgumentStillBinds:
    def test_a_bare_identifier_of_known_type_is_still_read(self):
        inferred = _inferred_named(_named_caller("repo"))

        assert inferred["a.helper"]["handler"] == "Repo"
