"""Where the PyPI Dart grammar lags the language, pinned as current behaviour.

`tree-sitter-dart==0.1.0` is the only release on PyPI and predates Dart 3.13
declaring constructors, so it cannot parse them. That costs the declaration
itself, and the loss is silent — nothing in the output says a class is missing.

What it must never cost is a WRONG name. A recovered tree offers a keyword where
the class name belongs, and taking it would publish a component called `const`.
The name-trust check refuses that, so the failure stays "absent" rather than
becoming "absent and impersonated".

These expectations describe a limitation, not a desired outcome. A grammar
release that fixes declaring constructors will fail this file, which is the
point: the failure is the signal to update it.
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

from src.core.models import Component, ParseDiagnostic, IdScheme
from src.parsers.dart_parser import DartParser

pytestmark = pytest.mark.skipif(not DART_AVAILABLE, reason="tree-sitter-dart not installed")

FIXTURE = Path(__file__).parent / "fixtures" / "parser_test_dart_grammar_upgrade.dart"


# ── harness ───────────────────────────────────────────────────────────────────


def _parse(source: bytes) -> Any:
    return Parser(Language(tsdart.language())).parse(source).root_node


def _defect_counts(node: Any) -> tuple[int, int]:
    """Whole-tree ERROR and MISSING node counts, as the product's reports do."""
    errors = missing = 0
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "ERROR":
            errors += 1
        if current.is_missing:
            missing += 1
        stack.extend(current.children)
    return errors, missing


def _walk(node: Any) -> list[Any]:
    """Every node of the subtree, so a test can select the ones it cares about."""
    seen = []
    stack = [node]
    while stack:
        current = stack.pop()
        seen.append(current)
        stack.extend(current.children)
    return seen


def _extract(path: Path) -> tuple[List[Component], List[ParseDiagnostic]]:
    source = path.read_bytes()
    dart = DartParser()
    dart._scheme = IdScheme.V1_STEM
    components = dart.extract_components(_parse(source), source, path)
    return components, dart.drain_parse_diagnostics()


@pytest.fixture(scope="module")
def parsed() -> Dict[str, Any]:
    components, refusals = _extract(FIXTURE)
    return {
        "components": {c.id: c for c in components},
        "stem": FIXTURE.stem,
        "refusals": refusals,
    }


# ── which grammar is loaded ───────────────────────────────────────────────────


def test_the_grammar_is_the_pypi_distribution():
    """No vendored copy: the grammar comes from the installed wheel."""
    assert tsdart.__name__ == "tree_sitter_dart"
    assert "site-packages" in tsdart.__file__.replace("\\", "/")


# ── the lag, stated exactly ───────────────────────────────────────────────────


def test_a_declaring_constructor_does_not_parse():
    source = b"class RoundedRectComponent() extends PositionComponent {}\n"
    errors, _missing = _defect_counts(_parse(source))

    assert errors, "a declaring constructor is expected to produce an ERROR node"


@pytest.mark.parametrize(
    ("declaration", "member"),
    [
        # Verbatim from flame; the fixture records file and line for each.
        ("RoundedRectComponent", "render"),  # plain, with superclass + mixin
        ("ExampleApp", "build"),  # `const`, required field param, super.key
        ("PathFinderData", "recompute"),  # `const` + named constructor `._`
        ("Hitbox", "onCollision"),  # declaring ctor after type parameters
    ],
)
def test_a_declaring_constructor_class_is_absent_not_misnamed(parsed, declaration, member):
    """The class is lost. What matters is that nothing takes its name."""
    stem = parsed["stem"]

    assert f"{stem}.{declaration}" not in parsed["components"]
    assert f"{stem}.{declaration}.{member}" not in parsed["components"]


def test_no_declaration_is_ever_named_after_a_keyword(parsed):
    """The guarantee that makes this limitation tolerable.

    `class const ExampleApp({...})` recovers with `const` where the name belongs.
    Publishing that would lose `ExampleApp` AND invent a component called
    `const`, which a search would then surface as if it were real.
    """
    keyword_named = [
        identity
        for identity in parsed["components"]
        if identity.rsplit(".", 1)[-1] in {"const", "final", "class", "var", "late"}
    ]

    assert keyword_named == []


def test_the_refusals_are_recorded_rather_than_silent(parsed):
    """A refused declaration leaves a diagnostic, so the loss is reportable."""
    assert parsed["refusals"], "a refused name should produce a parse diagnostic"


def test_an_enum_declaring_constructor_still_survives(parsed):
    """The one form this grammar reads: `enum GameColors()` keeps its values."""
    stem = parsed["stem"]
    identity = f"{stem}.GameColors"

    assert identity in parsed["components"]
    assert parsed["components"][identity].type.value == "enum"


def test_a_members_owner_can_be_lost_while_the_member_survives(parsed):
    """The artifact worth knowing: a member can be reparented to file level.

    When the class header does not parse, a member inside it is no longer nested
    under anything, so it is indexed as a top-level function. It is reachable by
    name but attributed to no class.
    """
    stem = parsed["stem"]
    hoisted = [f"{stem}.render", f"{stem}.onCollision"]

    assert any(identity in parsed["components"] for identity in hoisted)


# ── blast radius ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        b"class Plain { Plain(); void ok() {} }\n",
        b"class Named { Named.from(int v); void ok() {} }\n",
        b"class Const { const Const(); }\n",
        b"class Factory { factory Factory() => Factory._(); Factory._(); }\n",
    ],
)
def test_the_ordinary_constructor_forms_are_unaffected(source):
    errors, missing = _defect_counts(_parse(source))

    assert (errors, missing) == (0, 0)


def test_a_class_beside_an_unparseable_one_is_still_indexed():
    """The loss is bounded to the declaration that does not parse."""
    source = b"class Broken() extends P {}\nclass Fine {\n  void ok() {}\n}\n"
    dart = DartParser()
    dart._scheme = IdScheme.V1_STEM
    components = dart.extract_components(_parse(source), source, Path("probe.dart"))
    names = {c.id for c in components}

    assert "probe.Fine" in names
    assert "probe.Fine.ok" in names


def test_a_generic_named_constructor_call_yields_no_edge():
    """`Box<int>.named(1)` resolves to nothing, on this grammar as on others.

    Recorded so the absence is not mistaken for something this grammar choice
    caused: the shape produced no edge before the switch either.
    """
    source = (
        b"class Box<T> {\n"
        b"  Box.named(T v) {}\n"
        b"}\n"
        b"class Use {\n"
        b"  void go() {\n"
        b"    var b = Box<int>.named(1);\n"
        b"  }\n"
        b"}\n"
    )
    dart = DartParser()
    dart._scheme = IdScheme.V1_STEM
    root = _parse(source)
    components = dart.extract_components(root, source, Path("probe.dart"))
    names = {c.id for c in components}

    # The declarations themselves are read correctly.
    assert "probe.Box.named" in names
    assert "probe.Use.go" in names

    # But the call site resolves to no callee, so no edge reaches Box.named.
    call_nodes = [
        node
        for node in _walk(root)
        if node.type in dart.get_call_node_types() and b"named" in node.text
    ]
    resolved = [name for name in (dart.extract_call_name(n) for n in call_nodes) if name]

    assert "Box.named" not in resolved


def test_this_fixture_is_separate_from_the_frozen_one(parsed):
    """Nothing here may reach `parser_test_dart.dart`, whose lines are pinned."""
    assert parsed["stem"] == "parser_test_dart_grammar_upgrade"
