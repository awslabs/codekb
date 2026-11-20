"""TypeScript flow-support: the class-field type information the call-graph
resolver needs for a field receiver.

A method body routinely calls through a class field — ``this.svc.update()`` —
but the resolver only knows a receiver's type for *local* variables. A field's
type lives on the class, not in the method, so without this module the edge is
dropped and the component's trace flow comes back empty.

Two TypeScript forms declare a typed field:

  class C {
    private readonly svc: Svc;                 // field definition
    constructor(private readonly dep: Dep) {}  // constructor PARAMETER PROPERTY
  }

A constructor parameter becomes a field only when it carries an accessibility
modifier (``public``/``private``/``protected``) or ``readonly`` — a bare
parameter is just a parameter. Both forms are returned here as
``(declaring_class, field_name, type_name)`` triples so a receiver key can be
scoped to methods of the class that DECLARES the field (one file may declare
several classes). Only a simple class type (a ``type_identifier``) is reported;
``string`` and generics like ``Promise<Checkout>`` name no single component and
are deliberately skipped to keep resolution high-precision.

Plain JavaScript has no type annotations, so this returns nothing for ``.js``
files — that case (``this.x = new Foo()`` in the constructor) is a separate,
assignment-based mechanism.
"""

from typing import Any, Callable, List, Optional, Set, Tuple

NodeTextFn = Callable[[Any], str]

# tree-sitter-typescript node types.
_CLASS_NODE_TYPES = frozenset({"class_declaration", "class"})
_CLASS_BODY = "class_body"
_CLASS_NAME = "type_identifier"
_FIELD_DEFINITION = "public_field_definition"
_METHOD = "method_definition"
_PROPERTY_ID = "property_identifier"
_FORMAL_PARAMETERS = "formal_parameters"
_PARAMETER_NODE_TYPES = frozenset({"required_parameter", "optional_parameter"})
# Presence of either marks a constructor parameter as a field (parameter property).
_FIELD_MODIFIERS = frozenset({"accessibility_modifier", "readonly"})
_TYPE_ANNOTATION = "type_annotation"
_SIMPLE_TYPE = "type_identifier"


def extract_typescript_field_types(
    root_node: Any, get_node_text_fn: NodeTextFn
) -> List[Tuple[str, str, str]]:
    """Return ``(declaring_class, field_name, type_name)`` for every typed field.

    Covers both class field definitions and constructor parameter properties.
    Deduplicated on ``(class, field)`` so a field annotated once is not reported
    twice. Order follows document order.
    """
    results: List[Tuple[str, str, str]] = []
    seen: Set[Tuple[str, str]] = set()

    def record(class_name: str, field: Optional[str], type_name: Optional[str]) -> None:
        if not field or not type_name:
            return
        if (class_name, field) in seen:
            return
        seen.add((class_name, field))
        results.append((class_name, field, type_name))

    def visit_class(class_node: Any) -> None:
        class_name = _first_child_text(class_node, _CLASS_NAME, get_node_text_fn)
        body = _first_child_of_type(class_node, _CLASS_BODY)
        if not class_name or body is None:
            return
        for member in body.children:
            if member.type == _FIELD_DEFINITION:
                record(
                    class_name,
                    _first_child_text(member, _PROPERTY_ID, get_node_text_fn),
                    _annotation_type(member, get_node_text_fn),
                )
            elif member.type == _METHOD and _method_name(member, get_node_text_fn) == "constructor":
                params = _first_child_of_type(member, _FORMAL_PARAMETERS)
                if params is None:
                    continue
                for param in params.children:
                    if param.type not in _PARAMETER_NODE_TYPES:
                        continue
                    # A parameter declares a field only if it carries a modifier.
                    if not any(c.type in _FIELD_MODIFIERS for c in param.children):
                        continue
                    record(
                        class_name,
                        _first_child_text(param, "identifier", get_node_text_fn),
                        _annotation_type(param, get_node_text_fn),
                    )

    # Explicit stack rather than recursion: a deeply nested module could exhaust
    # the interpreter's stack and drop every field-receiver edge in the file.
    stack = [root_node]
    while stack:
        current = stack.pop()
        if current.type in _CLASS_NODE_TYPES:
            visit_class(current)
        stack.extend(reversed(current.children))
    return results


# ── internals ───────────────────────────────────────────────────────────────


def _text(node: Optional[Any], get_node_text_fn: NodeTextFn) -> Optional[str]:
    """Read one node's text, mapping the failure sentinel onto None."""
    if node is None:
        return None
    value = get_node_text_fn(node)
    if not value or value == "unknown":
        return None
    return value


def _first_child_of_type(node: Any, type_name: str) -> Optional[Any]:
    for child in node.children:
        if child.type == type_name:
            return child
    return None


def _first_child_text(
    node: Any, type_name: str, get_node_text_fn: NodeTextFn
) -> Optional[str]:
    return _text(_first_child_of_type(node, type_name), get_node_text_fn)


def _method_name(method_node: Any, get_node_text_fn: NodeTextFn) -> Optional[str]:
    return _first_child_text(method_node, _PROPERTY_ID, get_node_text_fn)


def _annotation_type(node: Any, get_node_text_fn: NodeTextFn) -> Optional[str]:
    """Return the simple class type named by the node's ``type_annotation``.

    None when the member has no annotation or the annotation is not a plain
    class name (``string``, a union, or a generic like ``Promise<Checkout>``) —
    those name no single component to route to.
    """
    annotation = _first_child_of_type(node, _TYPE_ANNOTATION)
    if annotation is None:
        return None
    return _first_child_text(annotation, _SIMPLE_TYPE, get_node_text_fn)
