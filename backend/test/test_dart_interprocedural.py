"""Tests for Dart's inter-procedural type inference.

A Dart ``selector`` holds ``argument_part`` with ``arguments`` one level below
it, and each positional argument is wrapped again in an ``argument`` node —
deeper than the shared argument reader looks.

``get_dart_parameter_names`` returns POSITIONAL names only. The consumer pairs
index i of it with the i-th call-site argument's type, and Dart lets a caller
write named arguments in ANY order, so a named parameter in that sequence would
hand one argument's type to another. Named ones are inferred by label instead.
"""

from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

try:  # pragma: no cover - import guard, mirrors test_dart_flow_support.py
    import tree_sitter_dart as tsdart

    DART_AVAILABLE = True
except ImportError:  # pragma: no cover
    tsdart = None
    DART_AVAILABLE = False

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from src.catalog.component_catalog import ComponentCatalog
from src.core.models import Component, IdScheme
from src.flow.flow_analyzer import FlowAnalyzer
from src.parsers import DartParser, PythonParser

pytestmark = pytest.mark.skipif(not DART_AVAILABLE, reason="tree_sitter_dart not installed")


_PARSERS = {
    ".dart": (DartParser, lambda: Language(tsdart.language())),
    ".py": (PythonParser, lambda: Language(tspython.language())),
}


def _analyze(tmp_path: Path, files: Dict[str, str]) -> Tuple[FlowAnalyzer, Dict[str, List[str]]]:
    """Write a tree of source files and build one real call graph over all of them."""
    components: List[Component] = []
    for relative, body in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

        parser_class, language_factory = _PARSERS[path.suffix]
        source = path.read_bytes()
        tree = Parser(language_factory()).parse(source)
        # Assert readable, stable V1 ids; V2 is covered by test/test_id_scheme.py.
        lang_parser = parser_class()
        lang_parser._scheme = IdScheme.V1_STEM
        components.extend(lang_parser.extract_components(tree.root_node, source, path))

    catalog = ComponentCatalog()
    for component in components:
        catalog.add_component(component)

    analyzer = FlowAnalyzer(catalog)
    return analyzer, analyzer.build_call_graph(components)


def _targets(call_graph: Dict[str, List[str]], caller: str) -> List[str]:
    for component_id, callees in call_graph.items():
        if component_id.split("::", 1)[-1] == caller:
            return sorted(callee.split("::", 1)[-1] for callee in callees)
    return []


# A callee whose parameter carries NO annotation, so only an inferred type can
# resolve the call inside it. An annotated parameter would resolve from the
# annotation alone and prove nothing about inference.
_REPO = "class Repo {\n  void find() {}\n  void save() {}\n}\n"


class TestPositionalInferenceNowFires:
    """The descent through ``argument_part`` and the ``argument`` wrapper."""

    def test_an_unannotated_parameter_takes_its_type_from_a_call_site(self, tmp_path):
        analyzer, call_graph = _analyze(
            tmp_path,
            {
                "lib.dart": _REPO,
                "app.dart": "void callee(dynamic r) {\n"
                "  r.find();\n"
                "}\n"
                "void caller() {\n"
                "  var x = Repo();\n"
                "  callee(x);\n"
                "}\n",
            },
        )
        assert analyzer._inferred_param_types["app.callee"] == {0: "Repo"}
        assert _targets(call_graph, "app.callee") == ["lib.Repo.find"]

    def test_the_second_positional_argument_reaches_the_second_parameter(self, tmp_path):
        analyzer, call_graph = _analyze(
            tmp_path,
            {
                "lib.dart": _REPO,
                "app.dart": "void callee(dynamic a, dynamic b) {\n"
                "  b.find();\n"
                "}\n"
                "void caller() {\n"
                "  var x = Repo();\n"
                "  callee(1, x);\n"
                "}\n",
            },
        )
        assert analyzer._inferred_param_types["app.callee"] == {1: "Repo"}
        assert _targets(call_graph, "app.callee") == ["lib.Repo.find"]

    def test_an_optional_positional_parameter_is_reached_too(self, tmp_path):
        _, call_graph = _analyze(
            tmp_path,
            {
                "lib.dart": _REPO,
                "app.dart": "void callee(int a, [dynamic b]) {\n"
                "  b.find();\n"
                "}\n"
                "void caller() {\n"
                "  var x = Repo();\n"
                "  callee(1, x);\n"
                "}\n",
            },
        )
        assert _targets(call_graph, "app.callee") == ["lib.Repo.find"]

    def test_an_annotation_still_wins_over_an_inferred_type(self, tmp_path):
        """Inference is applied before the local annotations, never over them."""
        _, call_graph = _analyze(
            tmp_path,
            {
                "lib.dart": _REPO,
                "other.dart": "class Other {\n  void find() {}\n}\n",
                "app.dart": "void callee(Other r) {\n"
                "  r.find();\n"
                "}\n"
                "void caller() {\n"
                "  var x = Repo();\n"
                "  callee(x);\n"
                "}\n",
            },
        )
        assert _targets(call_graph, "app.callee") == ["other.Other.find"]

    def test_a_literal_argument_infers_nothing(self, tmp_path):
        analyzer, _ = _analyze(
            tmp_path,
            {
                "lib.dart": _REPO,
                "app.dart": "void callee(dynamic r) {\n  r.find();\n}\n"
                "void caller() {\n  callee(3);\n}\n",
            },
        )
        assert analyzer._inferred_param_types.get("app.callee", {}) == {}

    def test_a_cascaded_call_feeds_the_reader_too(self, tmp_path):
        """A ``cascade_section`` buries its arguments exactly as a selector does."""
        analyzer, _ = _analyze(
            tmp_path,
            {
                "lib.dart": _REPO,
                "app.dart": "class Holder {\n"
                "  void take(dynamic r) { r.find(); }\n"
                "}\n"
                "void caller(Holder h) {\n"
                "  var x = Repo();\n"
                "  h..take(x);\n"
                "}\n",
            },
        )
        assert analyzer._inferred_param_types["app.Holder.take"] == {0: "Repo"}


class TestNamedArgumentsAreNotTypedByPosition:
    """The misalignment the positional-only sequence makes unrepresentable."""

    _FILES = {
        "lib.dart": _REPO,
        "app.dart": "void go(int a, {dynamic c, dynamic d}) {\n"
        "  c.find();\n"
        "  d.find();\n"
        "}\n"
        "void caller() {\n"
        "  var x = Repo();\n"
        "  go(1, d: x);\n"
        "}\n",
    }

    def test_the_parameter_the_call_did_not_pass_gains_no_type(self, tmp_path):
        """Index 1 of the declaration order ``[a, c, d]`` is ``c``, so typing a named
        argument by its call-site position would bind ``c`` and give ``c.find()`` an
        edge the source never supports. Two edges would mean the misalignment is live.
        """
        analyzer, call_graph = _analyze(tmp_path, dict(self._FILES))

        assert _targets(call_graph, "app.go") == ["lib.Repo.find"]
        assert len(call_graph["app.go"]) == 1

    def test_a_named_argument_leaves_the_positional_map_untouched(self, tmp_path):
        analyzer, _ = _analyze(tmp_path, dict(self._FILES))

        assert analyzer._inferred_param_types.get("app.go", {}) == {}

    def test_a_named_argument_is_recorded_against_the_parameter_it_names(self, tmp_path):
        analyzer, _ = _analyze(tmp_path, dict(self._FILES))

        assert analyzer._dart_inferred_named_param_types["app.go"] == {"d": "Repo"}

    def test_a_named_only_callee_is_still_reprocessed(self, tmp_path):
        """A callee whose sole inference is a named one has no positional entry, so
        the second pass consults the named map alongside the positional one.
        """
        _, call_graph = _analyze(
            tmp_path,
            {
                "lib.dart": _REPO,
                "app.dart": "void go({dynamic c}) {\n"
                "  c.find();\n"
                "}\n"
                "void caller() {\n"
                "  var x = Repo();\n"
                "  go(c: x);\n"
                "}\n",
            },
        )
        assert _targets(call_graph, "app.go") == ["lib.Repo.find"]

    def test_named_arguments_written_in_any_order_each_reach_their_own_parameter(self, tmp_path):
        _, call_graph = _analyze(
            tmp_path,
            {
                "lib.dart": _REPO,
                "other.dart": "class Other {\n  void open() {}\n}\n",
                "app.dart": "void go({dynamic c, dynamic d}) {\n"
                "  c.open();\n"
                "  d.find();\n"
                "}\n"
                "void caller() {\n"
                "  var x = Repo();\n"
                "  var y = Other();\n"
                "  go(d: x, c: y);\n"
                "}\n",
            },
        )
        assert _targets(call_graph, "app.go") == ["lib.Repo.find", "other.Other.open"]

    @pytest.mark.parametrize("argument", ["c: 3", "c: makeIt()", "c: untypedLocal"])
    def test_a_named_argument_with_no_knowable_type_records_nothing(self, tmp_path, argument):
        """Only a bare identifier whose type is known locally is recorded: a literal,
        a call result, or an untyped identifier leaves the parameter untyped.
        """
        analyzer, _ = _analyze(
            tmp_path,
            {
                "lib.dart": _REPO,
                "app.dart": "void go({dynamic c}) {\n"
                "  c.find();\n"
                "}\n"
                "void caller() {\n"
                "  var untypedLocal = 3;\n"
                f"  go({argument});\n"
                "}\n"
                "dynamic makeIt() => null;\n",
            },
        )
        assert analyzer._dart_inferred_named_param_types.get("app.go", {}) == {}

    def test_a_call_node_with_no_arguments_node_records_nothing(self):
        """The early-out, driven directly: a property-read selector has none."""
        source = "void f(Repo r) { var n = r.name; }"
        root = Parser(Language(tsdart.language())).parse(source.encode()).root_node
        analyzer = FlowAnalyzer(ComponentCatalog())

        selectors: List[Any] = []

        def visit(node: Any) -> None:
            if node.type == "selector":
                selectors.append(node)
            for child in node.children:
                visit(child)

        visit(root)
        assert selectors
        for selector in selectors:
            assert analyzer._extract_dart_argument_types(selector, {"r": "Repo"}) == []
            analyzer._store_dart_named_param_types(selector, "app.go", {"r": "Repo"})
        assert analyzer._dart_inferred_named_param_types == {}


class TestNoOtherLanguageMoved:
    """The gate, proved by capture rather than by assertion.

    The argument reader is shared by eight other languages. These exercise the
    same inference in Python over a tree that also holds the Dart equivalent,
    and assert the complete edge set Python produced before the Dart branch.
    """

    _PYTHON = {
        "plib.py": "class PRepo:\n    def find(self):\n        return 1\n",
        "papp.py": "from plib import PRepo\n"
        "\n"
        "def callee(r):\n"
        "    return r.find()\n"
        "\n"
        "def caller():\n"
        "    x = PRepo()\n"
        "    return callee(x)\n",
    }

    def test_pythons_inter_procedural_inference_is_unchanged(self, tmp_path):
        _, call_graph = _analyze(tmp_path, dict(self._PYTHON))

        assert _targets(call_graph, "papp.callee") == ["plib.PRepo.find"]
        assert _targets(call_graph, "papp.caller") == ["papp.callee", "plib.PRepo"]

    def test_pythons_edges_do_not_move_when_dart_is_in_the_same_space(self, tmp_path):
        """One catalog, one lookup, both languages — Python's set is identical."""
        files = dict(self._PYTHON)
        files["lib.dart"] = _REPO
        # The Dart names are deliberately distinct from the Python ones: a
        # shared bare simple name is answered by whichever component registered
        # it first, which would make this measure that collision, not the gate.
        files["app.dart"] = (
            "void dartCallee(dynamic r) {\n  r.find();\n}\n"
            "void dartCaller() {\n  var x = Repo();\n  dartCallee(x);\n}\n"
        )
        _, call_graph = _analyze(tmp_path, files)

        assert _targets(call_graph, "papp.callee") == ["plib.PRepo.find"]
        assert _targets(call_graph, "papp.caller") == ["papp.callee", "plib.PRepo"]
        # and Dart's own inference fired in the same run
        assert _targets(call_graph, "app.dartCallee") == ["lib.Repo.find"]

    def test_the_shared_reader_is_reached_for_a_non_dart_language(self, tmp_path):
        """The gate returns early for Dart only; every other language falls through."""
        analyzer, _ = _analyze(tmp_path, dict(self._PYTHON))

        assert analyzer._inferred_param_types["papp.callee"] == {0: "PRepo"}


class TestTheDartArgumentsNodeIsFoundOnEveryCallShape:
    """``selector``, ``cascade_section`` and ``new_expression`` each hold it differently."""

    @staticmethod
    def _arguments_for(source: str, call_type: str) -> Any:
        root = Parser(Language(tsdart.language())).parse(source.encode()).root_node
        found: List[Any] = []

        def visit(node: Any) -> None:
            if node.type == call_type:
                resolved = FlowAnalyzer._dart_arguments_node(node)
                if resolved is not None:
                    found.append(resolved)
            for child in node.children:
                visit(child)

        visit(root)
        return found

    def test_a_selector_resolves_through_argument_part(self):
        found = self._arguments_for("void f() { go(1); }", "selector")
        assert [node.type for node in found] == ["arguments"]

    def test_a_cascade_section_resolves_through_argument_part(self):
        found = self._arguments_for("void f(Repo r) { r..save(1); }", "cascade_section")
        assert [node.type for node in found] == ["arguments"]

    def test_a_new_expression_resolves_directly(self):
        found = self._arguments_for("void f() { var a = new Store(1); }", "new_expression")
        assert [node.type for node in found] == ["arguments"]

    def test_a_property_read_selector_resolves_nothing(self):
        assert self._arguments_for("void f(Repo r) { var n = r.name; }", "selector") == []
