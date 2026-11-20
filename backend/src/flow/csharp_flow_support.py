"""C#-specific flow analysis support methods."""

from typing import Dict, List, Optional, Tuple, Any


# Tree-sitter-c-sharp node types that represent a "type expression" we care
# about for type inference. We intentionally exclude `nullable_type` and
# `array_type` from this set and unwrap them below to keep the bare class name.
_TYPE_NODE_TYPES = (
    "identifier",
    "predefined_type",
    "qualified_name",
    "generic_name",
)

_FUNCTION_NODE_TYPES = (
    "method_declaration",
    "constructor_declaration",
    "destructor_declaration",
    "operator_declaration",
    "conversion_operator_declaration",
    "local_function_statement",
)


def _strip_generics(name: str) -> str:
    """Strip generic type arguments from a name (`Foo<T>` -> `Foo`)."""
    if not name:
        return name
    idx = name.find("<")
    if idx == -1:
        return name
    return name[:idx]


def _unwrap_type_node(node: Any, get_node_text_fn) -> Optional[str]:
    """Return a bare type name from a type-bearing AST node.

    Handles nullable (`Foo?`), arrays (`Foo[]`), pointers (`Foo*`) and generics
    (`Foo<T>`) by recursing into the inner element type.
    """
    if node is None:
        return None

    node_type = node.type

    if node_type in _TYPE_NODE_TYPES:
        return _strip_generics(get_node_text_fn(node))

    if node_type in ("nullable_type", "array_type", "pointer_type", "ref_type"):
        # First child of a wrapper-style type node is the element type.
        for child in node.children:
            unwrapped = _unwrap_type_node(child, get_node_text_fn)
            if unwrapped:
                return unwrapped

    return None


def _find_function_node_at_line(node: Any, function_start_line: int) -> Optional[Any]:
    """Find a function-like declaration whose start line matches the target."""
    if node.type in _FUNCTION_NODE_TYPES and node.start_point[0] == function_start_line:
        return node
    for child in node.children:
        found = _find_function_node_at_line(child, function_start_line)
        if found is not None:
            return found
    return None


def _find_parameter_list(function_node: Any) -> Optional[Any]:
    """Return the `parameter_list` child of a function-like node."""
    for child in function_node.children:
        if child.type == "parameter_list":
            return child
    return None


def extract_csharp_parameter_types(
    node: Any, function_start_line: int, get_node_text_fn
) -> Dict[str, str]:
    """Extract parameter type annotations from a C# function-like declaration."""
    function_node = _find_function_node_at_line(node, function_start_line)
    if function_node is None:
        return {}

    param_list = _find_parameter_list(function_node)
    if param_list is None:
        return {}

    param_types: Dict[str, str] = {}
    for param_child in param_list.children:
        if param_child.type != "parameter":
            continue

        param_name: Optional[str] = None
        type_name: Optional[str] = None

        for subchild in param_child.children:
            if subchild.type == "identifier" and param_name is None:
                # First plain identifier inside a `parameter` is the parameter name
                # only after the type was already consumed; defer assignment until
                # we've seen at least one type-bearing node. We handle that by
                # checking type_name below.
                if type_name is not None:
                    param_name = get_node_text_fn(subchild)
                else:
                    # Could be `type` if grammar omitted explicit type wrapper for
                    # this position; we'll re-evaluate after the loop.
                    pass
            elif subchild.type in _TYPE_NODE_TYPES + (
                "nullable_type",
                "array_type",
                "pointer_type",
                "ref_type",
            ):
                if type_name is None:
                    type_name = _unwrap_type_node(subchild, get_node_text_fn)

        # Fallback: if we didn't capture the name above, walk again and pick the
        # last identifier (parameter name follows the type in C# syntax).
        if param_name is None:
            last_identifier: Optional[str] = None
            for subchild in param_child.children:
                if subchild.type == "identifier":
                    last_identifier = get_node_text_fn(subchild)
            param_name = last_identifier

        if param_name and type_name:
            param_types[param_name] = type_name

    return param_types


def extract_csharp_assignment_type(
    node: Any, get_node_text_fn
) -> Tuple[Optional[str], Optional[str]]:
    """Extract (var_name, type_name) from a C# local variable declaration.

    Handles both the explicit and inferred forms:
        Foo foo = new Foo();
        var foo = new Foo();
    """
    # `node` is a `local_declaration_statement` which wraps a `variable_declaration`.
    var_decl = None
    if node.type == "variable_declaration":
        var_decl = node
    else:
        for child in node.children:
            if child.type == "variable_declaration":
                var_decl = child
                break

    if var_decl is None:
        return None, None

    declared_type: Optional[str] = None
    is_var = False
    for child in var_decl.children:
        if child.type == "implicit_type":
            is_var = True
        elif declared_type is None:
            unwrapped = _unwrap_type_node(child, get_node_text_fn)
            if unwrapped:
                declared_type = unwrapped

    var_name: Optional[str] = None
    init_type: Optional[str] = None

    for child in var_decl.children:
        if child.type != "variable_declarator":
            continue
        for subchild in child.children:
            if subchild.type == "identifier" and var_name is None:
                var_name = get_node_text_fn(subchild)
            elif subchild.type == "equals_value_clause":
                # `= <expr>`; pull the RHS expression.
                for vchild in subchild.children:
                    if vchild.type == "object_creation_expression":
                        # `new Foo(...)` or `new Foo<T>(...)`
                        for ochild in vchild.children:
                            if ochild.type in _TYPE_NODE_TYPES:
                                init_type = _strip_generics(get_node_text_fn(ochild))
                                break
                    elif vchild.type == "implicit_object_creation_expression":
                        # `new(...)` — type is inferred from the LHS, leave init_type empty.
                        pass

    # Prefer the explicitly declared type; otherwise use the constructor type
    # we recovered from the RHS (covers the `var x = new Foo()` case).
    type_name = declared_type if (declared_type and not is_var) else init_type
    return var_name, type_name


def get_csharp_parameter_names(node: Any, function_start_line: int, get_node_text_fn) -> List[str]:
    """Extract C# parameter names in declaration order."""
    function_node = _find_function_node_at_line(node, function_start_line)
    if function_node is None:
        return []

    param_list = _find_parameter_list(function_node)
    if param_list is None:
        return []

    names: List[str] = []
    for param_child in param_list.children:
        if param_child.type != "parameter":
            continue
        # The parameter name is the last identifier child of the parameter node.
        last_identifier: Optional[str] = None
        for subchild in param_child.children:
            if subchild.type == "identifier":
                last_identifier = get_node_text_fn(subchild)
        if last_identifier:
            names.append(last_identifier)

    return names
