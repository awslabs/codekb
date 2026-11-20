"""Ruby-specific flow analysis support methods.

These helpers feed the language-agnostic flow analyzer with the local type
information it needs to resolve calls such as ``repo.find`` or ``@gateway.charge``
to a concrete component.

**There is deliberately no ``extract_ruby_parameter_types``.** The other
languages implement one because their grammars carry parameter type annotations
(``void f(Foo x)``); Ruby has none, so such a function could only ever return an
empty mapping. Parameter types for Ruby come from the flow analyzer's second
pass instead, which replays the types of arguments seen at call sites onto the
callee's parameters — that pass needs :func:`get_ruby_parameter_names`.

Names are normalised to match what :class:`~codekb.parsers.ruby_parser.RubyParser`
emits as call names: instance and class variables keep their ``@``/``@@`` sigils
(the parser uses them as receiver keys), while constant paths are reduced to
their last segment (``Billing::AuditLog`` -> ``AuditLog``) so they line up with
the class names used to build ``component_lookup``.
"""

from typing import Any, Callable, List, Optional, Tuple


# Reads the source text of an AST node. The flow analyzer passes its own
# `_get_node_text_simple`, which returns "unknown" rather than raising.
NodeTextFn = Callable[[Any], str]


_FUNCTION_NODE_TYPES = ("method", "singleton_method")

# Parameter forms whose name lives in a ``name`` field, as opposed to a bare
# ``identifier`` parameter which is the node itself.
_WRAPPED_PARAM_TYPES = (
    "optional_parameter",
    "keyword_parameter",
    "splat_parameter",
    "hash_splat_parameter",
    "block_parameter",
)

# Receiver/target node types whose text is usable as a variable key.
_ASSIGNABLE_TARGETS = ("identifier", "instance_variable", "class_variable", "constant")


def _simple_type_name(text: Optional[str]) -> Optional[str]:
    """Reduce a Ruby constant path to its last segment (``A::B`` -> ``B``)."""
    if not text or text == "unknown":
        return None
    return text.split("::")[-1]


def _constructed_type(right_node: Any, get_node_text_fn: NodeTextFn) -> Optional[str]:
    """Return the class name for a construction expression, else ``None``.

    Recognises ``Foo.new(...)`` and ``Foo::Bar.new(...)``, which is how Ruby
    instantiates. Anything else (a literal, a method result, a conditional) tells
    us nothing about the static type.
    """
    if right_node is None or right_node.type != "call":
        return None

    method_node = right_node.child_by_field_name("method")
    if method_node is None or get_node_text_fn(method_node) != "new":
        return None

    receiver = right_node.child_by_field_name("receiver")
    if receiver is None or receiver.type not in ("constant", "scope_resolution"):
        return None

    return _simple_type_name(get_node_text_fn(receiver))


def extract_ruby_assignment_type(
    node: Any, get_node_text_fn: NodeTextFn
) -> Tuple[Optional[str], Optional[str]]:
    """Extract ``(var_name, type_name)`` from a Ruby construction assignment.

    Handles ``assignment`` (``x = Foo.new``, ``@gateway = Foo.new``) and
    ``operator_assignment`` (``@registry ||= Foo.new``). Returns
    ``(None, None)`` when the right-hand side is not a construction.
    """
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")

    if left is None or left.type not in _ASSIGNABLE_TARGETS:
        return None, None

    var_name = get_node_text_fn(left)
    if not var_name or var_name == "unknown":
        return None, None

    type_name = _constructed_type(right, get_node_text_fn)
    if not type_name:
        return None, None

    return var_name, type_name


def get_ruby_parameter_names(
    node: Any, function_start_line: int, get_node_text_fn: NodeTextFn
) -> List[str]:
    """Extract Ruby parameter names in declaration order.

    Names are returned bare — without ``*``, ``**``, ``&``, or a trailing ``:``
    — because that is how they are referred to inside the method body, which is
    what the caller matches them against.
    """
    param_names: List[str] = []

    if node.type in _FUNCTION_NODE_TYPES and node.start_point[0] == function_start_line:
        params_node = node.child_by_field_name("parameters")
        if params_node is None:
            # ``def foo`` with no parameter list.
            return param_names
        for param_child in params_node.children:
            if not param_child.is_named:
                continue
            name: Optional[str]
            if param_child.type == "identifier":
                name = get_node_text_fn(param_child)
            elif param_child.type in _WRAPPED_PARAM_TYPES:
                name_node = param_child.child_by_field_name("name")
                name = get_node_text_fn(name_node) if name_node is not None else None
            else:
                continue
            if name and name != "unknown":
                param_names.append(name)
        return param_names

    for child in node.children:
        result = get_ruby_parameter_names(child, function_start_line, get_node_text_fn)
        if result:
            return result

    return param_names


def extract_ruby_instance_attribute_types(
    root_node: Any, get_node_text_fn: NodeTextFn
) -> List[Tuple[str, str]]:
    """Find ``@ivar = Foo.new`` assignments anywhere in a subtree.

    Instance variables are assigned in ``initialize`` but used from every other
    method, so they fall outside the per-method line range that local variable
    inference works within. The flow analyzer uses this to register
    ``@ivar.method`` lookup keys up front, mirroring what it already does for
    Python's ``self.attr = ClassName()``.

    Returns a list of ``(ivar_name, class_name)`` pairs, sigil included.
    """
    results: List[Tuple[str, str]] = []

    def visit(node: Any) -> None:
        if node.type in ("assignment", "operator_assignment"):
            left = node.child_by_field_name("left")
            if left is not None and left.type == "instance_variable":
                ivar = get_node_text_fn(left)
                type_name = _constructed_type(node.child_by_field_name("right"), get_node_text_fn)
                if ivar and ivar != "unknown" and type_name:
                    results.append((ivar, type_name))
        for child in node.children:
            visit(child)

    visit(root_node)
    return results
