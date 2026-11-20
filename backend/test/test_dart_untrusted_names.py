"""Tests for the silent-misnaming guard: a declaration we cannot name is skipped.

The grammar recovers from syntax it cannot parse by producing a tree anyway, and
a recovered tree can offer a KEYWORD where a declared name belongs.
`class const Foo({...})` parses with `const` as the class name and `Foo`
stranded in a sibling ERROR node, publishing a component named `const`.

The check is positional: a reserved word is refused in any declared-name
position, a built-in identifier only in a declared TYPE position, because `get`,
`set` and `export` are ordinary Dart method names.
"""

from pathlib import Path
from typing import Any, Dict, List

import pytest

try:  # pragma: no cover - import guard, mirrors test_parser_integration.py
    import tree_sitter_dart as tsdart

    DART_AVAILABLE = True
except ImportError:  # pragma: no cover
    tsdart = None
    DART_AVAILABLE = False

from tree_sitter import Language, Parser

from src.analysis.code_analyzer import collect_parse_failure_metadata
from src.core.models import (
    Component,
    ParseDiagnostic,
    ParseDiagnosticKind,
    ParseFailureReport,
    ParseResult,
    IdScheme,
)
from src.parsers.dart_parser import (
    _DART_BUILT_IN_IDENTIFIERS,
    _DART_RESERVED_WORDS,
    _UNTRUSTWORTHY_NAMES_ANYWHERE,
    _UNTRUSTWORTHY_TYPE_NAMES,
    DartParser,
)

pytestmark = pytest.mark.skipif(not DART_AVAILABLE, reason="tree_sitter_dart not installed")

FIXTURES_DIR = Path(__file__).parent / "fixtures"
# Declaring constructors: the syntax that motivated the guard. The shipped
# grammar cannot parse them, so this fixture refuses names rather than parsing
# cleanly — `test_declaring_constructors_are_refused_not_guessed` asserts that.
DECLARING_CTOR_FIXTURE = FIXTURES_DIR / "parser_test_dart_declaring_constructors.dart"
# Declarations this grammar still mis-names, so the guard keeps a live input.
DEFECTIVE_FIXTURE = FIXTURES_DIR / "parser_test_dart_unparseable.dart"
CLEAN_FIXTURE = FIXTURES_DIR / "parser_test_dart.dart"


# ── harness ───────────────────────────────────────────────────────────────────


def _walk(node: Any) -> Any:
    """Yield a node and every descendant, for tests that need a specific one."""
    yield node
    for child in node.children:
        yield from _walk(child)


def _extract(file_path: Path) -> tuple[List[Component], List[ParseDiagnostic]]:
    """Parse a file and return its components beside the refusals it recorded."""
    parser = Parser(Language(tsdart.language()))
    source = file_path.read_bytes()
    dart = DartParser()
    dart._scheme = IdScheme.V1_STEM
    components = dart.extract_components(parser.parse(source).root_node, source, file_path)
    return components, dart.drain_parse_diagnostics()


def _extract_source(source: str, name: str) -> tuple[List[Component], List[ParseDiagnostic]]:
    """Extract from a snippet, naming the file so identities read the way they would."""
    parser = Parser(Language(tsdart.language()))
    encoded = source.encode()
    dart = DartParser()
    dart._scheme = IdScheme.V1_STEM
    path = Path(f"{name}.dart")
    components = dart.extract_components(parser.parse(encoded).root_node, encoded, path)
    return components, dart.drain_parse_diagnostics()


def _summarise(path: Path) -> Dict[str, Any]:
    components, refusals = _extract(path)
    return {"ids": [c.id for c in components], "stem": path.stem, "refusals": refusals}


@pytest.fixture(scope="module")
def defective() -> Dict[str, Any]:
    """A file this grammar still mis-names, so the refusal path has an input."""
    return _summarise(DEFECTIVE_FIXTURE)


@pytest.fixture(scope="module")
def declaring_ctors() -> Dict[str, Any]:
    """The declaring-constructor file, which this grammar reads correctly."""
    return _summarise(DECLARING_CTOR_FIXTURE)


# ── the defect itself ─────────────────────────────────────────────────────────


def test_declaring_constructors_are_refused_not_guessed(declaring_ctors):
    """`class const InventoryEvent({...})` does not parse on this grammar.

    The recovered tree puts `const` where the name belongs. The declaration is
    therefore lost — but it must be lost cleanly, with nothing published under a
    name nobody wrote.
    """
    stem = declaring_ctors["stem"]
    assert f"{stem}.const" not in declaring_ctors["ids"]

    # Not one identity anywhere in the file is named after a reserved word, at
    # any position: an identity is a dotted path, and a keyword in the middle of
    # one files real behaviour under a name nobody wrote.
    offenders = [
        identity
        for identity in declaring_ctors["ids"]
        if set(identity.split(".")) & _DART_RESERVED_WORDS
    ]
    assert offenders == []

    # The declarations this grammar cannot reach. Absent, never impersonated.
    for lost in (
        f"{stem}.InventoryEvent",
        f"{stem}.PathFinderData",
    ):
        assert lost not in declaring_ctors["ids"]

    # The loss is reportable rather than silent.
    assert declaring_ctors["refusals"]


def test_a_refused_declaration_takes_its_members_with_it(defective):
    """A member's identity is qualified by its owner, so it is no more trustworthy."""
    ids = defective["ids"]
    stem = defective["stem"]

    assert f"{stem}.sealed" not in ids
    assert f"{stem}.sealed.ok" not in ids
    assert f"{stem}.interface.run" not in ids
    # Nor is the member promoted to a top-level function to save it.
    assert f"{stem}.ok" not in ids
    assert f"{stem}.run" not in ids


def test_the_refusal_is_visible_in_the_files_parse_diagnostics(defective):
    """A skipped declaration is attributable, so it cannot read as an empty file."""
    refusals = defective["refusals"]

    assert len(refusals) == 2, "both mis-named declarations are refused"
    assert {r.kind for r in refusals} == {ParseDiagnosticKind.UNTRUSTED_NAME}
    assert [r.line for r in refusals] == [27, 32]
    assert [r.file_path for r in refusals] == [DEFECTIVE_FIXTURE] * 2
    assert "keyword `sealed`" in refusals[0].detail
    assert "keyword `interface`" in refusals[1].detail


def test_the_refusal_reaches_the_per_file_report_and_the_runs_metadata():
    """The count travels with the grammar's own three, and earns the file a row."""
    components, refusals = _extract(DEFECTIVE_FIXTURE)
    assert components  # the file is not emptied by the guard

    result = ParseResult(
        file_path=DEFECTIVE_FIXTURE,
        language="dart",
        components=components,
        diagnostics=list(refusals),
    )
    report = result.failure_report()

    assert report.untrusted_name_count == 2
    # The grammar's own counts are not touched by a refusal: this result carries
    # only refusals, and both error counts stay zero.
    assert report.error_node_count == 0
    assert report.missing_node_count == 0
    assert report.parse_error_count == 0
    # A file whose only defect is a refusal is not clean, which is what earns it
    # a per-file row in the run's metadata.
    assert report.is_structurally_clean() is False

    metadata = collect_parse_failure_metadata([report])
    rows = metadata["parse_failure_reports"]
    assert [row["file_path"] for row in rows] == [str(DEFECTIVE_FIXTURE)]
    assert rows[0]["untrusted_name_count"] == 2
    assert metadata["parse_failure_summary"]["files_with_defects"] == 1


def test_a_refusal_free_file_is_counted_clean_exactly_as_before():
    """The fourth count never invents a defect: zero refusals reads as clean."""
    report = ParseFailureReport(Path("fine.dart"), "dart")

    assert report.untrusted_name_count == 0
    assert report.is_structurally_clean() is True
    assert ParseFailureReport.from_dict(report.to_dict()) == report


def test_a_refusal_carries_its_reason_through_serialization(defective):
    """The reason is part of the record, so a reader is not left with a count."""
    refusal = defective["refusals"][0]

    data = refusal.to_dict()
    assert data["kind"] == "untrusted_name"
    assert "keyword `sealed`" in data["detail"]
    assert ParseDiagnostic.from_dict(data) == refusal

    # The grammar's own diagnostics carry no reason, and serialize exactly as
    # they did before the field existed.
    plain = ParseDiagnostic(Path("x.dart"), ParseDiagnosticKind.ERROR, 1, 0, 0)
    assert "detail" not in plain.to_dict()
    assert ParseDiagnostic.from_dict(plain.to_dict()) == plain


def test_a_declaration_with_no_name_node_is_skipped_without_a_refusal():
    """Absent is not untrustworthy: nothing was read, so there is nothing to report."""
    parser = Parser(Language(tsdart.language()))
    source = b"enum Suit { hearts }\n"
    root = parser.parse(source).root_node
    enum_node = next(c for c in root.children if c.type == "enum_declaration")
    body = next(c for c in enum_node.children if c.type == "enum_body")

    dart = DartParser()
    dart._scheme = IdScheme.V1_STEM
    assert dart._declared_name(body, Path("f.dart")) is None
    assert dart.drain_parse_diagnostics() == []


# ── the forms that are correctly named, and must keep their components ────────


def test_declaring_constructor_without_a_modifier_keeps_its_real_name(declaring_ctors):
    """`class Waypoint()` and its member, named correctly.

    The pinned grammar left a MISSING node in a sibling subtree here: damaged
    declaration, undamaged name. Distrusting the whole declaration would cost it.
    """
    ids = declaring_ctors["ids"]
    stem = declaring_ctors["stem"]

    assert f"{stem}.Waypoint" in ids
    assert f"{stem}.Waypoint.advance" in ids


def test_enum_with_a_declaring_constructor_keeps_its_name_and_values(declaring_ctors):
    """`enum Suit()` likewise: damaged declaration before, clean parse now."""
    ids = declaring_ctors["ids"]
    stem = declaring_ctors["stem"]

    assert f"{stem}.Suit" in ids
    assert f"{stem}.Suit.hearts" in ids
    assert f"{stem}.Suit.spades" in ids


def test_identifiers_that_contain_or_resemble_a_keyword_still_produce_components(declaring_ctors):
    """Dart permits `constValue`, `Constants`, `finalise` and a private `_const`."""
    ids = declaring_ctors["ids"]
    stem = declaring_ctors["stem"]

    for expected in (
        f"{stem}.constValue",
        f"{stem}.finalise",
        f"{stem}._const",
        f"{stem}.Constants",
        f"{stem}.Constants.describe",
        f"{stem}._Const",
        f"{stem}._Const.run",
        f"{stem}.Mapper",
    ):
        assert expected in ids, f"{expected} was lost to an over-broad keyword check"


def test_built_in_identifiers_are_still_legal_member_names(declaring_ctors):
    """`get`, `set` and `export` are built-in identifiers.

    Dart bars them from a declared type name and allows them as member names, so
    an over-broad keyword check would delete real components.
    """
    ids = declaring_ctors["ids"]
    stem = declaring_ctors["stem"]

    assert f"{stem}.KeyValueStore.get" in ids
    assert f"{stem}.KeyValueStore.set" in ids
    assert f"{stem}.KeyValueStore.export" in ids
    # Including at top level, where it is a function rather than a method.
    assert f"{stem}.export" in ids


def test_an_unnamed_constructor_is_still_named_new():
    """`new` is a reserved word, and the guard tests the DECLARED name, not that."""
    components, refusals = _extract_source(
        "class Session {\n  Session();\n  Session.restored(String id);\n}\n", "session"
    )
    ids = [c.id for c in components]

    assert "session.Session.new" in ids
    assert "session.Session.restored" in ids
    assert refusals == []


# ── the other half of the contract: a name read out of a broken subtree ───────


def test_a_name_stranded_inside_an_error_node_is_refused_not_dug_out():
    """`class sealed Data {...}` strands the real class name inside an ERROR node.

    Digging it out is the same defect wearing a better name. Only a direct node
    test reaches the rule: a declaration inside an ERROR node is never dispatched.
    """
    parser = Parser(Language(tsdart.language()))
    source = b"class sealed Data { void run() {} }\n"
    root = parser.parse(source).root_node

    def find_stranded(node: Any) -> Any:
        if node.type == "identifier" and node.text == b"Data" and node.parent.type == "ERROR":
            return node
        for child in node.children:
            found = find_stranded(child)
            if found is not None:
                return found
        return None

    stranded = find_stranded(root)
    assert stranded is not None, "the real name is in the tree, inside the ERROR node"

    dart = DartParser()
    dart._scheme = IdScheme.V1_STEM
    assert dart._trusted_name(stranded, Path("f.dart")) is None
    refusals = dart.drain_parse_diagnostics()
    assert len(refusals) == 1
    assert "sits inside a ERROR node" in refusals[0].detail


def test_the_file_root_being_an_error_node_refuses_nothing_by_itself():
    """A file that did not parse is reported by its error count, not by deletion.

    A badly damaged file gets an ERROR node for its whole root. Testing the root
    would refuse every declaration in it, so `_broken_ancestor` stops before it.
    """
    parser = Parser(Language(tsdart.language()))
    source = b"class Router {\n  void push(String route) {\n"  # deliberately unterminated
    root = parser.parse(source).root_node
    assert root.is_error, "this input is the file-root ERROR case"

    names = {
        node.text.decode(): node
        for node in _walk(root)
        if node.type == "identifier" and node.text in (b"Router", b"push")
    }
    assert set(names) == {"Router", "push"}, "both names are in the ERROR-rooted tree"
    # `Router` is a DIRECT child of the ERROR root: the strongest form of the case.
    assert names["Router"].parent.type == "ERROR"

    dart = DartParser()
    dart._scheme = IdScheme.V1_STEM
    for text, node in names.items():
        assert dart._broken_ancestor(node) is None, "the file root is not a broken ancestor"
        assert dart._trusted_name(node, Path("router.dart")) == text
    assert dart.drain_parse_diagnostics() == [], "the file root alone refuses nothing"


def test_a_class_with_one_broken_member_keeps_its_name():
    """The ancestor walk is deliberately narrow: only the name node's own chain."""
    components, _ = _extract_source(
        "class Holder<T extends {\n  void ok() {}\n}\nint later = 2;\n", "holder"
    )
    ids = [c.id for c in components]

    assert "holder.Holder" in ids
    assert "holder.later" in ids


# ── the closed set ────────────────────────────────────────────────────────────


def test_the_keyword_set_is_positional_in_the_way_dart_is():
    """One closed set, projected the way Dart's own grammar applies it."""
    # A reserved word is never a declared name anywhere.
    for word in ("const", "class", "var", "void", "enum", "final", "extends", "super"):
        assert word in _UNTRUSTWORTHY_NAMES_ANYWHERE
        assert word in _UNTRUSTWORTHY_TYPE_NAMES

    # A built-in identifier is barred from a declared type name only.
    for word in ("get", "set", "export", "static", "late", "required", "abstract", "typedef"):
        assert word in _UNTRUSTWORTHY_TYPE_NAMES
        assert word not in _UNTRUSTWORTHY_NAMES_ANYWHERE

    # The Dart 3 class modifiers behave the same way.
    for word in ("sealed", "base", "interface", "mixin"):
        assert word in _UNTRUSTWORTHY_TYPE_NAMES

    # Dart's contextual keywords are in neither: they are valid identifiers
    # everywhere but their own clause, and `of` names a static method on half
    # of Flutter.
    for word in ("of", "on", "show", "hide", "async", "sync"):
        assert word not in _UNTRUSTWORTHY_TYPE_NAMES
        assert word not in _UNTRUSTWORTHY_NAMES_ANYWHERE

    assert _DART_RESERVED_WORDS.isdisjoint(_DART_BUILT_IN_IDENTIFIERS)


def test_a_keyword_in_a_type_name_position_is_refused():
    """`class sealed Thing` — the modifier lands where the name belongs."""
    components, refusals = _extract_source("class sealed Thing {\n  void ok() {}\n}\n", "mod")

    assert [c.id for c in components] == []
    assert "keyword `sealed`" in refusals[0].detail


def test_the_guard_is_not_specific_to_declaring_constructors():
    """Dart 3.7 augmentation syntax is a second construct this grammar cannot parse.

    It recovers the same way, with `class` where the name belongs, so the guard is
    written against the shape of the damage rather than against one syntax.
    """
    components, refusals = _extract_source("augment class Widget { void ok() {} }\n", "aug")

    assert [c.id for c in components] == []
    assert len(refusals) == 1
    assert "keyword `class`" in refusals[0].detail


# ── the carrier ───────────────────────────────────────────────────────────────


def test_refusals_are_drained_once_and_never_attributed_to_the_next_file():
    """One parser instance extracts many files; a refusal belongs to exactly one."""
    parser = Parser(Language(tsdart.language()))
    dart = DartParser()
    dart._scheme = IdScheme.V1_STEM

    defective_source = DEFECTIVE_FIXTURE.read_bytes()
    dart.extract_components(
        parser.parse(defective_source).root_node, defective_source, DEFECTIVE_FIXTURE
    )
    first = dart.drain_parse_diagnostics()
    assert len(first) == 2
    # Drained means cleared: a second read of the same extraction reports nothing.
    assert dart.drain_parse_diagnostics() == []

    clean_source = CLEAN_FIXTURE.read_bytes()
    dart.extract_components(parser.parse(clean_source).root_node, clean_source, CLEAN_FIXTURE)
    assert dart.drain_parse_diagnostics() == []


def test_a_parser_that_records_nothing_still_answers_the_drain():
    """The hook is language-neutral, so the shared path needs no per-language branch."""
    from src.parsers import PythonParser

    assert PythonParser().drain_parse_diagnostics() == []


# ── the clean file is untouched ───────────────────────────────────────────────


def test_clean_fixture_identities_are_unaffected():
    """Concrete identities from the frozen fixture, not a count.

    A count would pass while a name moved.
    """
    components, refusals = _extract(CLEAN_FIXTURE)
    ids = set(c.id for c in components)

    assert refusals == [], "the clean fixture gives the guard nothing to refuse"

    for expected in (
        "parser_test_dart.Repository",
        "parser_test_dart.Repository.new",
        "parser_test_dart.Repository.seeded",
        "parser_test_dart.Repository.build",
        "parser_test_dart.Repository.version.get",
        "parser_test_dart.Repository.version.set",
        "parser_test_dart.Repository.operator-:0",
        "parser_test_dart.Repository.operator[]=:2",
        # `of` is a contextual keyword and Flutter's most common static method
        # name. A guard that barred contextual keywords would lose it.
        "parser_test_dart.Repository.of",
        "parser_test_dart.computeTotal",
        "parser_test_dart.Status",
        "parser_test_dart.Status.active",
        "parser_test_dart.Loggable",
        "parser_test_dart.Loggable.log",
        "parser_test_dart.extension$String.shout",
        "parser_test_dart.Transform",
        "parser_test_dart._internalCounter",
    ):
        assert expected in ids, f"{expected} moved or was lost"
