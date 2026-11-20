"""Tests for Dart flow support: the four functions and their four dispatch sites.

Resolution tests run through the real ``FlowAnalyzer`` over components a parser
produced from files on disk, because a support module that exists but is not
dispatched behaves exactly as absent. Unit-level tests parse a snippet directly.

No other language registers a bare ``field.method`` key — Ruby's carry an ``@``
sigil and Python's are ``self.attr.method`` — so the hazard the scoping tests
guard is capture: a Dart key answering a call nobody had claimed.
"""

from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

try:  # pragma: no cover - import guard, mirrors test_parser_integration.py
    import tree_sitter_dart as tsdart

    DART_AVAILABLE = True
except ImportError:  # pragma: no cover
    tsdart = None
    DART_AVAILABLE = False

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from src.catalog.component_catalog import ComponentCatalog
from src.core.models import Component, IdScheme
from src.flow.dart_flow_support import (
    _simple_type_name,
    extract_dart_assignment_type,
    extract_dart_field_types,
    extract_dart_parameter_types,
    get_dart_parameter_names,
)
from src.flow.flow_analyzer import FlowAnalyzer
from src.parsers import DartParser, PythonParser

pytestmark = pytest.mark.skipif(not DART_AVAILABLE, reason="tree_sitter_dart not installed")


# ── harness ───────────────────────────────────────────────────────────────────


def _dart_root(source: str) -> Any:
    """Parse a Dart snippet and return its root node."""
    return Parser(Language(tsdart.language())).parse(source.encode()).root_node


def _node_text(node: Any) -> str:
    """Stand in for ``FlowAnalyzer._get_node_text_simple``, sentinel included."""
    try:
        text: str = node.text.decode("utf-8")
        return text
    except Exception:  # pragma: no cover - defensive, matches the analyzer
        return "unknown"


def _unknown_text(_node: Any) -> str:
    """A text function that always fails, as the analyzer's does on a bad node."""
    return "unknown"


_PARSERS = {
    ".dart": (DartParser, lambda: Language(tsdart.language())),
    ".py": (PythonParser, lambda: Language(tspython.language())),
}


def _analyze(tmp_path: Path, files: Dict[str, str]) -> Tuple[FlowAnalyzer, Dict[str, List[str]]]:
    """Write a tree of source files and build one real call graph over all of them.

    One catalog and one ``build_call_graph`` call, so every component shares the
    single ``component_lookup`` the cross-language tests depend on.
    """
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
    call_graph = analyzer.build_call_graph(components)
    return analyzer, call_graph


def _start_line(source: str, needle: str) -> int:
    """Zero-based row of the line containing ``needle``, as the analyzer passes it."""
    for index, line in enumerate(source.splitlines()):
        if needle in line:
            return index
    raise AssertionError(f"{needle!r} not found in snippet")


def _param_types(source: str, needle: str = "void f(") -> Dict[str, str]:
    """Run parameter-type extraction against the function on the matching line."""
    return extract_dart_parameter_types(_dart_root(source), _start_line(source, needle), _node_text)


def _param_names(source: str, needle: str = "void f(") -> List[str]:
    """Run parameter-name extraction against the function on the matching line."""
    return get_dart_parameter_names(_dart_root(source), _start_line(source, needle), _node_text)


def _first_assignment(source: str, node_type: str = "local_variable_declaration") -> Any:
    """Return the first node of ``node_type`` in a snippet."""
    found: List[Any] = []

    def visit(node: Any) -> None:
        if node.type == node_type:
            found.append(node)
        for child in node.children:
            visit(child)

    visit(_dart_root(source))
    assert found, f"no {node_type} in snippet"
    return found[0]


def _assignment(source: str, node_type: str = "local_variable_declaration") -> Tuple[Any, Any]:
    return extract_dart_assignment_type(_first_assignment(source, node_type), _node_text)


def _all_assignments(source: str) -> Dict[str, str]:
    """Bind every declaration and reassignment in a snippet, as the analyzer does.

    Mirrors the analyzer's guarded per-node dispatch, including ``variable_pattern``,
    the node Dart 3 pattern variables bind through.
    """
    bindings: Dict[str, str] = {}

    def visit(node: Any) -> None:
        if node.type in (
            "local_variable_declaration",
            "initialized_identifier",
            "assignment_expression",
            "variable_pattern",
        ):
            name, type_name = extract_dart_assignment_type(node, _node_text)
            if name and type_name:
                bindings[name] = type_name
        for child in node.children:
            visit(child)

    visit(_dart_root(source))
    return bindings


REPO = """class Repo {
  void find() {}
}
"""


# ── the two tests that carry the weight ───────────────────────────────────────


class TestFieldKeyIsScoped:
    """The field receiver widening must reach only the Dart class that declares it."""

    def test_non_dart_call_shaped_like_a_dart_field_gains_no_edge(self, tmp_path):
        """A Python call written exactly like a Dart field call resolves to nothing.

        If the Dart field path registered ``_repo.find`` bare, this Python call would
        start reaching a Dart method; the key is scoped to its caller instead.
        """
        _, call_graph = _analyze(
            tmp_path,
            {
                "svc.dart": REPO
                + """class Service {
  final Repo _repo;
  Service(this._repo);
  void run() {
    _repo.find();
  }
}
""",
                "pysvc.py": """class Service:
    def run(self):
        _repo.find()
""",
            },
        )

        # The Dart caller does resolve: the key exists, scoped to it.
        assert "svc.Repo.find" in call_graph["svc.Service.run"]

        # The Python caller, writing the identical call, reaches nothing.
        assert call_graph["pysvc.Service.run"] == []

    def test_same_named_fields_of_different_types_resolve_separately(self, tmp_path):
        """A bare key carries neither class nor file, so one of these would win by
        build order. The caller's component id carries both.
        """
        _, call_graph = _analyze(
            tmp_path,
            {
                "a.dart": """import 'types.dart';

class ServiceA {
  final Repo _repo;
  ServiceA(this._repo);
  void run() {
    _repo.find();
  }
}
""",
                "b.dart": """import 'types.dart';

class ServiceB {
  final Other _repo;
  ServiceB(this._repo);
  void run() {
    _repo.find();
  }
}
""",
                "types.dart": REPO
                + """class Other {
  void find() {}
}
""",
            },
        )

        assert call_graph["a.ServiceA.run"] == ["types.Repo.find"]
        assert call_graph["b.ServiceB.run"] == ["types.Other.find"]


# ── extract_dart_parameter_types ──────────────────────────────────────────────


class TestParameterTypes:
    def test_required_positional_binds(self):
        assert _param_types("void f(Repo r) {}") == {"r": "Repo"}

    def test_optional_positional_binds(self):
        assert _param_types("void f([Repo? r]) {}") == {"r": "Repo"}

    def test_named_binds(self):
        assert _param_types("void f({required Repo r}) {}") == {"r": "Repo"}

    def test_initialising_formal_binds_the_field_type(self):
        source = """class Holder {
  final Repo repo;
  Holder(this.repo);
}
"""
        assert _param_types(source, needle="Holder(this.repo)") == {"repo": "Repo"}

    def test_typed_initialising_formal_binds_its_own_annotation(self):
        source = """class Holder {
  final repo;
  Holder(Repo this.repo);
}
"""
        assert _param_types(source, needle="Holder(Repo this.repo)") == {"repo": "Repo"}

    def test_initialising_formal_with_no_visible_field_binds_nothing(self):
        source = """class Holder {
  Holder(this.repo);
}
"""
        assert _param_types(source, needle="Holder(this.repo)") == {}

    def test_super_formal_binds_nothing_when_the_field_is_in_the_superclass(self):
        source = """class Child extends Parent {
  Child(super.repo);
}
"""
        assert _param_types(source, needle="Child(super.repo)") == {}

    def test_function_typed_parameters_bind_nothing(self):
        assert _param_types("void f(void Function(Repo) cb) {}") == {}
        assert _param_types("void f(Repo cb()) {}") == {}

    def test_unannotated_parameter_binds_nothing(self):
        assert _param_types("void f(a, Repo r) {}") == {"r": "Repo"}

    def test_a_multi_line_parameter_list_is_read_whole(self):
        source = "void f(\n  Repo a,\n  Other b,\n) {}"
        assert _param_types(source) == {"a": "Repo", "b": "Other"}

    def test_empty_parameter_list_binds_nothing(self):
        assert _param_types("void f() {}") == {}

    def test_getter_binds_nothing_and_does_not_fail(self):
        source = """class Holder {
  Repo get repo => _repo;
}
"""
        assert _param_types(source, needle="get repo") == {}

    def test_a_line_with_no_signature_binds_nothing(self):
        assert _param_types("class A {}\n", needle="class A") == {}


class TestSharedTypeReduction:
    """Annotation reduction, exercised through the parameter path that applies it."""

    @pytest.mark.parametrize(
        "annotation,expected",
        [
            ("Repo?", "Repo"),
            ("List<Repo>", "List"),
            ("Map<String, Repo>", "Map"),
            ("prefix.Repo", "Repo"),
            ("prefix.Repo?", "Repo"),
            ("List<prefix.Repo>", "List"),
        ],
    )
    def test_reductions(self, annotation, expected):
        assert _param_types(f"void f({annotation} r) {{}}") == {"r": expected}

    @pytest.mark.parametrize(
        "annotation",
        ["dynamic", "Object", "Object?", "void", "Never", "Function"],
    )
    def test_no_information_types_bind_nothing(self, annotation):
        assert _param_types(f"void f({annotation} r) {{}}") == {}

    def test_an_unreadable_annotation_binds_nothing(self):
        """``unknown`` is the text function's failure sentinel, never a type name."""
        source = "void f(Repo r) {}"
        assert extract_dart_parameter_types(_dart_root(source), 0, _unknown_text) == {}

    def test_a_declared_type_parameter_binds_nothing(self):
        assert _param_types("void f<T>(T x, Repo r) {}", needle="void f<T>") == {"r": "Repo"}

    @pytest.mark.parametrize("letter", ["T", "E", "T1"])
    def test_the_type_parameter_heuristic_binds_nothing(self, letter):
        """Where the declaration is not visible, a bare letter is not a class."""
        assert _param_types(f"void f({letter} x) {{}}") == {}

    def test_a_class_type_parameter_binds_nothing(self):
        source = """class Box<TValue> {
  void put(TValue v, Repo r) {}
}
"""
        assert _param_types(source, needle="void put") == {"r": "Repo"}


class TestTheReductionDirectly:
    """The shared reduction, on text the grammar does not hand it through a node.

    A declaration modifier never reaches the reduction from a parameter — the grammar
    gives ``final`` its own node and the extractors drop it — but the reduction is
    the one place the rule is stated.
    """

    @pytest.mark.parametrize(
        "written,expected",
        [
            ("final Repo", "Repo"),
            ("late final Repo", "Repo"),
            ("const Repo", "Repo"),
            ("  Repo  ", "Repo"),
            ("_Repo", "_Repo"),
            ("Repo$Impl", "Repo$Impl"),
        ],
    )
    def test_it_reduces(self, written, expected):
        assert _simple_type_name(written) == expected

    @pytest.mark.parametrize(
        "written",
        [
            None,
            "",
            "   ",
            "unknown",
            "final",
            "(int, int)",
            "void Function(Repo)",
            "Repo Impl",
        ],
    )
    def test_it_refuses(self, written):
        assert _simple_type_name(written) is None


# ── extract_dart_assignment_type ──────────────────────────────────────────────


class TestAssignmentTypes:
    def test_an_annotation_beats_a_construction(self):
        """``Repo r = SubRepo();`` binds what the programmer declared."""
        assert _assignment("void f() { Repo r = SubRepo(); }") == ("r", "Repo")

    @pytest.mark.parametrize(
        "statement",
        ["var r = Repo();", "final r = Repo.named();", "var r = new Repo();"],
    )
    def test_a_construction_binds_its_class(self, statement):
        assert _assignment(f"void f() {{ {statement} }}") == ("r", "Repo")

    def test_a_private_class_construction_binds(self):
        assert _assignment("void f() { var r = _Repo(); }") == ("r", "_Repo")

    def test_an_as_cast_binds_its_target(self):
        assert _assignment("void f() { var r = x as Repo; }") == ("r", "Repo")

    def test_a_prefixed_cast_reduces(self):
        assert _assignment("void f() { var r = x as prefix.Repo; }") == ("r", "Repo")

    def test_await_binds_nothing(self):
        """Descending through ``await`` would bind the inner call's name."""
        assert _assignment("void f() { var r = await makeRepo(); }") == ("r", None)

    @pytest.mark.parametrize(
        "statement",
        [
            "var r = 3;",
            "var r = [1, 2];",
            "var r = c ? Repo() : Other();",
            "var r = makeRepo();",
            "var r = () => Repo();",
            "var r = other;",
        ],
    )
    def test_a_non_construction_binds_nothing(self, statement):
        assert _assignment(f"void f() {{ {statement} }}") == ("r", None)

    @pytest.mark.parametrize(
        "statement",
        [
            "var r = Repo()..save();",
            "var r = Repo()..save()..clear();",
            "var r = new Repo()..save();",
            "var r = const Repo()..save();",
        ],
    )
    def test_a_construction_with_a_cascade_binds_its_class(self, statement):
        """A cascade evaluates to its target by Dart's own rule, so ``Repo()..save()``
        is a ``Repo`` without inferring anything about the cascaded calls.
        """
        assert _assignment(f"void f() {{ {statement} }}") == ("r", "Repo")

    @pytest.mark.parametrize(
        "statement",
        [
            "var r = x..save();",
            "var r = makeRepo()..save();",
            "var r = other..save()..clear();",
        ],
    )
    def test_a_cascade_over_a_non_construction_still_binds_nothing(self, statement):
        """Stripping the cascade must not become descending through it.

        ``x..save()`` reduces to the bare identifier ``x``; walking into the sections
        would instead bind the variable to the method name ``save``.
        """
        assert _assignment(f"void f() {{ {statement} }}") == ("r", None)

    def test_a_multi_variable_declaration_binds_each_and_never_cross_binds(self):
        bindings = _all_assignments("void f() { var a = Repo(), b = Other(); }")
        assert bindings == {"a": "Repo", "b": "Other"}

    def test_a_multi_variable_declaration_binds_only_what_it_can(self):
        bindings = _all_assignments("void f() { var a = Repo(), b = 3; }")
        assert bindings == {"a": "Repo"}

    def test_a_reassignment_binds(self):
        assert _assignment(
            "void f() { var r = 1; r = Repo(); }", node_type="assignment_expression"
        ) == ("r", "Repo")

    def test_a_field_reassignment_target_is_not_bound(self):
        """``a.b = Repo();`` names no single variable, so nothing is bound."""
        assert _assignment("void f() { a.b = Repo(); }", node_type="assignment_expression") == (
            None,
            None,
        )

    def test_an_unhandled_node_type_binds_nothing(self):
        assert extract_dart_assignment_type(_dart_root("void f() {}"), _node_text) == (
            None,
            None,
        )

    def test_a_declaration_with_only_an_annotation_binds(self):
        assert _assignment("void f() { Repo? r; }") == ("r", "Repo")


# ── get_dart_parameter_names ──────────────────────────────────────────────────


class TestParameterNames:
    """The sequence is POSITIONAL parameters in declaration order.

    The only consumer pairs index ``i`` of this sequence with index ``i`` of a call
    site's argument types, and a Dart call site writes named arguments in any order,
    so a named parameter in this list is a mis-binding waiting to happen.
    """

    def test_only_the_positional_parameters_of_all_three_kinds_are_returned(self):
        """Dart forbids one signature carrying both ``[...]`` and ``{...}``, so the
        grammar parses the bracketed group under an ``ERROR`` node. The collector
        descends into it; the ``{...}`` group is skipped because it is named.
        """
        source = "void f(Repo a, [Repo? b], {Repo? c}) {}"
        assert _param_names(source) == ["a", "b"]

    def test_declaration_order_on_a_valid_optional_positional_signature(self):
        assert _param_names("void f(Repo a, [Repo? b]) {}") == ["a", "b"]

    def test_named_parameters_are_excluded_because_a_call_site_does_not_order_them(self):
        """``go(1, c: x)`` and ``go(1, b: x)`` both write their named argument at
        call-site index 1, so index 1 of this sequence cannot answer for both. A named
        argument is recorded against its own parameter name instead.
        """
        assert _param_names("void f(Repo a, {required Repo b, Other? c}) {}") == ["a"]

    def test_a_signature_with_only_named_parameters_returns_empty(self):
        assert _param_names("void f({Repo? a, Repo? b}) {}") == []

    def test_an_empty_parameter_list_returns_empty(self):
        assert _param_names("void f() {}") == []

    def test_the_function_at_the_asked_for_line_is_the_one_read(self):
        source = """void first(Repo a) {}

void second(Other b, Repo c) {}
"""
        assert _param_names(source, needle="void second") == ["b", "c"]
        assert _param_names(source, needle="void first") == ["a"]

    def test_initialising_formals_are_named_without_their_prefix(self):
        source = """class Holder {
  final Repo repo;
  Holder(this.repo, super.other);
}
"""
        assert _param_names(source, needle="Holder(") == ["repo", "other"]

    def test_a_line_with_no_signature_returns_empty(self):
        assert _param_names("class A {}\n", needle="class A") == []

    def test_a_function_with_no_body_still_reports_its_parameters(self):
        assert _param_names("void f(Repo r);") == ["r"]

    def test_the_dart_branch_is_wired_into_the_parameter_names_chain(self):
        """Asserted through the dispatcher rather than through a call graph, which
        never enters this branch.
        """
        analyzer = FlowAnalyzer(ComponentCatalog())
        assert analyzer._get_parameter_names(_dart_root("void f(Repo r) {}"), 0, "dart") == ["r"]


# ── extract_dart_field_types ──────────────────────────────────────────────────


class TestFieldTypes:
    def test_an_annotated_field_is_reported_with_its_declaring_class(self):
        source = """class Holder {
  final Repo _repo;
}
"""
        assert extract_dart_field_types(_dart_root(source), _node_text) == [
            ("Holder", "_repo", "Repo")
        ]

    def test_a_late_final_field_is_reported(self):
        source = """class Holder {
  late final Repo _repo;
}
"""
        assert extract_dart_field_types(_dart_root(source), _node_text) == [
            ("Holder", "_repo", "Repo")
        ]

    def test_a_field_constructed_in_its_initialiser_is_reported(self):
        source = """class Holder {
  final _repo = Repo();
}
"""
        assert extract_dart_field_types(_dart_root(source), _node_text) == [
            ("Holder", "_repo", "Repo")
        ]

    def test_a_field_constructed_in_a_constructor_body_is_reported(self):
        source = """class Holder {
  Repo _repo;
  Holder() {
    _repo = Repo();
  }
}
"""
        assert extract_dart_field_types(_dart_root(source), _node_text) == [
            ("Holder", "_repo", "Repo")
        ]

    def test_a_typed_initialising_formal_over_an_unannotated_field_is_reported(self):
        source = """class Holder {
  final _repo;
  Holder(Repo this._repo);
}
"""
        assert extract_dart_field_types(_dart_root(source), _node_text) == [
            ("Holder", "_repo", "Repo")
        ]

    def test_an_annotation_wins_over_a_constructed_subtype(self):
        source = """class Holder {
  Repo _repo;
  Holder() {
    _repo = SubRepo();
  }
}
"""
        assert extract_dart_field_types(_dart_root(source), _node_text) == [
            ("Holder", "_repo", "Repo")
        ]

    def test_an_assignment_in_an_ordinary_method_is_not_a_field_type(self):
        """Only a constructor body is read: elsewhere a bare name is a local."""
        source = """class Holder {
  void run() {
    helper = Repo();
  }
}
"""
        assert extract_dart_field_types(_dart_root(source), _node_text) == []

    @pytest.mark.parametrize(
        "declaration",
        ["final _repo;", "dynamic _repo;", "var _repo = 3;", "final _repo = makeRepo();"],
    )
    def test_a_field_with_no_resolvable_type_is_not_reported(self, declaration):
        source = f"class Holder {{\n  {declaration}\n}}\n"
        assert extract_dart_field_types(_dart_root(source), _node_text) == []

    def test_a_static_field_is_reported(self):
        source = """class Holder {
  static final Repo shared = Repo();
}
"""
        assert extract_dart_field_types(_dart_root(source), _node_text) == [
            ("Holder", "shared", "Repo")
        ]

    def test_a_mixin_field_is_reported_under_the_mixin_name(self):
        source = """mixin Logging {
  final Repo _repo;
}
"""
        assert extract_dart_field_types(_dart_root(source), _node_text) == [
            ("Logging", "_repo", "Repo")
        ]

    def test_each_class_in_a_file_reports_its_own_fields(self):
        source = """class A {
  final Repo _repo;
}

class B {
  final Other _repo;
}
"""
        assert extract_dart_field_types(_dart_root(source), _node_text) == [
            ("A", "_repo", "Repo"),
            ("B", "_repo", "Other"),
        ]

    def test_a_class_with_no_fields_reports_nothing(self):
        source = """abstract class Holder {
  void run();
  Repo get repo;
}
"""
        assert extract_dart_field_types(_dart_root(source), _node_text) == []

    def test_a_file_with_no_declarations_reports_nothing(self):
        assert extract_dart_field_types(_dart_root("// only a comment\n"), _node_text) == []

    def test_an_unreadable_class_name_reports_nothing(self):
        source = "class Holder {\n  final Repo _repo;\n}\n"
        assert extract_dart_field_types(_dart_root(source), _unknown_text) == []


# ── end to end, through the real flow analysis ────────────────────────────────


class TestEndToEnd:
    def test_a_caller_to_callee_edge_carries_its_line_number(self, tmp_path):
        """Through the shipped walker rather than this module's helpers."""
        analyzer, call_graph = _analyze(
            tmp_path,
            {
                "app.dart": """void helper() {}

void main() {
  helper();
}
"""
            },
        )

        assert "app.helper" in call_graph["app.main"]
        assert analyzer._call_graph["app.main"] == [("app.helper", 4)]

    def test_a_call_on_an_annotated_parameter_resolves(self, tmp_path):
        _, call_graph = _analyze(
            tmp_path,
            {
                "app.dart": REPO
                + """void use(Repo r) {
  r.find();
}
"""
            },
        )

        assert call_graph["app.use"] == ["app.Repo.find"]

    def test_a_call_on_a_named_parameter_resolves(self, tmp_path):
        """A named parameter is typed from its own annotation, not from a call site."""
        _, call_graph = _analyze(
            tmp_path,
            {
                "app.dart": REPO
                + """void use({required Repo r}) {
  r.find();
}
"""
            },
        )

        assert call_graph["app.use"] == ["app.Repo.find"]

    def test_a_call_on_a_local_typed_by_construction_resolves(self, tmp_path):
        _, call_graph = _analyze(
            tmp_path,
            {
                "app.dart": REPO
                + """void use() {
  var r = Repo();
  r.find();
}
"""
            },
        )

        assert "app.Repo.find" in call_graph["app.use"]

    def test_a_call_on_a_cast_local_resolves(self, tmp_path):
        _, call_graph = _analyze(
            tmp_path,
            {
                "app.dart": REPO
                + """void use(Object o) {
  var r = o as Repo;
  r.find();
}
"""
            },
        )

        assert call_graph["app.use"] == ["app.Repo.find"]

    @pytest.mark.parametrize(
        "field_declaration,constructor",
        [
            ("  final Repo _repo;\n", "  Service(this._repo);\n"),
            ("  final _repo = Repo();\n", ""),
            ("  final _repo;\n", "  Service(Repo this._repo);\n"),
        ],
    )
    def test_a_field_typed_each_of_the_three_ways_resolves(
        self, tmp_path, field_declaration, constructor
    ):
        _, call_graph = _analyze(
            tmp_path,
            {
                "app.dart": REPO
                + "class Service {\n"
                + field_declaration
                + constructor
                + """  void run() {
    _repo.find();
  }
}
"""
            },
        )

        assert call_graph["app.Service.run"] == ["app.Repo.find"]

    def test_a_field_with_no_resolvable_type_registers_nothing(self, tmp_path):
        _, call_graph = _analyze(
            tmp_path,
            {
                "app.dart": REPO
                + """class Service {
  final _repo;
  void run() {
    _repo.find();
  }
}
"""
            },
        )

        assert call_graph["app.Service.run"] == []

    def test_a_field_call_from_another_dart_class_in_the_same_file_gains_no_edge(self, tmp_path):
        """The key is scoped to the declaring class, not to the file."""
        _, call_graph = _analyze(
            tmp_path,
            {
                "app.dart": REPO
                + """class Owner {
  final Repo _repo;
  Owner(this._repo);
}

class Stranger {
  void run() {
    _repo.find();
  }
}
"""
            },
        )

        assert call_graph["app.Stranger.run"] == []

    def test_a_dart_file_with_no_classes_or_functions_analyses_cleanly(self, tmp_path):
        _, call_graph = _analyze(tmp_path, {"empty.dart": "// nothing here\n"})
        assert call_graph == {}

    def test_an_abstract_method_and_a_getter_analyse_cleanly(self, tmp_path):
        _, call_graph = _analyze(
            tmp_path,
            {
                "app.dart": REPO
                + """abstract class Service {
  void run();
  Repo get repo;
}
"""
            },
        )

        assert call_graph["app.Service.run"] == []

    def test_python_self_attribute_resolution_still_works_beside_dart(self, tmp_path):
        """Dart writes into ``component_lookup``, which every language reads: the
        Python ``self.attr.method`` path is unaffected when Dart components are built in
        the same pass.
        """
        _, call_graph = _analyze(
            tmp_path,
            {
                "app.dart": REPO
                + """class Service {
  final Repo _repo;
  Service(this._repo);
  void run() {
    _repo.find();
  }
}
""",
                "pyapp.py": """class Repo:
    def find(self):
        pass


class Service:
    def __init__(self):
        self._repo = Repo()

    def run(self):
        self._repo.find()
""",
            },
        )

        assert "pyapp.Repo.find" in call_graph["pyapp.Service.run"]
        assert "app.Repo.find" in call_graph["app.Service.run"]


# ── scoping, construction forms, and recorded gaps ────────────────────────────


class TestAmbiguousFieldTargetRegistersNothing:
    """The target side of the scoped key must not be decided by build order.

    The caller's component id keeps two same-named *fields* apart but does nothing
    about two same-named *classes*: every file declaring the matched name writes the
    same ``<caller>::<field>.<method>`` key. The rule is declaring file first, then a
    unique match, then nothing.
    """

    _TWO_DECLARATIONS = {
        "one.dart": """class Repo {
  void find() {}
}
""",
        "two.dart": """class Repo {
  void find() {}
}
""",
        "svc.dart": """import 'one.dart';

class Service {
  final Repo _repo;
  Service(this._repo);
  void run() {
    _repo.find();
  }
}
""",
    }

    def test_a_class_name_declared_in_several_files_resolves_to_none_of_them(self, tmp_path):
        """``svc.dart`` imports only ``one.dart``, but nothing here reads import
        directives, so neither declaration can be preferred and the call is left
        unresolved rather than guessed.
        """
        _, call_graph = _analyze(tmp_path, dict(self._TWO_DECLARATIONS))

        assert call_graph["svc.Service.run"] == []

    def test_a_class_name_declared_in_exactly_one_other_file_still_resolves(self, tmp_path):
        """One declaration elsewhere is unambiguous, so the edge stands: the ambiguity
        rule must not delete the ordinary cross-file edge.
        """
        files = dict(self._TWO_DECLARATIONS)
        del files["two.dart"]

        _, call_graph = _analyze(tmp_path, files)

        assert call_graph["svc.Service.run"] == ["one.Repo.find"]

    def test_a_declaration_in_the_field_s_own_file_wins_over_another_file(self, tmp_path):
        files = dict(self._TWO_DECLARATIONS)
        files["svc.dart"] = REPO + files["svc.dart"]

        _, call_graph = _analyze(tmp_path, files)

        assert call_graph["svc.Service.run"] == ["svc.Repo.find"]


class TestConstConstruction:
    """``const Repo()`` is a construction, and a common Flutter field form."""

    def test_a_const_construction_binds_a_local(self):
        assert _assignment("void f() { var r = const Repo(); }") == ("r", "Repo")

    def test_a_const_construction_binds_an_unannotated_field(self):
        source = """class Holder {
  final _repo = const Repo();
}
"""
        assert extract_dart_field_types(_dart_root(source), _node_text) == [
            ("Holder", "_repo", "Repo")
        ]

    def test_a_const_construction_in_a_constructor_body_binds_the_field(self):
        """An untyped field, so route 1 cannot cover it and the const branch must."""
        source = """class Holder {
  var _repo;
  Holder() {
    _repo = const Repo();
  }
}
"""
        assert ("Holder", "_repo", "Repo") in extract_dart_field_types(
            _dart_root(source), _node_text
        )

    def test_a_call_on_a_const_constructed_field_resolves_end_to_end(self, tmp_path):
        _, call_graph = _analyze(
            tmp_path,
            {
                "app.dart": REPO
                + """class Service {
  final _repo = const Repo();
  void run() {
    _repo.find();
  }
}
"""
            },
        )

        assert call_graph["app.Service.run"] == ["app.Repo.find"]


class TestModifiedParameterAnnotations:
    """The ``_parameter_parts`` modifier skip is load-bearing, not unreachable.

    ``final Repo r`` puts a ``final_builtin`` before the type and ``covariant Repo r``
    an anonymous ``covariant`` token. Without the skip the joined annotation reads
    ``finalRepo`` / ``covariantRepo``, a name no component carries.
    """

    @pytest.mark.parametrize(
        "source",
        [
            "void f(final Repo r) {}",
            "void f(covariant Repo r) {}",
        ],
    )
    def test_a_modifier_before_the_type_does_not_reach_the_annotation(self, source):
        assert _param_types(source) == {"r": "Repo"}

    @pytest.mark.parametrize(
        "source",
        [
            "void f(final Repo r) {}",
            "void f(covariant Repo r) {}",
        ],
    )
    def test_a_modified_parameter_is_still_named(self, source):
        assert _param_names(source) == ["r"]

    def test_a_call_on_a_final_parameter_resolves_end_to_end(self, tmp_path):
        _, call_graph = _analyze(
            tmp_path,
            {
                "app.dart": REPO
                + """void use(final Repo r) {
  r.find();
}
"""
            },
        )

        assert call_graph["app.use"] == ["app.Repo.find"]


class TestSignatureWithoutAParameterList:
    """A getter signature carries no ``formal_parameter_list``.

    ``int get x => 1;`` parses as ``method_signature(getter_signature)`` with no
    parameter list anywhere beneath it.
    """

    def test_a_getter_reports_no_parameter_names(self):
        source = """class Holder {
  int get count => 1;
}
"""
        assert _param_names(source, needle="int get count") == []

    def test_a_getter_binds_no_parameter_types(self):
        source = """class Holder {
  int get count => 1;
}
"""
        assert _param_types(source, needle="int get count") == {}


class TestPrefixedConstruction:
    """``new prefix.Repo()`` bound the *prefix*, which reached a real class.

    A lower-cased class key is registered for every method, so a library prefix
    beside a same-named class gained an edge at a class the calling file never
    mentions. ``prefix.Repo`` and ``Repo.named`` both parse as ``type_identifier``
    ``.`` ``identifier``, so the last class-shaped segment is taken.
    """

    def test_a_prefixed_new_construction_binds_the_class_not_the_prefix(self):
        assert _assignment("void f() { var r = new prefix.Repo(); }") == ("r", "Repo")

    def test_a_prefixed_const_construction_binds_the_class_not_the_prefix(self):
        assert _assignment("void f() { var r = const prefix.Repo(); }") == ("r", "Repo")

    @pytest.mark.parametrize(
        "statement",
        [
            "var r = new Repo();",
            "var r = const Repo();",
            "var r = new Repo.named();",
            "var r = const Repo.named();",
            "var r = new prefix.Repo.named();",
            "var r = new prefix.Repo<int>();",
            "var r = new _Repo2();",
        ],
    )
    def test_every_other_written_form_still_binds_a_class(self, statement):
        """``Repo.named`` has the same node shape as ``prefix.Repo``, so taking the last
        *segment* would bind ``named``; the last *class-shaped* segment binds ``Repo`` in
        both.
        """
        name, bound = _assignment(f"void f() {{ {statement} }}")
        assert name == "r"
        assert bound in ("Repo", "_Repo2")

    def test_a_prefixed_new_construction_resolves_to_the_prefixed_class(self, tmp_path):
        _, call_graph = _analyze(
            tmp_path,
            {
                "app.dart": """void main() {
  var r = new prefix.Repo();
  r.find();
}
""",
                "lib.dart": REPO
                + """class Prefix {
  void build() {}
}
""",
            },
        )

        assert call_graph["app.main"] == ["lib.Repo.find"]

    def test_the_prefix_no_longer_reaches_a_same_named_class(self, tmp_path):
        """With no ``Repo`` anywhere, nothing is bound: ``prefix`` previously reached
        ``class Prefix`` through the lower-cased class key.
        """
        _, call_graph = _analyze(
            tmp_path,
            {
                "app.dart": """void main() {
  var r = new prefix.Repo();
  r.build();
}
""",
                "lib.dart": """class Prefix {
  void build() {}
}
""",
            },
        )

        assert call_graph["app.main"] == []

    def test_an_unconventional_lower_case_class_now_binds_nothing(self):
        """``new repo()`` is legal Dart against the naming convention, and binds
        nothing: a missing edge, never a wrong one.
        """
        assert _assignment("void f() { var r = new repo(); }") == ("r", None)

    @pytest.mark.parametrize(
        "statement",
        ["var r = new prefix.Repo();", "var r = new Repo();", "var r = const Repo();"],
    )
    def test_an_unreadable_segment_binds_nothing(self, statement):
        """One unreadable segment discards the whole construction, so the analyzer's
        ``"unknown"`` sentinel can never become a type name. The text function here
        fails on ``type_identifier`` only.
        """

        def text(node: Any) -> str:
            return "unknown" if node.type == "type_identifier" else _node_text(node)

        node = _first_assignment(f"void f() {{ {statement} }}")
        assert extract_dart_assignment_type(node, text) == ("r", None)


class TestGenericExtensionTypeParameter:
    """A generic extension's type parameter was bound as a class name.

    The enclosing-declaration walk recognised only the declarations whose bodies are
    searched for fields, and every extension is excluded from that set, so
    ``Element`` in ``extension Grid<Element>`` was read as the class ``Element``. The
    single-letter heuristic hid ``T`` but not a spelled-out name.
    """

    _EXTENSION = """extension Grid<Element> on List<Element> {
  void each(Element e) {
    e.render();
  }
}
"""

    _CONTROL = """class Grid<Element> {
  void each(Element e) {
    e.render();
  }
}
"""

    _ELEMENT = """class Element {
  void render() {}
}
"""

    def test_an_extension_type_parameter_binds_no_parameter(self):
        assert _param_types(self._EXTENSION, needle="void each(") == {}

    def test_a_class_type_parameter_binds_no_parameter(self):
        """The control, which behaved correctly all along."""
        assert _param_types(self._CONTROL, needle="void each(") == {}

    def test_an_extension_type_parameter_gains_no_edge(self, tmp_path):
        _, call_graph = _analyze(tmp_path, {"ext.dart": self._EXTENSION, "lib.dart": self._ELEMENT})

        assert call_graph["ext.Grid.each"] == []

    def test_the_class_shaped_control_gains_no_edge(self, tmp_path):
        _, call_graph = _analyze(tmp_path, {"ext.dart": self._CONTROL, "lib.dart": self._ELEMENT})

        assert call_graph["ext.Grid.each"] == []

    def test_a_multi_parameter_extension_binds_neither(self):
        source = """extension MapX<Key, Value> on Map<Key, Value> {
  void put(Key k, Value v) {}
}
"""
        assert _param_types(source, needle="void put(") == {}

    def test_an_extension_method_still_types_a_real_class_parameter(self, tmp_path):
        _, call_graph = _analyze(
            tmp_path,
            {
                "ext.dart": """extension Grid<Element> on List<Element> {
  void each(Repo r) {
    r.find();
  }
}
""",
                "lib.dart": REPO,
            },
        )

        assert call_graph["ext.Grid.each"] == ["lib.Repo.find"]


class TestAConstructionCallFeedsPositionalInference:
    """A ``new_expression`` whose callee resolves to a FUNCTION, not a constructor.

    That component IS in the re-processed callable set, so the inferred type is
    consumed and yields an edge — the route ``test_dart_interprocedural.py`` does
    not cover, since it calls through a plain selector.
    """

    _FILES = {
        "aaa.dart": """void Box(Repo r) {
  r.find();
}
""",
        "zzz.dart": """class Box {}

void main() {
  var a = Repo();
  var b = new Box(a);
}
""",
        "lib.dart": REPO,
    }

    def test_a_constructor_less_class_puts_a_function_key_in_the_map(self, tmp_path):
        analyzer, call_graph = _analyze(tmp_path, dict(self._FILES))

        assert "aaa.Box" in analyzer._inferred_param_types
        assert "aaa.Box" in call_graph  # a FUNCTION, so it *is* re-processed

    def test_the_dart_values_of_the_map_are_now_populated(self, tmp_path):
        """A ``selector`` holds ``argument_part`` with ``arguments`` one level deeper
        than the shared reader's scan of direct children, and wraps each written
        positional argument in an ``argument`` node; the Dart branch unwraps both.
        """
        analyzer, call_graph = _analyze(tmp_path, dict(self._FILES))

        assert analyzer._inferred_param_types["aaa.Box"] == {0: "Repo"}
        assert call_graph["aaa.Box"] == ["lib.Repo.find"]


class TestRecordedGaps:
    """Forms that resolve to nothing because nothing in view types them.

    Each is a missing binding, never a wrong one, which is this module's standing
    choice for a form it does not resolve.
    """

    def test_a_pattern_variable_declaration_without_types_binds_nothing(self):
        """``var (a, b) = (Repo(), Other())`` parses its variables as
        ``constant_pattern`` with no written type, and the element types of the record on
        the right are not in view, so binding either name would be a guess.
        """
        assert _all_assignments("void f() { var (a, b) = (Repo(), Other()); }") == {}

    def test_a_pattern_assignment_binds_nothing(self):
        """``(a, b) = ...`` is a ``pattern_assignment``, a node type never passed in,
        and the two variables carry no annotation of their own.
        """
        source = """void f() {
  var a;
  var b;
  (a, b) = (Repo(), Other());
}
"""
        assert _all_assignments(source) == {}
