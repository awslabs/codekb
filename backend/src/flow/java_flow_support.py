"""Java-specific flow analysis support methods."""

from typing import Callable, Dict, FrozenSet, List, Optional, Set, Tuple, Any

# tree-sitter-java node types for class-field type extraction.
_CLASS_NODE_TYPES = frozenset(
    {"class_declaration", "enum_declaration", "record_declaration"}
)
_CLASS_BODY_TYPES = frozenset({"class_body", "enum_body", "record_declaration_body"})
_FIELD_DECLARATION = "field_declaration"
_VARIABLE_DECLARATOR = "variable_declarator"
_SIMPLE_TYPE = "type_identifier"


def extract_java_parameter_types(
    node: Any, function_start_line: int, get_node_text_fn
) -> Dict[str, str]:
    """Extract parameter type annotations from Java method/constructor."""
    param_types = {}

    if (
        node.type in ["method_declaration", "constructor_declaration"]
        and node.start_point[0] == function_start_line
    ):
        for child in node.children:
            if child.type == "formal_parameters":
                for param_child in child.children:
                    if param_child.type == "formal_parameter":
                        param_name = None
                        type_name = None
                        for subchild in param_child.children:
                            if subchild.type in [
                                "type_identifier",
                                "integral_type",
                                "floating_point_type",
                                "boolean_type",
                            ]:
                                type_name = get_node_text_fn(subchild)
                            elif subchild.type == "variable_declarator":
                                for vchild in subchild.children:
                                    if vchild.type == "identifier":
                                        param_name = get_node_text_fn(vchild)
                            elif subchild.type == "identifier":
                                param_name = get_node_text_fn(subchild)
                        if param_name and type_name:
                            param_types[param_name] = type_name
        return param_types

    for child in node.children:
        param_types.update(
            extract_java_parameter_types(child, function_start_line, get_node_text_fn)
        )

    return param_types


def extract_java_assignment_type(
    node: Any, get_node_text_fn
) -> Tuple[Optional[str], Optional[str]]:
    """Extract (var_name, type_name) from Java local variable declaration."""
    var_name = None
    type_name = None

    for child in node.children:
        if child.type in [
            "type_identifier",
            "integral_type",
            "floating_point_type",
            "boolean_type",
        ]:
            type_name = get_node_text_fn(child)
        elif child.type == "variable_declarator":
            for subchild in child.children:
                if subchild.type == "identifier":
                    var_name = get_node_text_fn(subchild)
                elif subchild.type == "object_creation_expression":
                    for vchild in subchild.children:
                        if vchild.type == "type_identifier":
                            type_name = get_node_text_fn(vchild)

    return var_name, type_name


def get_java_parameter_names(node: Any, function_start_line: int, get_node_text_fn) -> List[str]:
    """Extract Java parameter names in order."""
    param_names = []

    if (
        node.type in ["method_declaration", "constructor_declaration"]
        and node.start_point[0] == function_start_line
    ):
        for child in node.children:
            if child.type == "formal_parameters":
                for param_child in child.children:
                    if param_child.type == "formal_parameter":
                        for subchild in param_child.children:
                            if subchild.type == "variable_declarator":
                                for vchild in subchild.children:
                                    if vchild.type == "identifier":
                                        param_names.append(get_node_text_fn(vchild))
                                        break
                            elif subchild.type == "identifier":
                                param_names.append(get_node_text_fn(subchild))
        if param_names:
            return param_names

    for child in node.children:
        result = get_java_parameter_names(child, function_start_line, get_node_text_fn)
        if result:
            return result

    return param_names


def extract_java_field_types(
    root_node: Any, get_node_text_fn: Callable[[Any], str]
) -> List[Tuple[str, str, str]]:
    """Return ``(declaring_class, field_name, type_name)`` for every class field
    whose declared type is a simple class name.

    A Java method routinely calls through a class field — ``cartsService.getCart()``
    or the ``this.cartsService.getCart()`` variant — but the call-graph resolver
    only knows a receiver's type for *local* variables. A field's type lives on
    the class, so without this the edge is dropped (and, before the
    receiver-extraction fix in JavaParser.extract_call_name, mis-bound to any
    component sharing the field's simple name). This mirrors the Dart/TypeScript
    field mechanisms.

    A field is declared by a ``field_declaration`` carrying a ``type`` and one or
    more ``variable_declarator`` names (``int a, b;`` declares two). Only a
    simple class type (a ``type_identifier``) is reported; primitives (``int``),
    arrays, and generics like ``Map<String, CheckoutData>`` or ``Mono<Checkout>``
    name no single component and are deliberately skipped to keep resolution
    high-precision. Each triple is scoped to the class that DECLARES the field
    (one file may declare several classes, including nested ones). Deduplicated
    on ``(class, field)``; order follows document order.
    """
    results: List[Tuple[str, str, str]] = []
    seen: Set[Tuple[str, str]] = set()

    def record(
        class_name: Optional[str], field: Optional[str], type_name: Optional[str]
    ) -> None:
        if not class_name or not field or not type_name:
            return
        if (class_name, field) in seen:
            return
        seen.add((class_name, field))
        results.append((class_name, field, type_name))

    def visit_class(class_node: Any) -> None:
        class_name = _field_text(class_node.child_by_field_name("name"), get_node_text_fn)
        body = _first_child_of_types(class_node, _CLASS_BODY_TYPES)
        if not class_name or body is None:
            return
        # Only this class's OWN body fields. A nested class's field_declaration
        # lives under its own body node, so it is not reached here and is instead
        # attributed to that class when the stack visits it.
        for member in body.children:
            if member.type != _FIELD_DECLARATION:
                continue
            type_node = member.child_by_field_name("type")
            if type_node is None or type_node.type != _SIMPLE_TYPE:
                continue
            type_name = _field_text(type_node, get_node_text_fn)
            for child in member.children:
                if child.type != _VARIABLE_DECLARATOR:
                    continue
                field = _field_text(child.child_by_field_name("name"), get_node_text_fn)
                record(class_name, field, type_name)

    # Explicit stack rather than recursion: a deeply nested file could exhaust
    # the interpreter's stack and drop every field-receiver edge in the file.
    stack = [root_node]
    while stack:
        current = stack.pop()
        if current.type in _CLASS_NODE_TYPES:
            visit_class(current)
        stack.extend(reversed(current.children))
    return results


def _field_text(node: Optional[Any], get_node_text_fn: Callable[[Any], str]) -> Optional[str]:
    """Read one node's text, mapping the failure sentinel onto None."""
    if node is None:
        return None
    value = get_node_text_fn(node)
    if not value or value == "unknown":
        return None
    return value


def _first_child_of_types(node: Any, type_names: FrozenSet[str]) -> Optional[Any]:
    for child in node.children:
        if child.type in type_names:
            return child
    return None
