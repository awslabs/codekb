"""Unit tests for extract_typescript_field_types: which class fields are typed,
and which annotations are deliberately not reported."""

import pytest

try:  # pragma: no cover - import guard, mirrors the other TS/Dart modules
    import tree_sitter_typescript as tsts

    TS_AVAILABLE = True
except ImportError:  # pragma: no cover
    tsts = None
    TS_AVAILABLE = False

from tree_sitter import Language, Parser

from src.flow.typescript_flow_support import extract_typescript_field_types

pytestmark = pytest.mark.skipif(not TS_AVAILABLE, reason="tree_sitter_typescript not installed")


def _fields(source: str):
    src = source.encode()
    tree = Parser(Language(tsts.language_typescript())).parse(src)

    def get_text(node):
        return src[node.start_byte : node.end_byte].decode("utf-8")

    return extract_typescript_field_types(tree.root_node, get_text)


def test_constructor_parameter_property_is_a_typed_field():
    fields = _fields(
        "class CheckoutController {\n"
        "  constructor(private readonly checkoutService: CheckoutService) {}\n"
        "}\n"
    )
    assert ("CheckoutController", "checkoutService", "CheckoutService") in fields


def test_public_and_protected_parameter_properties_count():
    fields = _fields(
        "class C {\n"
        "  constructor(public a: AService, protected b: BService) {}\n"
        "}\n"
    )
    assert ("C", "a", "AService") in fields
    assert ("C", "b", "BService") in fields


def test_plain_constructor_parameter_is_not_a_field():
    # No accessibility/readonly modifier → an ordinary parameter, not a field.
    fields = _fields("class C {\n  constructor(id: string, svc: Svc) {}\n}\n")
    assert fields == []


def test_class_field_definition_is_reported():
    fields = _fields("class C {\n  private readonly svc: CheckoutService;\n}\n")
    assert fields == [("C", "svc", "CheckoutService")]


def test_primitive_and_generic_types_are_skipped():
    # `string` is a predefined_type and Promise<T> is a generic_type — neither
    # is a type_identifier naming a single component, so both are skipped.
    fields = _fields(
        "class C {\n"
        "  private name: string;\n"
        "  private pending: Promise<Checkout>;\n"
        "  constructor(readonly svc: CheckoutService) {}\n"
        "}\n"
    )
    assert fields == [("C", "svc", "CheckoutService")]


def test_field_declared_once_is_not_duplicated():
    fields = _fields("class C {\n  constructor(private readonly svc: Svc) {}\n}\n")
    assert fields.count(("C", "svc", "Svc")) == 1


def test_declaring_class_is_tracked_per_class():
    fields = _fields(
        "class A {\n  constructor(private x: XService) {}\n}\n"
        "class B {\n  constructor(private x: YService) {}\n}\n"
    )
    assert ("A", "x", "XService") in fields
    assert ("B", "x", "YService") in fields


def test_a_class_without_typed_fields_yields_nothing():
    fields = _fields("class C {\n  run() { return 1; }\n}\n")
    assert fields == []
