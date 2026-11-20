"""
Dart flow-analysis support.

Four functions feed the language-agnostic flow analyzer the local type
information it needs to resolve a call's receiver — knowing that ``r`` in
``r.find()`` is a ``Repo``.

Dart carries parameter type annotations, so it gets a parameter-types function
rather than relying on call-site replay. It also needs a fourth function the
three-function contract does not name, for field receivers, because
``_repo.find()`` on a declared field is the dominant Flutter idiom and resolves
to nothing without it.

Every type is reduced to the simple class name the component lookup is keyed on,
and anything carrying no information binds nothing rather than guessing: a wrong
binding sends a call edge to the wrong component, which is worse than no edge.
"""

from typing import Any, Callable, Dict, List, Optional, Set, Tuple


# Reads the source text of an AST node. The flow analyzer passes its own
# `_get_node_text_simple`, which returns "unknown" rather than raising.
NodeTextFn = Callable[[Any], str]


# The signature nodes a component's recorded start line can land on. A method is
# a `method_signature` wrapping a `function_signature` at the same row, so the
# outer one is matched first and the parameter list is found one level deeper.
_SIGNATURE_NODE_TYPES = frozenset(
    {
        "function_signature",
        "method_signature",
        "constructor_signature",
        "factory_constructor_signature",
        "constant_constructor_signature",
        "getter_signature",
        "setter_signature",
        "operator_signature",
        "local_function_declaration",
    }
)

_CONSTRUCTOR_SIGNATURE_TYPES = frozenset(
    {
        "constructor_signature",
        "factory_constructor_signature",
        "constant_constructor_signature",
    }
)

# Declarations that own a body in which fields may be declared. An extension is
# deliberately absent: `_enclosing_field_types` uses this set to resolve the
# field an initialising formal (`this.repo`) names, and an extension can declare
# no constructor, so it must not become a stop in that upward walk.
_TYPE_DECLARATION_NODE_TYPES = frozenset(
    {
        "class_definition",
        "mixin_declaration",
        "extension_type_declaration",
        "enum_declaration",
    }
)

# Declarations whose body the top-level field walk reads — one entry wider,
# because an extension can declare STATIC fields. Only a NAMED extension
# qualifies, and the existing `identifier` read enforces that: an unnamed
# extension's members are parented nowhere, so there is no owner to scope to.
_FIELD_OWNER_NODE_TYPES = _TYPE_DECLARATION_NODE_TYPES | {"extension_declaration"}

# Declarations that can put a type parameter in scope over a nested signature.
# An `extension_declaration` carries `type_parameters` just as a class does, so
# `extension Grid<Element> on List<Element>` must contribute `Element`. Same
# members as the field-owner set today; named apart so either can move alone.
_TYPE_PARAMETER_OWNER_NODE_TYPES = _TYPE_DECLARATION_NODE_TYPES | {"extension_declaration"}

_BODY_NODE_TYPES = ("class_body", "enum_body")

# The bodies the top-level field walk opens, paired with
# ``_FIELD_OWNER_NODE_TYPES``. ``_BODY_NODE_TYPES`` stays as it was, so nothing
# that reads it — ``_enclosing_field_types`` included — changes.
_FIELD_BODY_NODE_TYPES = _BODY_NODE_TYPES + ("extension_body",)

# Grammar nodes that spell "declared without a written type": `var` is
# `inferred_type`, `final`/`const` are their own builtins. `late` and `static`
# are anonymous tokens and never reach a text join.
_DECLARATION_MODIFIER_TYPES = frozenset(
    {
        "inferred_type",
        "final_builtin",
        "const_builtin",
        "late",
        "static",
        "external",
        "covariant",
        "abstract",
    }
)

# A reduced name that tells the resolver nothing. Binding one is worse than
# leaving the variable unbound: the lookup matches no component and the slot a
# real binding could fill is spent.
_NO_INFORMATION_TYPES = frozenset(
    {"dynamic", "var", "Object", "void", "Never", "Function", "unknown"}
)

_FIELD_ENTRY_TYPES = ("initialized_identifier", "static_final_declaration")

_FIELD_LIST_TYPES = ("initialized_identifier_list", "static_final_declaration_list")


def _simple_type_name(text: Optional[str]) -> Optional[str]:
    """Reduce a written Dart type to the simple class name lookups are keyed on.

    Strips a trailing ``?``, takes a generic's base (``List<Repo>`` -> ``List``) and a
    library-prefixed name's last segment (``prefix.Repo`` -> ``Repo``). Returns None
    for a type carrying no information, for the ``"unknown"`` text sentinel, and for
    a lone upper-case letter — how a generic type parameter is spotted out of view.
    """
    if not text:
        return None

    name = text.strip()
    for modifier in ("late", "final", "const"):
        while name.startswith(modifier) and name[len(modifier) :][:1] in ("", " ", "\t"):
            name = name[len(modifier) :].strip()

    name = name.split("<")[0].strip()
    while name.endswith("?"):
        name = name[:-1].strip()
    name = name.split(".")[-1].strip()

    if not name or name in _NO_INFORMATION_TYPES:
        return None

    # A generic type parameter is not a class name (`T`, `E`, `T1`).
    if len(name) <= 2 and name[0].isupper() and (len(name) == 1 or name[1].isdigit()):
        return None

    # Anything that is not a plain identifier cannot key a component lookup —
    # a record type `(int, int)` or a leftover function type reaches nothing.
    if not (name[0].isalpha() or name[0] in "_$"):
        return None
    if not all(char.isalnum() or char in "_$" for char in name):
        return None

    return name


def _looks_like_a_class_name(text: str) -> bool:
    """Report whether a written segment follows Dart's class-naming convention.

    A leading ``_`` or ``$`` is skipped so a private class ``_Repo`` qualifies.
    Callers read the text through :func:`_text` first, so the sentinel never
    reaches here.
    """
    letters = text.lstrip("_$")
    return bool(letters) and letters[0].isupper()


def _invoked_class_segment(node: Any, get_node_text_fn: NodeTextFn) -> Optional[str]:
    """Pick the class out of a ``new``/``const`` construction's dotted callee.

    The grammar does NOT separate a library prefix from a named constructor: both
    ``new prefix.Repo()`` and ``new Repo.named()`` parse as ``type_identifier`` ``.``
    ``identifier``. So the last CLASS-SHAPED segment is taken, which reads
    ``prefix.Repo`` as ``Repo`` and ``Repo.named`` as ``Repo`` — taking the first or
    the last segment outright would get one of them wrong.
    """
    segments: List[str] = []
    for child in node.children:
        if child.type in ("type_arguments", "arguments"):
            break
        if child.type not in ("type_identifier", "identifier"):
            continue
        text = _text(child, get_node_text_fn)
        if text is None:
            return None
        segments.append(text)

    for segment in reversed(segments):
        if _looks_like_a_class_name(segment):
            return segment

    return None


def _constructed_type(value_nodes: List[Any], get_node_text_fn: NodeTextFn) -> Optional[str]:
    """Return the class name when a value expression constructs, else None.

    Dart requires no ``new``, so a construction is an ordinary call whose callee is a
    class name: ``Repo()`` is an ``identifier`` plus a ``selector``. ``new Repo()``
    and ``const Repo()`` keep their own node types. A class name is told from a
    function call only by Dart's upper-case convention, so this is a heuristic.
    """
    if not value_nodes:
        return None

    first = value_nodes[0]

    if first.type in ("new_expression", "const_object_expression"):
        return _simple_type_name(_invoked_class_segment(first, get_node_text_fn))

    if first.type != "identifier" or not _invokes(value_nodes[1:]):
        return None

    callee = _text(first, get_node_text_fn)
    if callee is None or not _looks_like_a_class_name(callee):
        return None

    return _simple_type_name(callee)


def _cast_type(value_nodes: List[Any], get_node_text_fn: NodeTextFn) -> Optional[str]:
    """Return the target type of an ``as`` cast, else ``None``.

    A cast is an explicit assertion by the programmer rather than an inference
    from a value, so ``var r = x as Repo;`` carries a type where a method result
    does not.
    """
    if not value_nodes or value_nodes[0].type != "type_cast_expression":
        return None

    cast = _first_child(value_nodes[0], "type_cast")
    if cast is None:
        return None

    parts: List[Any] = []
    seen_operator = False
    for child in cast.children:
        if child.type == "as_operator":
            seen_operator = True
            continue
        if seen_operator:
            parts.append(child)

    return _simple_type_name(_joined_text(parts, get_node_text_fn))


def extract_dart_parameter_types(
    root_node: Any, start_line: int, get_node_text_fn: NodeTextFn
) -> Dict[str, str]:
    """Bind the annotated parameters of the Dart function declared at ``start_line``.

    Covers all four Dart parameter forms: required positional (``Repo r``), optional
    positional (``[Repo? r]``), named (``{required Repo r}``) and an initialising
    formal (``this.repo``). An initialising formal takes the enclosing class's field
    type when visible. A function-typed parameter binds nothing, since a call on it
    resolves to whatever was passed.
    """
    param_types: Dict[str, str] = {}

    signature = _signature_at_line(root_node, start_line)
    if signature is None:
        return param_types

    parameter_list = _find_parameter_list(signature)
    if parameter_list is None:
        return param_types

    type_parameters = _declared_type_parameters(signature, get_node_text_fn)
    field_types: Optional[Dict[str, str]] = None

    for param in _collect_formal_parameters(parameter_list):
        name, annotation, is_initialising = _parameter_parts(param, get_node_text_fn)
        if not name or name == "unknown":
            continue

        type_name = _simple_type_name(annotation)
        if type_name is None and is_initialising:
            if field_types is None:
                field_types = _enclosing_field_types(signature, get_node_text_fn)
            type_name = field_types.get(name)

        if type_name and type_name not in type_parameters:
            param_types[name] = type_name

    return param_types


def extract_dart_assignment_type(
    node: Any, get_node_text_fn: NodeTextFn
) -> Tuple[Optional[str], Optional[str]]:
    """Extract ``(var_name, type_name)`` from one Dart declaration or assignment.

    Handles a declaration, each further variable of a multi-variable declaration (so
    ``var a = Repo(), b = Other();`` binds both and never cross-binds), a
    reassignment, and a Dart 3 pattern variable. An explicit annotation wins over an
    inferred construction: Dart permits a subtype on the right.
    """
    if node.type == "local_variable_declaration":
        definition = _first_child(node, "initialized_variable_definition")
        if definition is None:
            return None, None
        return _declared_variable_type(definition, get_node_text_fn)

    if node.type == "initialized_identifier":
        return _extra_variable_type(node, get_node_text_fn)

    if node.type == "assignment_expression":
        return _reassignment_type(node, get_node_text_fn)

    if node.type == "variable_pattern":
        return _pattern_variable_type(node, get_node_text_fn)

    return None, None


def get_dart_parameter_names(
    root_node: Any, start_line: int, get_node_text_fn: NodeTextFn
) -> List[str]:
    """Extract Dart POSITIONAL parameter names in declaration order.

    Required positional first, then optional positional in ``[...]``. Named
    parameters are excluded, and that is the contract's correctness: the only
    consumer pairs index ``i`` of this list with the type of the ``i``-th call-site
    argument, and Dart lets a caller write named arguments in any order — so
    including them would hand one argument's type to a different parameter.
    """
    signature = _signature_at_line(root_node, start_line)
    if signature is None:
        return []

    parameter_list = _find_parameter_list(signature)
    if parameter_list is None:
        return []

    names: List[str] = []
    for param in _collect_formal_parameters(parameter_list, positional_only=True):
        name, _, _ = _parameter_parts(param, get_node_text_fn)
        if name and name != "unknown":
            names.append(name)

    return names


def extract_dart_field_types(
    root_node: Any, get_node_text_fn: NodeTextFn
) -> List[Tuple[str, str, str]]:
    """Find the typed fields of every named Dart declaration in a subtree.

    Returns ``(declaring_class, field_name, type_name)`` triples, so a receiver key
    can be scoped to methods of the class that DECLARES the field — a Dart file
    routinely declares several. Three routes reach a field's type: its own
    annotation, a construction in its initialiser or a constructor body, and a typed
    initialising formal over an unannotated field.
    """
    results: List[Tuple[str, str, str]] = []
    seen: Set[Tuple[str, str]] = set()

    def visit(node: Any) -> None:
        """Record the fields of one declaration; the caller drives the descent."""
        if node.type in _FIELD_OWNER_NODE_TYPES:
            class_name = _first_child_text(node, "identifier", get_node_text_fn)
            body = _first_child_of_types(node, _FIELD_BODY_NODE_TYPES)
            if class_name and class_name != "unknown" and body is not None:
                # A field typed by the declaration's own type parameter names no
                # class, so it must not reach the component lookup. The parameter
                # path filters the same way; `_simple_type_name` alone refuses
                # only a short name like `T`, so `TValue` would slip through and
                # bind to a real class of that name if one existed.
                type_parameters = _declared_type_parameters(node, get_node_text_fn)
                for field, type_name in _class_field_types(body, get_node_text_fn).items():
                    if (class_name, field) in seen or type_name in type_parameters:
                        continue
                    seen.add((class_name, field))
                    results.append((class_name, field, type_name))

    # Explicit stack rather than recursion, as the shared node walk uses: a
    # deeply nested widget tree could otherwise exhaust the interpreter's stack
    # and drop every field-receiver edge in the file.
    stack = [root_node]
    while stack:
        current = stack.pop()
        visit(current)
        # Reversed so popping yields children in document order, as the
        # recursive form did: callers append results in that order.
        stack.extend(reversed(current.children))
    return results


# ── internals ─────────────────────────────────────────────────────────────────


def _text(node: Optional[Any], get_node_text_fn: NodeTextFn) -> Optional[str]:
    """Read one node's text, mapping the failure sentinel onto ``None``."""
    if node is None:
        return None
    value = get_node_text_fn(node)
    if not value or value == "unknown":
        return None
    return value


def _joined_text(nodes: List[Any], get_node_text_fn: NodeTextFn) -> Optional[str]:
    """Join a run of nodes' texts, so ``prefix`` ``.`` ``Repo`` reads as written.

    Anonymous tokens are included deliberately: the ``.`` of a library prefix and the
    ``?`` of a nullable type are what make the joined text reducible.
    """
    if not nodes:
        return None
    parts: List[str] = []
    for node in nodes:
        value = _text(node, get_node_text_fn)
        if value is None:
            return None
        parts.append(value)
    return "".join(parts)


def _is_same_node(left: Any, right: Any) -> bool:
    """Report whether two node handles name the same node of one tree.

    ``child_by_field_name`` and ``children`` hand back distinct wrappers for the same
    node, so ``is`` would compare wrappers rather than positions.
    """
    return (
        left.type == right.type
        and left.start_byte == right.start_byte
        and left.end_byte == right.end_byte
    )


def _first_child(node: Any, child_type: str) -> Optional[Any]:
    """Return the first direct child of the given type."""
    for child in node.children:
        if child.type == child_type:
            return child
    return None


def _first_child_of_types(node: Any, child_types: Tuple[str, ...]) -> Optional[Any]:
    """Return the first direct child whose type is one of ``child_types``."""
    for child in node.children:
        if child.type in child_types:
            return child
    return None


def _first_child_text(node: Any, child_type: str, get_node_text_fn: NodeTextFn) -> Optional[str]:
    """Return the text of the first direct child of the given type."""
    return _text(_first_child(node, child_type), get_node_text_fn)


def _last_child(node: Any, child_type: str) -> Optional[Any]:
    """Return the last direct child of the given type.

    A parameter's name is its last ``identifier``: an earlier one can belong to
    the type, as in ``prefix.Repo r``.
    """
    found = None
    for child in node.children:
        if child.type == child_type:
            found = child
    return found


def _has_child_of_types(node: Any, child_types: Tuple[str, ...]) -> bool:
    """Report whether a node has a direct child of one of the given types."""
    return any(child.type in child_types for child in node.children)


def _invokes(nodes: List[Any]) -> bool:
    """Report whether a run of value nodes contains an argument-bearing selector.

    That selector is what marks "a call happened here" in this grammar; a
    ``.name`` selector on its own is a property read.
    """
    for node in nodes:
        if node.type == "selector" and _first_child(node, "argument_part") is not None:
            return True
    return False


def _signature_at_line(node: Any, start_line: int) -> Optional[Any]:
    """Find the declaration signature starting at ``start_line``.

    Line-anchored like the siblings' lookups. Subtrees that cannot contain the
    row are not descended into, which changes no result.
    """
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in _SIGNATURE_NODE_TYPES and current.start_point[0] == start_line:
            return current
        # Reversed so the first match in document order is returned, as the
        # recursive form did. Subtrees that cannot contain the row are skipped.
        for child in reversed(current.children):
            if child.start_point[0] > start_line or child.end_point[0] < start_line:
                continue
            stack.append(child)

    return None


def _find_parameter_list(node: Any) -> Optional[Any]:
    """Find the ``formal_parameter_list`` a signature owns, one level deep.

    A method is a ``method_signature`` wrapping the ``function_signature`` that
    actually owns the list, so the second pass is required.
    """
    direct = _first_child(node, "formal_parameter_list")
    if direct is not None:
        return direct
    for child in node.children:
        found = _first_child(child, "formal_parameter_list")
        if found is not None:
            return found
    return None


def _collect_formal_parameters(parameter_list: Any, positional_only: bool = False) -> List[Any]:
    """Return every ``formal_parameter`` of a list, in declaration order.

    ``optional_formal_parameters`` holds the ``[...]`` and ``{...}`` groups. ERROR is
    descended into too: Dart forbids both groups in one signature, so a signature
    writing both parses with the first group under an ERROR node.
    """
    found: List[Any] = []
    for child in parameter_list.children:
        if child.type == "formal_parameter":
            found.append(child)
        elif child.type in ("optional_formal_parameters", "ERROR"):
            if positional_only and _is_named_parameter_group(child):
                continue
            for inner in child.children:
                if inner.type == "formal_parameter":
                    found.append(inner)
    return found


def _is_named_parameter_group(group: Any) -> bool:
    """Report whether an optional-parameter group was written with ``{}``.

    Dart spells named parameters ``{...}`` and optional positional ones ``[...]``.
    The brace is an anonymous token, so the written form is read rather than guessed.
    """
    for child in group.children:
        if child.type == "{":
            return True
        if child.type == "[":
            return False
    return False


def _parameter_parts(
    param: Any, get_node_text_fn: NodeTextFn
) -> Tuple[Optional[str], Optional[str], bool]:
    """Return ``(name, written annotation, is initialising formal)`` for a parameter.

    A function-typed parameter returns no name, which is how it binds nothing:
    ``void Function(Repo) cb`` carries a ``function_type`` child and ``Repo cb()``
    carries a nested ``formal_parameter_list``.
    """
    inner = _first_child_of_types(param, ("constructor_param", "super_formal_parameter"))
    if inner is not None:
        name_node = _last_child(inner, "identifier")
        annotation_nodes: List[Any] = []
        for child in inner.children:
            if child.type in ("this", "super"):
                break
            annotation_nodes.append(child)
        return (
            _text(name_node, get_node_text_fn),
            _joined_text(annotation_nodes, get_node_text_fn),
            True,
        )

    if _has_child_of_types(param, ("function_type", "formal_parameter_list")):
        return None, None, False

    name_node = _last_child(param, "identifier")
    if name_node is None:
        return None, None, False

    annotation_nodes = []
    for child in param.children:
        if _is_same_node(child, name_node):
            break
        if child.type in _DECLARATION_MODIFIER_TYPES:
            continue
        annotation_nodes.append(child)

    return (
        _text(name_node, get_node_text_fn),
        _joined_text(annotation_nodes, get_node_text_fn),
        False,
    )


def _declared_type_parameters(signature: Any, get_node_text_fn: NodeTextFn) -> Set[str]:
    """Collect the type parameters the signature and its enclosing declarations declare.

    ``T`` in ``void f<T>(T x)`` is not a class name, and neither is ``Element`` in
    ``extension Grid<Element> on List<Element>`` — so the upward walk includes
    extension declarations, or a generic extension's parameter would bind as a class.
    """
    names: Set[str] = set()

    def collect(node: Any) -> None:
        group = _first_child(node, "type_parameters")
        if group is None:
            return
        for child in group.children:
            if child.type != "type_parameter":
                continue
            name = _first_child_text(child, "type_identifier", get_node_text_fn)
            if name:
                names.add(name)

    collect(signature)

    node = signature.parent
    while node is not None:
        if node.type in _TYPE_PARAMETER_OWNER_NODE_TYPES:
            collect(node)
        node = node.parent

    return names


def _enclosing_field_types(signature: Any, get_node_text_fn: NodeTextFn) -> Dict[str, str]:
    """Return the field types of the type declaration enclosing a signature.

    An initialising formal names a field of its OWN class, so nothing wider is read:
    a same-named field in another class is not what ``this.repo`` refers to.
    """
    node = signature.parent
    while node is not None:
        if node.type in _TYPE_DECLARATION_NODE_TYPES:
            body = _first_child_of_types(node, _BODY_NODE_TYPES)
            if body is not None:
                return _class_field_types(body, get_node_text_fn)
            return {}
        node = node.parent
    return {}


def _class_field_types(body: Any, get_node_text_fn: NodeTextFn) -> Dict[str, str]:
    """Resolve the fields of one declaration body by the three routes of."""
    field_types: Dict[str, str] = {}
    declared: Set[str] = set()

    for child in body.children:
        if child.type != "declaration":
            continue
        entries, annotation_nodes = _field_declaration_parts(child)
        if not entries:
            continue
        annotated = _simple_type_name(_joined_text(annotation_nodes, get_node_text_fn))
        for entry in entries:
            field = _first_child_text(entry, "identifier", get_node_text_fn)
            if not field:
                continue
            declared.add(field)
            if annotated:
                field_types.setdefault(field, annotated)
                continue
            constructed = _constructed_type(_entry_value_nodes(entry), get_node_text_fn)
            if constructed:
                field_types.setdefault(field, constructed)

    for field, type_name in _constructor_body_assignments(body, get_node_text_fn):
        if field in declared:
            field_types.setdefault(field, type_name)

    for field, type_name in _initialising_formal_types(body, get_node_text_fn):
        field_types.setdefault(field, type_name)

    return field_types


def _field_declaration_parts(declaration: Any) -> Tuple[List[Any], List[Any]]:
    """Split a class-level ``declaration`` into its field entries and annotation.

    A ``declaration`` holding a signature is a method or a constructor, not a
    field, and yields no entries.
    """
    field_list = _first_child_of_types(declaration, _FIELD_LIST_TYPES)
    if field_list is None:
        return [], []

    entries = [child for child in field_list.children if child.type in _FIELD_ENTRY_TYPES]

    annotation_nodes: List[Any] = []
    for child in declaration.children:
        if _is_same_node(child, field_list):
            break
        if child.type in _DECLARATION_MODIFIER_TYPES:
            continue
        annotation_nodes.append(child)

    return entries, annotation_nodes


def _entry_value_nodes(entry: Any) -> List[Any]:
    """Return the value nodes of one field entry, after its name."""
    named = [child for child in entry.children if child.is_named]
    return named[1:]


def _constructor_body_assignments(body: Any, get_node_text_fn: NodeTextFn) -> List[Tuple[str, str]]:
    """Find ``_repo = Repo();`` in a constructor body.

    Only a constructor's body is read. An assignment in an ordinary method is a
    local variable far more often than a field, and reading those would type a
    name that is not a field at all.
    """
    found: List[Tuple[str, str]] = []
    children = list(body.children)

    for index, child in enumerate(children):
        if child.type != "method_signature":
            continue
        if _first_child_of_types(child, tuple(_CONSTRUCTOR_SIGNATURE_TYPES)) is None:
            continue
        following = index + 1
        if following >= len(children) or children[following].type != "function_body":
            continue

        stack = [children[following]]
        while stack:
            current = stack.pop()
            if current.type == "assignment_expression":
                name, type_name = _reassignment_type(current, get_node_text_fn)
                if name and type_name:
                    found.append((name, type_name))
            # Reversed so popping yields children in document order, as the
            # recursive form did: callers append results in that order.
            stack.extend(reversed(current.children))

    return found


def _initialising_formal_types(body: Any, get_node_text_fn: NodeTextFn) -> List[Tuple[str, str]]:
    """Find ``Repo(Repo this._repo)`` — a typed initialising formal.

    The one initialising-formal form that adds anything: an untyped
    ``this._repo`` takes the field's own annotation, which route 1 already has.
    """
    found: List[Tuple[str, str]] = []

    stack = [body]
    while stack:
        current = stack.pop()
        if current.type == "constructor_param":
            name_node = _last_child(current, "identifier")
            annotation_nodes: List[Any] = []
            for child in current.children:
                if child.type == "this":
                    break
                annotation_nodes.append(child)
            name = _text(name_node, get_node_text_fn)
            type_name = _simple_type_name(_joined_text(annotation_nodes, get_node_text_fn))
            if name and type_name:
                found.append((name, type_name))
        # Reversed so popping yields children in document order, as the
        # recursive form did: callers append results in that order.
        stack.extend(reversed(current.children))
    return found


def _declared_variable_type(
    definition: Any, get_node_text_fn: NodeTextFn
) -> Tuple[Optional[str], Optional[str]]:
    """Bind the first variable of an ``initialized_variable_definition``."""
    name_node = definition.child_by_field_name("name")
    name = _text(name_node, get_node_text_fn)
    if not name:
        return None, None

    annotation_nodes: List[Any] = []
    value_nodes: List[Any] = []
    passed_name = False
    for child in definition.children:
        if not passed_name and _is_same_node(child, name_node):
            passed_name = True
            continue
        if not passed_name:
            if child.type not in _DECLARATION_MODIFIER_TYPES:
                annotation_nodes.append(child)
            continue
        # A further variable of a multi-variable declaration is its own node and
        # is bound on its own visit, never from this one's value.
        if child.is_named and child.type != "initialized_identifier":
            value_nodes.append(child)

    annotated = _simple_type_name(_joined_text(annotation_nodes, get_node_text_fn))
    if annotated:
        return name, annotated

    return name, _inferred_value_type(value_nodes, get_node_text_fn)


def _extra_variable_type(
    entry: Any, get_node_text_fn: NodeTextFn
) -> Tuple[Optional[str], Optional[str]]:
    """Bind one further variable of a multi-variable declaration."""
    named = [child for child in entry.children if child.is_named]
    if not named or named[0].type != "identifier":
        return None, None

    name = _text(named[0], get_node_text_fn)
    if not name:
        return None, None

    return name, _inferred_value_type(named[1:], get_node_text_fn)


def _pattern_variable_type(
    pattern: Any, get_node_text_fn: NodeTextFn
) -> Tuple[Optional[str], Optional[str]]:
    """Bind one variable of a Dart 3 pattern, where the pattern wrote its type.

    One node type covers every place Dart 3 introduces a pattern variable, so this
    handler serves a destructuring declaration, an if-case, a switch case and a
    pattern for-in alike. Each variable is its own node, so a record pattern binds
    each independently and never cross-binds.
    """
    name_node = _last_child(pattern, "identifier")
    name = _text(name_node, get_node_text_fn)
    if not name or name == "_":
        return None, None

    annotation_nodes: List[Any] = []
    for child in pattern.children:
        if name_node is not None and _is_same_node(child, name_node):
            break
        if child.type in _DECLARATION_MODIFIER_TYPES:
            continue
        annotation_nodes.append(child)

    if not any(child.type == "type_identifier" for child in annotation_nodes):
        return name, None

    return name, _simple_type_name(_joined_text(annotation_nodes, get_node_text_fn))


def _reassignment_type(
    node: Any, get_node_text_fn: NodeTextFn
) -> Tuple[Optional[str], Optional[str]]:
    """Bind the target of ``r = Repo();``, treated the same as a declaration."""
    left = node.child_by_field_name("left")
    if left is None or left.type != "assignable_expression":
        return None, None

    named = [child for child in left.children if child.is_named]
    if len(named) != 1 or named[0].type != "identifier":
        return None, None

    name = _text(named[0], get_node_text_fn)
    if not name:
        return None, None

    value_nodes = [
        child
        for child in node.children
        if child.is_named and not _is_same_node(child, left) and child.type != "assignment_operator"
    ]

    return name, _inferred_value_type(value_nodes, get_node_text_fn)


def _inferred_value_type(value_nodes: List[Any], get_node_text_fn: NodeTextFn) -> Optional[str]:
    """Infer a type from a value expression: a construction, then an ``as`` cast.

    A trailing cascade is stripped rather than refused: a cascade evaluates to its
    TARGET, not to the last call in it, so ``var r = Repo()..save();`` is a ``Repo``
    by the language's own rule. An ``await``, a literal or a method result infers
    nothing.
    """
    without_cascades = [node for node in value_nodes if node.type != "cascade_section"]

    constructed = _constructed_type(without_cascades, get_node_text_fn)
    if constructed:
        return constructed

    return _cast_type(without_cascades, get_node_text_fn)
