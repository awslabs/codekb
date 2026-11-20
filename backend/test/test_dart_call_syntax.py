"""Tests for three Dart call shapes that yielded no edge, or the wrong one.

A bare simple name is a registered key for every component in the space, so
``(x as Repo).find()`` extracting the bare ``find`` landed on some unrelated
``find`` — a wrong edge, which this feature ranks as worse than a missing one.
Null-aware access ``r?.find()`` and cascade notation ``r..save()..clear()``
yielded no call node at all, so every call written in a cascade was missing,
and cascades are pervasive in Flutter. Each wrong-edge test keeps a decoy
component carrying the same bare member name, so it cannot pass by default.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
from src.flow.dart_flow_support import _NO_INFORMATION_TYPES, _simple_type_name
from src.flow.flow_analyzer import FlowAnalyzer
from src.parsers import DartParser
from src.parsers.dart_parser import _NO_INFORMATION_CAST_TYPES

pytestmark = pytest.mark.skipif(not DART_AVAILABLE, reason="tree_sitter_dart not installed")


# ── harness ───────────────────────────────────────────────────────────────────


def _analyze(tmp_path: Path, files: Dict[str, str]) -> Dict[str, List[str]]:
    """Write a Dart tree and build one real call graph over all of it."""
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
    """Every component the named caller reaches, with the space prefix stripped."""
    for component_id, callees in call_graph.items():
        if component_id.split("::", 1)[-1] == caller:
            return sorted(callee.split("::", 1)[-1] for callee in callees)
    return []


def _call_names(source: str) -> List[str]:
    """Every call name the Dart parser extracts from a snippet, in document order.

    Mirrors the shared walker, handing :meth:`DartParser.extract_call_name` the
    same node the walker would hand it.
    """
    parser = DartParser()
    call_node_types = parser.get_call_node_types()
    root = Parser(Language(tsdart.language())).parse(source.encode()).root_node
    found: List[Tuple[int, int, str]] = []

    def visit(node: Any) -> None:
        if node.type in call_node_types:
            if node.type == "new_expression" and len(node.children) >= 2:
                handed: Optional[Any] = node.children[1]
            else:
                handed = node.children[0] if node.children else node
            if handed is not None:
                name = parser.extract_call_name(handed)
                if name:
                    found.append((node.start_byte, node.end_byte, name))
        for child in node.children:
            visit(child)

    visit(root)
    return [name for _start, _end, name in sorted(found)]


# A decoy class declaring the same bare member names as the class under test.
# Nothing imports it; it exists so a bare-name resolution has somewhere wrong
# to land, which is what makes the wrong-edge assertions mean anything.
_DECOY = """
class Decoy {
  void find() {}
  void save() {}
  void clear() {}
  void notify() {}
}
"""


class TestACastReceiverIsAttributedToItsAssertedType:
    """``(x as Repo).find()`` — the wrong-edge shape."""

    def test_the_call_name_is_the_asserted_type_not_a_bare_name(self):
        assert _call_names("void f(Object x) { (x as Repo).find(); }") == ["Repo.find"]

    def test_a_cast_receiver_resolves_to_its_own_type_past_a_decoy(self, tmp_path):
        """The decoy is what makes this a wrong-edge test rather than a lookup test.

        The bare ``find`` matches a key every component in the space registers, so
        with ``Decoy.find`` present and declared first it resolved there.
        """
        call_graph = _analyze(
            tmp_path,
            {
                "a_decoy.dart": _DECOY,
                "repo.dart": "class Repo { void find() {} }\n"
                "void useCast(Object x) { (x as Repo).find(); }\n",
            },
        )
        assert _targets(call_graph, "repo.useCast") == ["repo.Repo.find"]

    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("Repo", "Repo.find"),
            ("Repo?", "Repo.find"),
            ("List<Repo>", "List.find"),
            ("prefix.Repo", "Repo.find"),
        ],
    )
    def test_the_asserted_type_is_reduced_the_way_an_annotation_is(self, written, expected):
        """The three reductions a written annotation gets: ``?``, generics, prefix."""
        assert _call_names(f"void f(Object x) {{ (x as {written}).find(); }}") == [expected]

    @pytest.mark.parametrize("written", ["dynamic", "Object", "T", "T1", "(int, int)"])
    def test_a_cast_carrying_no_usable_type_yields_no_call_at_all(self, written):
        """A cast is the one receiver shape that states what the receiver is, so where
        its asserted type carries no information, falling back to the space-wide bare
        name would reinstate the wrong edge. No call name at all is the standing choice.
        """
        assert _call_names(f"void f(Object x) {{ (x as {written}).find(); }}") == []

    def test_a_cast_receiver_does_not_resolve_to_a_decoy_when_its_own_type_is_absent(
        self, tmp_path
    ):
        """An unindexed cast type produces no edge, not a bare-name guess."""
        call_graph = _analyze(
            tmp_path,
            {
                "a_decoy.dart": _DECOY,
                "user.dart": "void useCast(Object x) { (x as Absent).find(); }\n",
            },
        )
        assert _targets(call_graph, "user.useCast") == []

    def test_a_parenthesized_receiver_that_is_not_a_cast_keeps_its_bare_name(self):
        """Unchanged on purpose: ``(a + b).toString()`` and ``f().bar()`` assert no type,
        so they carry nothing to replace the bare name with. This pins that the cast
        fix did not quietly take them with it.
        """
        assert _call_names("void f() { (a + b).find(); }") == ["find"]
        assert _call_names("void f() { (g()).find(); }") == ["g", "find"]

    def test_a_cast_inside_a_cascade_is_attributed_the_same_way(self):
        assert _call_names("void f(Object x) { (x as Repo)..save(); }") == ["Repo.save"]


class TestNullAwareCallsProduceTheSameEdgeAsPlainOnes:
    """``r?.find()`` — a missing-edge shape."""

    def test_the_two_spellings_extract_the_same_call_name(self):
        assert _call_names("void f(Repo? r) { r?.find(); }") == ["r.find"]
        assert _call_names("void f(Repo r) { r.find(); }") == ["r.find"]

    def test_a_null_aware_call_resolves_to_the_same_component(self, tmp_path):
        call_graph = _analyze(
            tmp_path,
            {
                "a_decoy.dart": _DECOY,
                "repo.dart": "class Repo { void find() {} }\n"
                "void nullAware(Repo? r) { r?.find(); }\n"
                "void plain(Repo r) { r.find(); }\n",
            },
        )
        assert _targets(call_graph, "repo.nullAware") == ["repo.Repo.find"]
        assert _targets(call_graph, "repo.nullAware") == _targets(call_graph, "repo.plain")

    def test_a_null_aware_property_read_is_not_counted_as_a_call(self):
        """``r?.name`` on its own invokes nothing, so it must add no call name."""
        assert _call_names("void f(Repo? r) { var n = r?.name; }") == []

    def test_one_call_name_per_null_aware_invocation_and_no_double_count(self):
        assert _call_names("void f(Repo? r) { r?.find(); r?.save(); }") == ["r.find", "r.save"]

    def test_a_chained_null_aware_call_keeps_the_bare_name_a_chain_already_had(self):
        """``r?.a?.find()`` matches plain ``r.a.find()``: the receiver is a property."""
        assert _call_names("void f(Repo? r) { r?.a?.find(); }") == ["find"]
        assert _call_names("void f(Repo r) { r.a.find(); }") == ["find"]


class TestCascadeSectionsProduceOneEdgeEach:
    """``r..save()..clear()`` — the other missing-edge shape."""

    def test_every_call_in_a_cascade_is_extracted_and_attributed_to_the_target(self):
        assert _call_names("void f(Repo r) { r..save()..clear(); }") == ["r.save", "r.clear"]

    def test_a_null_aware_cascade_is_read_the_same_way(self):
        assert _call_names("void f(Repo? r) { r?..save(); }") == ["r.save"]

    def test_a_cascade_resolves_to_the_targets_own_class_past_a_decoy(self, tmp_path):
        call_graph = _analyze(
            tmp_path,
            {
                "a_decoy.dart": _DECOY,
                "repo.dart": "class Repo { void save() {} void clear() {} }\n"
                "void useCascade(Repo r) { r..save()..clear(); }\n",
            },
        )
        assert _targets(call_graph, "repo.useCascade") == ["repo.Repo.clear", "repo.Repo.save"]

    def test_a_cascade_on_this_is_attributed_to_this(self):
        assert _call_names("class C { void f() { this..save(); } }") == ["this.save"]

    def test_a_cascade_with_arguments_still_yields_one_call_per_section(self):
        assert _call_names("void f(Repo r) { r..save(1, k: 2)..clear(); }") == [
            "r.save",
            "r.clear",
        ]

    def test_a_nested_selector_in_a_section_matches_the_non_cascade_form(self):
        """The invoked member belongs to the property ``a``, not to the cascade target,
        so attributing it to ``r`` would be a wrong edge. The cascade path returns the
        same bare name the non-cascade path already returns.
        """
        assert _call_names("void f(Repo r) { r..a.b(); }") == ["b"]
        assert _call_names("void f(Repo r) { r.a.b(); }") == ["b"]

    @pytest.mark.parametrize(
        "statement",
        [
            "r..enabled = true;",
            "r..enabled = true..visible = false;",
            "r..a.b = c;",
            "r..enabled = true..save();",
        ],
    )
    def test_a_cascade_section_that_assigns_adds_no_call(self, statement):
        """``..enabled = true`` writes a property; it invokes nothing, and Flutter writes
        a great many cascade setters. The argument part is the marker that decides a
        call happened; the last case mixes both and must yield the call alone.
        """
        expected = ["r.save"] if "save()" in statement else []
        assert _call_names(f"void f(Repo r) {{ {statement} }}") == expected

    def test_a_cascade_over_a_construction_binds_the_variable_and_resolves_later_calls(
        self, tmp_path
    ):
        """A cascade evaluates to its TARGET, not to its last section, so ``var a =
        Repo()..save();`` makes ``a`` a ``Repo`` by the language's own rule. Without the
        binding the later ``a.find()`` resolved by bare name and could land on the decoy.
        """
        call_graph = _analyze(
            tmp_path,
            {
                "a_decoy.dart": _DECOY,
                "repo.dart": "class Repo { void save() {} void find() {} }\n"
                "void useBind() {\n"
                "  var a = Repo()..save();\n"
                "  a.find();\n"
                "}\n",
            },
        )
        assert "repo.Repo.find" in _targets(call_graph, "repo.useBind")
        assert "a_decoy.Decoy.find" not in _targets(call_graph, "repo.useBind")


class TestTheTwoTypeReductionsAgree:
    """Two modules hold the same no-information type set and must not drift apart.

    ``dart_parser`` cannot import ``dart_flow_support`` — the parser layer does not
    depend on the flow layer — so the duplicated reduction is deliberate.
    """

    def test_the_no_information_sets_are_identical(self):
        assert _NO_INFORMATION_CAST_TYPES == _NO_INFORMATION_TYPES

    @pytest.mark.parametrize(
        "written",
        [
            "Repo",
            "Repo?",
            "Repo??",
            "List<Repo>",
            "Map<String, Repo>",
            "prefix.Repo",
            "prefix.Repo?",
            "_Repo",
            r"$Repo",
            "T",
            "E",
            "T1",
            "dynamic",
            "Object",
            "void",
            "Never",
            "Function",
            "unknown",
            "(int, int)",
            "",
        ],
    )
    def test_both_reductions_return_the_same_answer(self, written):
        assert DartParser._simple_cast_type_name(written) == _simple_type_name(written)
