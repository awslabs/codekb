"""Tests for two Dart bindings that wrote a type nothing read.

Dart gives an extension no instance fields but does allow static ones, and the
field walk's owner set excluded every extension declaration — so a static field
in ``extension E on Repo`` was reported by nothing while the same field in a
class was. For Dart 3 patterns, ``pattern_variable_declaration`` carries no
``initialized_variable_definition`` and ``variable_pattern`` was not a node type
the Dart branch dispatched on. Both were missing bindings, closed on the same
terms: bind where the source wrote a type, bind nothing where it did not.
"""

from pathlib import Path
from typing import Any, Dict, List

import pytest

try:  # pragma: no cover - import guard, mirrors test_dart_flow_support.py
    import tree_sitter_dart as tsdart

    DART_AVAILABLE = True
except ImportError:  # pragma: no cover
    tsdart = None
    DART_AVAILABLE = False

from tree_sitter import Language, Parser

from src.catalog.component_catalog import ComponentCatalog
from src.core.models import Component, IdScheme
from src.flow.dart_flow_support import (
    _BODY_NODE_TYPES,
    _FIELD_BODY_NODE_TYPES,
    _FIELD_OWNER_NODE_TYPES,
    _TYPE_DECLARATION_NODE_TYPES,
    _TYPE_PARAMETER_OWNER_NODE_TYPES,
    extract_dart_assignment_type,
    extract_dart_field_types,
    extract_dart_parameter_types,
)
from src.analysis.code_analyzer import CodeAnalyzer
from src.catalog.component_catalog import SimpleBusinessBehaviorAnalyzer
from src.flow.flow_analyzer import FlowAnalyzer
from src.parsers import DartParser

pytestmark = pytest.mark.skipif(not DART_AVAILABLE, reason="tree_sitter_dart not installed")


# ── harness ───────────────────────────────────────────────────────────────────


def _root(source: str) -> Any:
    return Parser(Language(tsdart.language())).parse(source.encode()).root_node


def _node_text(node: Any) -> str:
    try:
        text: str = node.text.decode("utf-8")
        return text
    except Exception:  # pragma: no cover - defensive, matches the analyzer
        return "unknown"


def _analyze(tmp_path: Path, files: Dict[str, str]) -> Dict[str, List[str]]:
    components: List[Component] = []
    for relative, body in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        source = path.read_bytes()
        tree = Parser(Language(tsdart.language())).parse(source)
        lang_parser = DartParser()
        lang_parser._scheme = IdScheme.V1_STEM  # readable V1 ids; V2 covered by test_id_scheme.py
        components.extend(lang_parser.extract_components(tree.root_node, source, path))

    catalog = ComponentCatalog()
    for component in components:
        catalog.add_component(component)
    return FlowAnalyzer(catalog).build_call_graph(components)


def _targets(call_graph: Dict[str, List[str]], caller: str) -> List[str]:
    for component_id, callees in call_graph.items():
        if component_id.split("::", 1)[-1] == caller:
            return sorted(callee.split("::", 1)[-1] for callee in callees)
    return []


def _patterns(source: str) -> Dict[str, Any]:
    """Bind every ``variable_pattern`` in a snippet, as the analyzer's walk does."""
    bindings: Dict[str, Any] = {}

    def visit(node: Any) -> None:
        if node.type == "variable_pattern":
            name, type_name = extract_dart_assignment_type(node, _node_text)
            if name:
                bindings[name] = type_name
        for child in node.children:
            visit(child)

    visit(_root(source))
    return bindings


_DECOY = """
class Decoy {
  void paFind() {}
  void pbOpen() {}
  void ebFind() {}
}
"""


class TestPatternVariables:
    """A Dart 3 pattern variable binds the type the pattern wrote."""

    def test_a_record_pattern_binds_each_typed_variable_independently(self):
        assert _patterns("void f() { var (Repo a, Box b) = pair(); }") == {
            "a": "Repo",
            "b": "Box",
        }

    def test_a_record_pattern_binds_only_the_variables_that_wrote_a_type(self):
        """``var (Repo a, b) = pair();`` mixes two shapes: ``Repo a`` parses as a
        ``variable_pattern`` and the bare ``b`` as a ``constant_pattern``, which this
        handler is never handed. So ``b`` is absent, not present-and-untyped.
        """
        assert _patterns("void f() { var (Repo a, b) = pair(); }") == {"a": "Repo"}

    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("Repo r", "Repo"),
            ("Repo? r", "Repo"),
            ("List<Repo> r", "List"),
            ("prefix.Repo r", "Repo"),
            ("final Repo r", "Repo"),
            ("dynamic r", None),
            ("Object r", None),
            ("T r", None),
        ],
    )
    def test_the_pattern_type_is_reduced_the_way_an_annotation_is(self, written, expected):
        """The same reductions, and the same refusals, a written annotation gets."""
        assert _patterns(f"void f(Object o) {{ if (o case {written}) {{}} }}") == {"r": expected}

    @pytest.mark.parametrize(
        "source",
        [
            "void f(Object o) { if (o case Repo r) { r.find(); } }",
            "void f(Object o) { switch (o) { case Repo r: r.find(); } }",
            "void f() { for (var (Repo r, _) in list) { r.find(); } }",
            "void f() { var (Repo r,) = pair(); r.find(); }",
        ],
    )
    def test_one_node_type_covers_every_place_dart_3_introduces_one(self, source):
        """Declaration, if-case, switch case and pattern for-in are one shape."""
        assert _patterns(source) == {"r": "Repo"}

    def test_an_object_patterns_field_binding_binds_nothing(self):
        """``if (o case Repo(:final name))`` writes no type for ``name``: the field's type
        lives on ``Repo``'s declaration, which is not in view here.
        """
        assert _patterns("void f(Object o) { if (o case Repo(:final name)) {} }") == {"name": None}

    def test_a_wildcard_binds_no_name_at_all(self):
        assert _patterns("void f() { for (var (Repo r, _) in list) {} }") == {"r": "Repo"}

    def test_a_typed_pattern_resolves_a_later_call_past_a_decoy(self, tmp_path):
        call_graph = _analyze(
            tmp_path,
            {
                "a_decoy.dart": _DECOY,
                "pat.dart": "class PatA { void paFind() {} }\n"
                "class PatB { void pbOpen() {} }\n"
                "void destructure() {\n"
                "  var (PatA a, PatB b) = pair();\n"
                "  a.paFind();\n"
                "  b.pbOpen();\n"
                "}\n"
                "void guarded(Object o) {\n"
                "  if (o case PatA r) { r.paFind(); }\n"
                "}\n"
                "dynamic pair() => null;\n",
            },
        )
        assert _targets(call_graph, "pat.destructure") == [
            "pat.PatA.paFind",
            "pat.PatB.pbOpen",
            "pat.pair",
        ]
        assert _targets(call_graph, "pat.guarded") == ["pat.PatA.paFind"]


class TestExtensionStaticFields:
    """A named extension's static field is reported, as a class's is."""

    def test_a_named_extensions_static_field_is_reported(self):
        extension = "extension E on Repo {\n  static Repo shared = Repo();\n}\n"
        holder = "class E {\n  static Repo shared = Repo();\n}\n"
        assert extract_dart_field_types(_root(extension), _node_text) == [("E", "shared", "Repo")]
        assert extract_dart_field_types(_root(holder), _node_text) == [("E", "shared", "Repo")]

    @pytest.mark.parametrize(
        "declaration",
        [
            "static Repo shared = Repo();",
            "static final Repo shared = Repo();",
            "static late Repo shared;",
            "static Repo? shared;",
            "static final shared = Repo();",
        ],
    )
    def test_each_route_to_a_type_works_inside_an_extension_too(self, declaration):
        """Annotation and construction, the two routes a static field can use. A typed
        initialising formal cannot apply: an extension declares no constructor.
        """
        source = f"extension E on Repo {{\n  {declaration}\n}}\n"
        assert extract_dart_field_types(_root(source), _node_text) == [("E", "shared", "Repo")]

    def test_an_unnamed_extension_still_contributes_nothing(self):
        """An unnamed ``extension on Repo`` has no ``identifier`` child and the parser
        parents its members nowhere — their ids are ``extension$Repo.member`` — so a key
        scoped to that name would match no caller's ``parent_component``.
        """
        source = "extension on Repo {\n  static Repo shared = Repo();\n}\n"
        assert extract_dart_field_types(_root(source), _node_text) == []

    def test_an_extension_static_field_resolves_as_a_receiver_past_a_decoy(self, tmp_path):
        call_graph = _analyze(
            tmp_path,
            {
                "a_decoy.dart": _DECOY,
                "ext.dart": "class ExtBase { void ebFind() {} }\n"
                "extension ExtHolder on ExtBase {\n"
                "  static ExtBase shared = ExtBase();\n"
                "  void reach() { shared.ebFind(); }\n"
                "}\n",
            },
        )
        assert _targets(call_graph, "ext.ExtHolder.reach") == ["ext.ExtBase.ebFind"]


class TestTheThreeNodeSetsStayDistinct:
    """Three node sets answer three different questions and must stay separate.

    Field reporting, an initialising formal's field lookup, and type-parameter
    scope each widen through their own frozenset. ``_FIELD_OWNER_NODE_TYPES`` and
    ``_TYPE_PARAMETER_OWNER_NODE_TYPES`` happen to share their members today;
    separate names are what let either move later without moving the other.
    """

    def test_the_field_walk_set_adds_exactly_the_extension_declaration(self):
        assert _FIELD_OWNER_NODE_TYPES - _TYPE_DECLARATION_NODE_TYPES == {"extension_declaration"}
        assert _TYPE_DECLARATION_NODE_TYPES - _FIELD_OWNER_NODE_TYPES == set()

    def test_the_type_parameter_set_is_unmoved(self):
        assert _TYPE_PARAMETER_OWNER_NODE_TYPES - _TYPE_DECLARATION_NODE_TYPES == {
            "extension_declaration"
        }

    def test_the_initialising_formal_set_did_not_gain_the_extension(self):
        assert "extension_declaration" not in _TYPE_DECLARATION_NODE_TYPES
        assert "extension_body" not in _BODY_NODE_TYPES
        assert "extension_body" in _FIELD_BODY_NODE_TYPES

    def test_a_generic_extensions_type_parameter_is_still_not_a_class(self):
        """``extension Grid<Element> on List<Element>`` contributes ``Element`` as a type
        parameter, so a parameter annotated ``Element`` binds nothing even where a real
        ``class Element`` exists.
        """
        source = (
            "class Element { void boom() {} }\n"
            "extension Grid<Element> on List<Element> {\n"
            "  void f(Element e) { e.boom(); }\n"
            "}\n"
        )
        start = next(index for index, line in enumerate(source.splitlines()) if "void f(" in line)
        assert extract_dart_parameter_types(_root(source), start, _node_text) == {}

    def test_an_initialising_formal_still_reads_only_its_own_class(self):
        """``_enclosing_field_types`` climbs to the type declaration enclosing a
        constructor signature. An extension can declare no constructor, so widening
        that walk would only let it terminate early and miss the real enclosing class.
        """
        source = "class Repo {\n  final Store store;\n  Repo(this.store);\n}\n"
        start = next(
            index for index, line in enumerate(source.splitlines()) if "Repo(this.store)" in line
        )
        assert extract_dart_parameter_types(_root(source), start, _node_text) == {"store": "Store"}


class TestAClassTypeParameterIsNotAFieldType:
    """The parameter path filters these; the field path must too.

    `_simple_type_name` only refuses a short name like `T`, so a spelled-out type
    parameter reaches `methods_by_class_suffix` and binds to a real class of that
    name if one exists — a wrong edge, which this module ranks worse than none.
    """

    @pytest.mark.parametrize("parameter", ["TValue", "Element", "T"])
    def test_a_field_typed_by_its_class_parameter_binds_nothing(self, parameter):
        source = f"class Box<{parameter}> {{\n  {parameter} field;\n}}\n"

        assert extract_dart_field_types(_root(source), _node_text) == []

    def test_a_field_typed_by_a_real_class_still_binds(self):
        """The capability must survive, or the filter bought safety by doing nothing."""
        source = "class Repo {}\nclass Box<TValue> {\n  Repo repo;\n  TValue value;\n}\n"

        assert extract_dart_field_types(_root(source), _node_text) == [("Box", "repo", "Repo")]

    def test_an_extensions_type_parameter_is_filtered_too(self):
        source = "extension Grid<Element> on List<Element> {\n  static Element? first;\n}\n"

        assert extract_dart_field_types(_root(source), _node_text) == []


class TestAnAccessorFieldTargetIsNotDecidedByBuildOrder:
    """A getter and its setter share a bare member name, so one key fits both.

    `get_simple_name` steps over the kind suffix by design, so both accessors
    produced the identical `<caller>::<field>.<name>` key and the second write
    won. A call reaches the GETTER — a setter is only reachable by assignment,
    which is not a call — so the getter is the only correct target.
    """

    _SOURCE = """class Repo {
  void Function() get callback => () {};
  set callback(void Function() v) {}
}
class Holder {
  final Repo _repo;
  Holder(this._repo);
  void go() { _repo.callback(); }
}
"""

    def _edges(self, source: str) -> list:
        import tempfile

        directory = Path(tempfile.mkdtemp())
        (directory / "a.dart").write_text(source)
        catalog = ComponentCatalog(SimpleBusinessBehaviorAnalyzer())
        knowledge_base = CodeAnalyzer(catalog).analyze_directory(directory, "space", id_scheme=IdScheme.V1_STEM)
        analyzer = FlowAnalyzer(catalog)
        analyzer.build_call_graph(knowledge_base.components)
        for caller, callees in analyzer._call_graph.items():
            if caller.endswith("Holder.go"):
                return [c.split("::")[-1] for c, _line in callees]
        return []

    def test_invoking_a_getters_result_reaches_the_getter(self):
        assert self._edges(self._SOURCE) == ["a.Repo.callback.get"]

    def test_a_getter_only_property_still_binds(self):
        source = self._SOURCE.replace("  set callback(void Function() v) {}\n", "")

        assert self._edges(source) == ["a.Repo.callback.get"]

    def test_a_setter_only_property_binds_nothing(self):
        """A setter cannot be reached by a call, so no edge is the right answer."""
        source = self._SOURCE.replace("  void Function() get callback => () {};\n", "")

        assert self._edges(source) == []
