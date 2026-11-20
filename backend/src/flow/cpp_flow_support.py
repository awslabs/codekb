"""C / C++-specific flow analysis support methods."""

from typing import Dict, List, Optional, Tuple, Any


# Nodes that can carry a function's parameter list at a given line.
_FUNCTION_NODE_TYPES = ("function_definition", "declaration", "field_declaration")

# Declarator wrappers between a declaration and its leaf identifier.
_DECLARATOR_WRAPPERS = (
    "pointer_declarator",
    "reference_declarator",
    "parenthesized_declarator",
    "array_declarator",
)


def _last_segment(name: str) -> str:
    """`std::string` -> `string`, `a::b::C` -> `C`."""
    if "::" in name:
        return name.rsplit("::", 1)[1]
    return name


def _strip_generics(name: str) -> str:
    idx = name.find("<")
    return name[:idx] if idx != -1 else name


def _unwrap_type(type_node: Any, get_text_fn) -> Optional[str]:
    """Recover a bare type name from a `[type]` node."""
    if type_node is None:
        return None
    t = type_node.type
    if t in ("type_identifier", "primitive_type", "sized_type_specifier"):
        return get_text_fn(type_node)
    if t == "qualified_identifier":
        return _last_segment(get_text_fn(type_node))
    if t == "template_type":
        name = type_node.child_by_field_name("name")
        if name is not None:
            return _last_segment(_strip_generics(get_text_fn(name)))
        return _strip_generics(_last_segment(get_text_fn(type_node)))
    # placeholder_type_specifier ("auto") and everything else: not a concrete type.
    return None


def _innermost_identifier(node: Any, get_text_fn) -> Optional[str]:
    """Dig through declarator wrappers to the leaf identifier."""
    cur = node
    seen = 0
    while cur is not None and seen < 10:
        seen += 1
        if cur.type in ("identifier", "field_identifier"):
            return get_text_fn(cur)
        nxt = cur.child_by_field_name("declarator")
        if nxt is None:
            for ch in cur.children:
                if ch.type in ("identifier", "field_identifier"):
                    return get_text_fn(ch)
            return None
        cur = nxt
    return None


def _find_function_declarator(node: Any) -> Optional[Any]:
    cur = node.child_by_field_name("declarator")
    seen = 0
    while cur is not None and seen < 10:
        seen += 1
        if cur.type == "function_declarator":
            return cur
        if cur.type in _DECLARATOR_WRAPPERS:
            nxt = cur.child_by_field_name("declarator")
            if nxt is None:
                nxt = next(
                    (
                        ch
                        for ch in cur.children
                        if ch.type in _DECLARATOR_WRAPPERS or ch.type == "function_declarator"
                    ),
                    None,
                )
            cur = nxt
        else:
            return None
    return None


def _find_function_node_at_line(node: Any, function_start_line: int) -> Optional[Any]:
    """Find a function-like node whose start line matches and that has params."""
    if (
        node.type in _FUNCTION_NODE_TYPES
        and node.start_point[0] == function_start_line
        and _find_function_declarator(node) is not None
    ):
        return node
    for child in node.children:
        found = _find_function_node_at_line(child, function_start_line)
        if found is not None:
            return found
    return None


def _iter_parameters(function_node: Any):
    func_decl = _find_function_declarator(function_node)
    if func_decl is None:
        return
    params = func_decl.child_by_field_name("parameters")
    if params is None:
        return
    for child in params.children:
        if child.type in (
            "parameter_declaration",
            "optional_parameter_declaration",
            "variadic_parameter_declaration",
        ):
            yield child


def extract_cpp_parameter_types(
    node: Any, function_start_line: int, get_node_text_fn
) -> Dict[str, str]:
    """Extract parameter (name -> type) map from a C/C++ function at a line."""
    function_node = _find_function_node_at_line(node, function_start_line)
    if function_node is None:
        return {}

    param_types: Dict[str, str] = {}
    for param in _iter_parameters(function_node):
        type_node = param.child_by_field_name("type")
        declarator = param.child_by_field_name("declarator")
        type_name = _unwrap_type(type_node, get_node_text_fn)
        name = _innermost_identifier(declarator, get_node_text_fn) if declarator else None
        if name and type_name:
            param_types[name] = type_name
    return param_types


def get_cpp_parameter_names(node: Any, function_start_line: int, get_node_text_fn) -> List[str]:
    """Extract C/C++ parameter names in declaration order."""
    function_node = _find_function_node_at_line(node, function_start_line)
    if function_node is None:
        return []

    names: List[str] = []
    for param in _iter_parameters(function_node):
        declarator = param.child_by_field_name("declarator")
        name = _innermost_identifier(declarator, get_node_text_fn) if declarator else None
        if name:
            names.append(name)
    return names


def _type_from_new_expression(node: Any, get_text_fn) -> Optional[str]:
    """`new Foo(...)` -> `Foo`."""
    if node is None or node.type != "new_expression":
        return None
    type_node = node.child_by_field_name("type")
    return _unwrap_type(type_node, get_text_fn)


def extract_cpp_assignment_type(node: Any, get_node_text_fn) -> Tuple[Optional[str], Optional[str]]:
    """Extract (var_name, type_name) from a C/C++ declaration or assignment.

    Handles:
        Foo x;                    # explicit type
        Foo x(...);               # direct-init
        Foo* p = new Foo();       # pointer + new
        auto p = new Foo();       # auto deduced from new
        x = new Foo();            # reassignment (assignment_expression)
    """
    if node.type == "declaration":
        type_node = node.child_by_field_name("type")
        declared_type = _unwrap_type(type_node, get_node_text_fn)

        var_name: Optional[str] = None
        init_type: Optional[str] = None
        for child in node.children:
            if child.type in ("identifier", "field_identifier"):
                var_name = get_node_text_fn(child)
            elif child.type in _DECLARATOR_WRAPPERS:
                var_name = _innermost_identifier(child, get_node_text_fn)
            elif child.type == "init_declarator":
                inner = child.child_by_field_name("declarator")
                var_name = _innermost_identifier(inner, get_node_text_fn)
                value = child.child_by_field_name("value")
                init_type = _type_from_new_expression(value, get_node_text_fn)
        return var_name, (declared_type or init_type)

    if node.type == "assignment_expression":
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        var_name = _innermost_identifier(left, get_node_text_fn) if left is not None else None
        type_name = _type_from_new_expression(right, get_node_text_fn)
        return var_name, type_name

    return None, None
