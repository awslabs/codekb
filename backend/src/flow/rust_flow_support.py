"""Rust-specific flow analysis support methods."""

from typing import Dict, List, Optional, Tuple, Any


_FUNCTION_NODE_TYPES = ("function_item", "function_signature_item")

_TYPE_NODE_TYPES = (
    "type_identifier",
    "scoped_type_identifier",
    "primitive_type",
    "generic_type",
    "tuple_type",
    "array_type",
    "reference_type",
    "pointer_type",
    "dynamic_type",
)


def _strip_generics(name: str) -> str:
    """Strip generic type arguments from a name (`Foo<T>` -> `Foo`)."""
    if not name:
        return name
    idx = name.find("<")
    if idx == -1:
        return name
    return name[:idx]


def _last_segment(name: str) -> str:
    """For a `module::Foo` style name, return `Foo`."""
    if "::" in name:
        return name.rsplit("::", 1)[1]
    return name


def _unwrap_type_node(node: Any, get_node_text_fn) -> Optional[str]:
    """Return a bare type name from a type-bearing AST node.

    Handles `&T`, `&mut T`, `*const T`, `Vec<T>`, `module::Foo` by recursing
    or stripping wrapper syntax to recover the base type identifier.
    """
    if node is None:
        return None

    node_type = node.type

    if node_type == "type_identifier":
        return get_node_text_fn(node)

    if node_type == "primitive_type":
        return get_node_text_fn(node)

    if node_type == "scoped_type_identifier":
        return _last_segment(get_node_text_fn(node))

    if node_type == "generic_type":
        # generic_type: type_identifier "<" type_arguments ">"
        for child in node.children:
            if child.type in ("type_identifier", "scoped_type_identifier"):
                return _last_segment(get_node_text_fn(child))
        # Fallback: strip the generic suffix from the raw text.
        return _strip_generics(get_node_text_fn(node))

    if node_type in ("reference_type", "pointer_type"):
        # Skip `&`, `mut`, `*`, `const` keywords; recurse into the underlying type.
        for child in node.children:
            if child.type not in ("&", "*", "mut", "const", "mutable_specifier", "lifetime"):
                unwrapped = _unwrap_type_node(child, get_node_text_fn)
                if unwrapped:
                    return unwrapped

    if node_type == "dynamic_type":
        # `dyn Trait` — surface the trait name.
        for child in node.children:
            if child.type != "dyn":
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


def _find_parameters_node(function_node: Any) -> Optional[Any]:
    """Return the `parameters` child of a function-like node."""
    for child in function_node.children:
        if child.type == "parameters":
            return child
    return None


def _extract_param_name_and_type(
    param_node: Any, get_node_text_fn
) -> Tuple[Optional[str], Optional[str]]:
    """Pull (name, type) out of a `parameter` node."""
    name: Optional[str] = None
    type_name: Optional[str] = None

    for child in param_node.children:
        if child.type == "identifier" and name is None:
            name = get_node_text_fn(child)
        elif child.type in _TYPE_NODE_TYPES and type_name is None:
            type_name = _unwrap_type_node(child, get_node_text_fn)

    return name, type_name


def extract_rust_parameter_types(
    node: Any, function_start_line: int, get_node_text_fn
) -> Dict[str, str]:
    """Extract parameter type annotations from a Rust function declaration."""
    function_node = _find_function_node_at_line(node, function_start_line)
    if function_node is None:
        return {}

    params_node = _find_parameters_node(function_node)
    if params_node is None:
        return {}

    param_types: Dict[str, str] = {}
    for child in params_node.children:
        if child.type != "parameter":
            continue
        name, type_name = _extract_param_name_and_type(child, get_node_text_fn)
        if name and type_name:
            param_types[name] = type_name

    return param_types


def extract_rust_assignment_type(
    node: Any, get_node_text_fn
) -> Tuple[Optional[str], Optional[str]]:
    """Extract (var_name, type_name) from a Rust `let_declaration`.

    Handles three idiomatic patterns:
        let x: Foo = ...;          # explicit type annotation
        let x = Foo::new(...);     # constructor call -> type Foo
        let x = Foo { ... };       # struct literal   -> type Foo
    """
    if node.type != "let_declaration":
        return None, None

    var_name: Optional[str] = None
    declared_type: Optional[str] = None
    init_type: Optional[str] = None

    # Iterate children: pattern (identifier / mutable_specifier identifier),
    # optional type annotation, optional `= <value>`.
    children = list(node.children)
    i = 0
    saw_equals = False
    while i < len(children):
        child = children[i]
        ctype = child.type

        # Pattern position (before `:` or `=`)
        if ctype == "identifier" and var_name is None:
            var_name = get_node_text_fn(child)

        elif ctype == "mutable_specifier":
            # `let mut x` — the next identifier is the variable name.
            pass

        elif ctype in _TYPE_NODE_TYPES and declared_type is None:
            declared_type = _unwrap_type_node(child, get_node_text_fn)

        elif ctype == "=":
            saw_equals = True

        elif saw_equals and init_type is None:
            init_type = _infer_type_from_initializer(child, get_node_text_fn)

        i += 1

    type_name = declared_type or init_type
    return var_name, type_name


def _infer_type_from_initializer(node: Any, get_node_text_fn) -> Optional[str]:
    """Best-effort: recover a type name from a `let` initializer expression."""
    ntype = node.type

    if ntype == "call_expression":
        # `Foo::new(...)` -> type Foo (look for scoped_identifier as function)
        if node.children:
            fn = node.children[0]
            if fn.type == "scoped_identifier":
                # Last segment is the function name, type is the path before it.
                text = get_node_text_fn(fn)
                if "::" in text:
                    type_part = text.rsplit("::", 1)[0]
                    return _strip_generics(_last_segment(type_part))
            elif fn.type == "generic_function":
                # `Foo::<T>::new(...)` -> recurse on the inner function expression
                for child in fn.children:
                    if child.type != "type_arguments":
                        return _infer_type_from_initializer(
                            _wrap_as_call(node, child), get_node_text_fn
                        )

    if ntype == "struct_expression":
        # `Foo { ... }` -> the `name` field is the struct name.
        for child in node.children:
            if child.type in ("type_identifier", "scoped_type_identifier"):
                return _last_segment(_strip_generics(get_node_text_fn(child)))

    if ntype == "reference_expression":
        # `&Foo::new()` etc. - recurse past the reference.
        for child in node.children:
            if child.type not in ("&", "mut", "mutable_specifier"):
                inferred = _infer_type_from_initializer(child, get_node_text_fn)
                if inferred:
                    return inferred

    if ntype == "try_expression":
        # `Foo::new()?` - recurse past the trailing `?`.
        for child in node.children:
            if child.type != "?":
                inferred = _infer_type_from_initializer(child, get_node_text_fn)
                if inferred:
                    return inferred

    return None


def _wrap_as_call(call_node: Any, function_child: Any) -> Any:
    """Helper that returns an object with the same `children`/`type` interface
    as a call_expression but with the function part replaced. Used for the
    rare `Foo::<T>::new()` recursion case.
    """

    class _Shim:
        type = "call_expression"
        children = [function_child] + [c for c in call_node.children[1:] if c is not function_child]

    return _Shim()


def get_rust_parameter_names(node: Any, function_start_line: int, get_node_text_fn) -> List[str]:
    """Extract Rust parameter names in declaration order (excluding `self`)."""
    function_node = _find_function_node_at_line(node, function_start_line)
    if function_node is None:
        return []

    params_node = _find_parameters_node(function_node)
    if params_node is None:
        return []

    names: List[str] = []
    for child in params_node.children:
        if child.type != "parameter":
            continue
        for subchild in child.children:
            if subchild.type == "identifier":
                names.append(get_node_text_fn(subchild))
                break

    return names
