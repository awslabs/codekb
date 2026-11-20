"""
Ruby-specific parser implementation.

Node types below were verified against ``tree-sitter-ruby`` 0.23.1. A few of the
grammar's shapes are worth stating up front because they drive the structure of
this parser:

* Composite nodes share their type name with the anonymous keyword token that
  introduces them — the ``class`` keyword is itself a node of type ``class``.
  Every dispatch therefore checks :attr:`is_named` so a keyword token is never
  mistaken for a definition.
* ``class Empty; end`` has **no** ``body`` field and ``def foo`` with no
  arguments has **no** ``parameters`` field, so both are treated as optional.
* Visibility is a sticky statement rather than a modifier on the definition:
  a bare ``private`` identifier in a class body applies to every method that
  follows it. It is tracked while iterating a body in source order.
* A class name may be a ``constant`` (``Foo``) or a ``scope_resolution``
  (``Admin::UsersController``). Qualified names are normalised to dots so
  component ids keep the dotted convention used by every other parser and
  ``Component.get_simple_name`` (which splits on ``.``) stays correct.

Ruby has no type annotations, so parameters carry names and defaults only;
type-based call resolution is handled separately in
:mod:`codekb.flow.ruby_flow_support`.
"""

from typing import Any, List, Optional
from pathlib import Path

from .base_parser import BaseLanguageParser
from ..core.models import Component, ComponentType, ComponentAttributes, Parameter, Visibility


# Definition nodes that introduce a new naming scope.
_SCOPE_NODES = ("class", "module")

# ``def foo`` and ``def self.foo`` respectively.
_METHOD_NODES = ("method", "singleton_method")

# Parameter node types, verified against the grammar. ``identifier`` is a plain
# required positional parameter; the rest wrap a name (and sometimes a default).
_PARAM_OPTIONAL = ("optional_parameter", "keyword_parameter")
_PARAM_SPLAT = ("splat_parameter", "hash_splat_parameter", "block_parameter", "forward_parameter")

# Every parameter form that carries its name in a ``name`` field.
_WRAPPED_PARAM_TYPES = _PARAM_OPTIONAL + _PARAM_SPLAT

# Block forms, which introduce their own parameters (``do |x|`` / ``{ |x| }``).
_BLOCK_NODES = ("do_block", "block")

# Nodes that bound local-variable scope when deciding whether a bare identifier
# is a variable read or a paren-less call.
_SCOPE_BOUNDARIES = ("method", "singleton_method", "class", "module", "program")

# Sticky visibility statements, as bare identifiers in a class/module body.
_VISIBILITY_KEYWORDS = {
    "private": Visibility.PRIVATE,
    "protected": Visibility.PROTECTED,
    "public": Visibility.PUBLIC,
}

# Visibility statements in their inline form: ``private def foo; end``.
_INLINE_VISIBILITY = {
    "private": Visibility.PRIVATE,
    "protected": Visibility.PROTECTED,
    "public": Visibility.PUBLIC,
    "private_class_method": Visibility.PRIVATE,
    "public_class_method": Visibility.PUBLIC,
}

# Attribute macros, mapped to the accessor methods each one generates.
# Ruby generates real callable methods here, so emitting them lets calls such as
# ``user.first_name`` resolve instead of dangling.
_ATTR_MACROS = {
    "attr_reader": ("get",),
    "attr_writer": ("set",),
    "attr_accessor": ("get", "set"),
}

# Macros that define a method under a name given as a symbol argument.
_DEFINING_MACROS = ("define_method", "alias_method")

# Guard rails for the declaration list recorded on a class/module. Rails models
# can carry a lot of DSL, and these end up in an LLM prompt.
_MAX_DECLARATIONS = 32
_MAX_DECLARATION_CHARS = 120

# Parent node types in which a bare ``identifier`` occupies a statement or
# condition slot, and is therefore a plausible paren-less method call.
#
# Ruby cannot be disambiguated here: `notify` on its own line is lexically
# identical to reading a local variable named `notify`, and the grammar emits a
# bare `identifier` for both. Since paren-less calls are pervasive in Ruby and
# Rails, they are treated as call candidates — resolution still requires the
# name to match a known component, which bounds the damage of a wrong guess.
#
# This is an allowlist on the *parent* rather than a denylist on the identifier
# because it is far tighter: it naturally excludes assignment targets, argument
# lists, parameter lists, and definition names, all of which are identifiers in
# non-call positions. Notably `argument_list` is excluded, so `foo(bar)` does not
# treat `bar` as a call — that costs us `return helper`, which is the deliberate
# trade of recall for precision.
_STATEMENT_PARENTS = (
    "body_statement",  # method, class, module, and do-block bodies
    "block_body",  # { ... } block bodies
    "program",  # top level
    "then",  # if/elsif/unless branches and rescue bodies
    "else",
    "elsif",
    "do",  # while/until loop bodies
    "begin",
    "ensure",
    "parenthesized_statements",
    "if",  # the condition slot of these forms
    "unless",
    "while",
    "until",
    "if_modifier",
    "unless_modifier",
    "while_modifier",
    "until_modifier",
    "rescue_modifier",
    "conditional",  # ternary
)

# Bare identifiers that are language constructs rather than calls.
_NON_CALL_IDENTIFIERS = frozenset(
    {
        "private",
        "protected",
        "public",
        "module_function",
        "require",
        "require_relative",
        "self",
        "super",
        "nil",
        "true",
        "false",
        "loop",
        "yield",
        "raise",
        "puts",
        "print",
        "p",
    }
)


class RubyParser(BaseLanguageParser):
    """Ruby-specific parser implementation."""

    def get_call_node_types(self) -> List[str]:
        """Return AST node types that represent method calls in Ruby.

        ``identifier`` is included because a paren-less zero-argument call
        (``notify``) produces a bare identifier rather than a ``call`` node — see
        :data:`_STATEMENT_PARENTS`. :meth:`extract_call_name` filters those down
        to identifiers in statement position.
        """
        return ["call", "identifier"]

    def get_function_node_types(self) -> List[str]:
        """Return AST node types that represent method definitions in Ruby."""
        return ["method", "singleton_method"]

    def extract_call_name(self, function_node: Any) -> Optional[str]:
        """Extract a call name from the ``method``/``receiver`` side of a call.

        The flow analyzer hands us ``node.children[0]`` of a ``call`` node. For a
        bare call (``notify_customer(x)``) that is the method identifier; for a
        call with a receiver (``@gateway.charge(x)``) it is the receiver, so the
        method name is recovered from the parent.
        """
        try:
            parent = function_node.parent
            if parent is None or parent.type != "call":
                # A bare identifier with no enclosing call node: either a
                # paren-less method call or a local variable read.
                return self._bare_identifier_call_name(function_node, parent)

            method_node = parent.child_by_field_name("method")
            receiver_node = parent.child_by_field_name("receiver")

            method_name = self._simple_name_of(method_node) if method_node is not None else None
            if not method_name:
                return None

            if receiver_node is None:
                # Bare call: ``notify_customer(customer)``, ``has_many :items``.
                return method_name

            receiver_text = self._receiver_key(receiver_node)
            if receiver_text is None:
                return None

            # ``Foo.new`` is construction. Resolve to the class itself, matching
            # how the Python parser maps ``EcommerceAPI()`` to ``EcommerceAPI``.
            if method_name == "new" and receiver_node.type in ("constant", "scope_resolution"):
                return receiver_text

            return f"{receiver_text}.{method_name}"
        except Exception:
            return None

    def extract_components(
        self, root_node: Any, source_code: bytes, file_path: Path
    ) -> List[Component]:
        """Extract components from a Ruby syntax tree."""
        components: List[Component] = []
        self._walk_body(root_node, None, file_path, components, class_level=False)
        return components

    def extract_function_parameters(
        self, function_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """Extract parameters from a Ruby method definition node."""
        parameters: List[Parameter] = []

        try:
            params_node = function_node.child_by_field_name("parameters")
            if params_node is None:
                # ``def foo`` / ``def foo; end`` — no parameter list at all.
                return parameters

            for child in params_node.children:
                if not child.is_named:
                    continue

                if child.type == "identifier":
                    positional = self._simple_name_of(child)
                    if positional:
                        parameters.append(Parameter(name=positional, required=True))
                    continue

                name_node = child.child_by_field_name("name")
                name = self._simple_name_of(name_node) if name_node is not None else None

                if child.type in _PARAM_OPTIONAL:
                    value_node = child.child_by_field_name("value")
                    default_value = (
                        self._get_node_text_simple(value_node) if value_node is not None else None
                    )
                    # ``def f(a: )`` is a *required* keyword argument: it has no
                    # default. ``def f(a: 1)`` and ``def f(a = 1)`` are optional.
                    is_required = child.type == "keyword_parameter" and value_node is None
                    parameters.append(
                        Parameter(
                            name=self._decorate_param_name(child, name),
                            default_value=default_value,
                            required=is_required,
                        )
                    )
                elif child.type in _PARAM_SPLAT:
                    parameters.append(
                        Parameter(name=self._decorate_param_name(child, name), required=False)
                    )
                else:
                    # Unknown/newer parameter form — record it rather than drop it.
                    text = self._get_node_text_simple(child)
                    if text and text != "unknown":
                        parameters.append(Parameter(name=text, required=False))
        except Exception:
            pass

        return parameters

    # ── Traversal ────────────────────────────────────────────────────────────

    def _walk_body(
        self,
        container: Any,
        parent_component: Optional[str],
        file_path: Path,
        components: List[Component],
        class_level: bool,
        visibility: Visibility = Visibility.PUBLIC,
        scope_component: Optional[Component] = None,
    ) -> None:
        """Walk a ``program``/``body_statement`` in source order.

        Source order matters: Ruby's ``private`` and ``protected`` are sticky
        statements that apply to every subsequent definition in the same body,
        so visibility is threaded through the iteration rather than read off the
        definition node.

        ``class_level`` marks bodies whose methods belong to the singleton class
        (``class << self``), so those methods are recorded as static.

        ``scope_component`` is the class/module component owning this body, used
        to attach DSL declarations without re-scanning the component list.
        """
        current_visibility = visibility

        for child in container.children:
            if not child.is_named:
                continue

            node_type = child.type

            # Sticky visibility statement: bare ``private`` / ``protected``.
            if node_type == "identifier":
                keyword = self._get_node_text_simple(child)
                if keyword in _VISIBILITY_KEYWORDS:
                    current_visibility = _VISIBILITY_KEYWORDS[keyword]
                continue

            if node_type in _SCOPE_NODES:
                self._extract_scope(child, parent_component, file_path, components)
                continue

            if node_type == "singleton_class":
                # ``class << self`` — no component of its own; its methods are
                # class-level methods of the enclosing class.
                body = child.child_by_field_name("body")
                if body is not None:
                    self._walk_body(
                        body,
                        parent_component,
                        file_path,
                        components,
                        class_level=True,
                        scope_component=scope_component,
                    )
                continue

            if node_type in _METHOD_NODES:
                component = self._extract_method_component(
                    child,
                    file_path,
                    parent_component,
                    current_visibility,
                    class_level=class_level,
                )
                if component:
                    components.append(component)
                    self._walk_nested_definitions(child, component.id, file_path, components)
                continue

            if node_type in ("assignment", "operator_assignment"):
                component = self._extract_value_component(child, file_path, parent_component)
                if component:
                    components.append(component)
                continue

            if node_type == "call":
                self._extract_from_call(
                    child,
                    parent_component,
                    file_path,
                    components,
                    current_visibility,
                    class_level,
                    scope_component,
                )
                continue

            if node_type == "alias":
                component = self._extract_alias_component(
                    child, file_path, parent_component, current_visibility
                )
                if component:
                    components.append(component)
                continue

            # Anything else (conditionals, begin/rescue, ...) may still contain
            # definitions — for example a class defined inside an ``if``.
            self._walk_body(
                child,
                parent_component,
                file_path,
                components,
                class_level,
                current_visibility,
                scope_component,
            )

    def _walk_nested_definitions(
        self,
        method_node: Any,
        parent_component: str,
        file_path: Path,
        components: List[Component],
    ) -> None:
        """Extract definitions nested inside a method body.

        Ruby permits ``class``/``def`` inside a method body. Those are rare but
        real, and skipping them would silently drop components.
        """
        body = method_node.child_by_field_name("body")
        if body is None:
            return
        for child in body.children:
            if not child.is_named:
                continue
            if child.type in _SCOPE_NODES:
                self._extract_scope(child, parent_component, file_path, components)
            elif child.type in _METHOD_NODES:
                component = self._extract_method_component(
                    child, file_path, parent_component, Visibility.PUBLIC, class_level=False
                )
                if component:
                    components.append(component)
                    self._walk_nested_definitions(child, component.id, file_path, components)

    def _extract_scope(
        self,
        node: Any,
        parent_component: Optional[str],
        file_path: Path,
        components: List[Component],
    ) -> None:
        """Extract a ``class``/``module`` component and recurse into its body."""
        component = self._extract_scope_component(node, file_path, parent_component)
        if component is None:
            return
        components.append(component)

        body = node.child_by_field_name("body")
        if body is not None:
            self._walk_body(
                body,
                component.id,
                file_path,
                components,
                class_level=False,
                scope_component=component,
            )

    def _extract_from_call(
        self,
        node: Any,
        parent_component: Optional[str],
        file_path: Path,
        components: List[Component],
        visibility: Visibility,
        class_level: bool,
        scope_component: Optional[Component] = None,
    ) -> None:
        """Handle the call-shaped constructs that define or decorate members.

        Three cases matter: attribute macros (``attr_accessor``) and defining
        macros (``define_method``), which generate real callable methods; and
        inline visibility (``private def foo``), which wraps a definition.
        """
        method_node = node.child_by_field_name("method")
        if method_node is None:
            return
        macro = self._simple_name_of(method_node)
        args = node.child_by_field_name("arguments")

        # ``private def foo; end`` / ``private_class_method def self.bar; end``
        if macro in _INLINE_VISIBILITY and args is not None:
            inline_visibility = _INLINE_VISIBILITY[macro]
            found_definition = False
            for arg in args.children:
                if arg.is_named and arg.type in _METHOD_NODES:
                    found_definition = True
                    component = self._extract_method_component(
                        arg,
                        file_path,
                        parent_component,
                        inline_visibility,
                        class_level=class_level or macro.endswith("_class_method"),
                    )
                    if component:
                        components.append(component)
                        self._walk_nested_definitions(arg, component.id, file_path, components)
            if found_definition:
                return

            # Retroactive form: ``private :foo, :bar`` marks already-defined
            # methods. Walk the symbol arguments and patch the visibility of
            # any previously emitted component with a matching simple name.
            symbol_names = self._symbol_names(args)
            if symbol_names:
                self._remark_visibility(
                    components, parent_component, symbol_names, inline_visibility, file_path
                )
                return

        if macro in _ATTR_MACROS and args is not None:
            for attr_name in self._symbol_names(args):
                for kind in _ATTR_MACROS[macro]:
                    name = attr_name if kind == "get" else f"{attr_name}="
                    component = self._synthesize_method(
                        node,
                        name,
                        file_path,
                        parent_component,
                        visibility,
                        signature=f"{macro} :{attr_name}",
                        parameters=[Parameter(name="value", required=True)]
                        if kind == "set"
                        else [],
                    )
                    if component:
                        components.append(component)
            return

        if macro in _DEFINING_MACROS and args is not None:
            names = self._symbol_names(args)
            # ``alias_method :new_name, :old_name`` defines the *first* symbol.
            if names:
                component = self._synthesize_method(
                    node,
                    names[0],
                    file_path,
                    parent_component,
                    visibility,
                    signature=f"{macro} :{names[0]}",
                )
                if component:
                    components.append(component)
            return

        # Any other bare call directly inside a class/module body is a
        # declaration: a mixin (``include Auditable``) or framework DSL
        # (``has_many``, ``validates``, ``before_action``). These generate
        # behaviour with no AST node of their own, so they cannot become
        # components; recording them on the enclosing scope preserves the signal
        # for summarization without inventing definitions.
        if scope_component is not None and node.child_by_field_name("receiver") is None:
            self._record_declaration(scope_component, node)

    # ── Component builders ───────────────────────────────────────────────────

    def _extract_scope_component(
        self, node: Any, file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Build a CLASS or MODULE component."""
        try:
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return None

            raw_name = self._get_node_text_simple(name_node)
            if not raw_name or raw_name == "unknown":
                return None
            # ``Admin::UsersController`` -> ``Admin.UsersController`` so ids stay
            # dot-delimited and get_simple_name() keeps working.
            name = raw_name.replace("::", ".")

            component_id = self.make_id(file_path, name, parent_component)
            is_module = node.type == "module"

            keyword = "module" if is_module else "class"
            signature = f"{keyword} {raw_name}"
            decorators: List[str] = []

            superclass_node = node.child_by_field_name("superclass")
            if superclass_node is not None:
                superclass = self._superclass_name(superclass_node)
                if superclass:
                    signature = f"{signature} < {superclass}"
                    decorators.append(f"inherits {superclass}")

            attributes = ComponentAttributes(
                signature=signature,
                visibility=Visibility.PUBLIC,
                decorators=decorators,
                line_count=node.end_point[0] - node.start_point[0] + 1,
            )

            return self._create_component(
                component_id,
                ComponentType.MODULE if is_module else ComponentType.CLASS,
                node,
                file_path,
                "ruby",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_method_component(
        self,
        node: Any,
        file_path: Path,
        parent_component: Optional[str],
        visibility: Visibility,
        class_level: bool,
    ) -> Optional[Component]:
        """Build a METHOD, FUNCTION, or CONSTRUCTOR component."""
        try:
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return None

            method_name = self._get_node_text_simple(name_node)
            if not method_name or method_name == "unknown":
                return None

            component_id = self.make_id(file_path, method_name, parent_component)
            parameters = self.extract_function_parameters(node, [])

            # ``def self.foo`` is a singleton method; so is anything inside a
            # ``class << self`` block.
            is_static = class_level or node.type == "singleton_method"
            receiver = "self." if node.type == "singleton_method" else ""
            param_text = ", ".join(self._parameter_signature(p) for p in parameters)
            signature = f"def {receiver}{method_name}" + (f"({param_text})" if parameters else "")

            if method_name == "initialize":
                component_type = ComponentType.CONSTRUCTOR
            elif parent_component:
                component_type = ComponentType.METHOD
            else:
                component_type = ComponentType.FUNCTION

            attributes = ComponentAttributes(
                signature=signature,
                parameters=parameters,
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                is_static=is_static,
            )

            return self._create_component(
                component_id,
                component_type,
                node,
                file_path,
                "ruby",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_value_component(
        self, node: Any, file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Build a CONSTANT or VARIABLE component from an assignment.

        Only constants (``MAX_RETRIES``) and class variables (``@@count``) become
        components. Local variables and instance variables are deliberately
        skipped: Ruby has no type annotations, so they add catalog noise without
        improving resolution, and the flow analyzer already infers their types
        separately for call resolution.
        """
        try:
            left = node.child_by_field_name("left")
            if left is None:
                return None

            if left.type == "constant":
                component_type = ComponentType.CONSTANT
            elif left.type == "class_variable":
                component_type = ComponentType.VARIABLE
            else:
                return None

            raw_name = self._get_node_text_simple(left)
            if not raw_name or raw_name == "unknown":
                return None
            name = raw_name.lstrip("@")

            attributes = ComponentAttributes(
                signature=self._truncate(self._get_node_text_simple(node), 200),
                visibility=Visibility.PUBLIC,
                line_count=node.end_point[0] - node.start_point[0] + 1,
            )

            return self._create_component(
                self.make_id(file_path, name, parent_component),
                component_type,
                node,
                file_path,
                "ruby",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_alias_component(
        self,
        node: Any,
        file_path: Path,
        parent_component: Optional[str],
        visibility: Visibility,
    ) -> Optional[Component]:
        """Build a METHOD component for ``alias new_name old_name``."""
        try:
            named = [c for c in node.children if c.is_named]
            if not named:
                return None
            new_name = self._simple_name_of(named[0])
            if not new_name:
                return None
            old_name = self._simple_name_of(named[1]) if len(named) > 1 else ""
            return self._synthesize_method(
                node,
                new_name,
                file_path,
                parent_component,
                visibility,
                signature=f"alias {new_name} {old_name}".strip(),
            )
        except Exception:
            return None

    def _synthesize_method(
        self,
        node: Any,
        name: str,
        file_path: Path,
        parent_component: Optional[str],
        visibility: Visibility,
        signature: str,
        parameters: Optional[List[Parameter]] = None,
    ) -> Optional[Component]:
        """Build a METHOD component for a macro-generated method.

        The node is the macro call itself, so the reported line range points at
        the declaration — the only location that exists in source.
        """
        try:
            if not name:
                return None
            return self._create_component(
                self.make_id(file_path, name, parent_component),
                ComponentType.METHOD if parent_component else ComponentType.FUNCTION,
                node,
                file_path,
                "ruby",
                ComponentAttributes(
                    signature=signature,
                    parameters=parameters or [],
                    visibility=visibility,
                    line_count=node.end_point[0] - node.start_point[0] + 1,
                ),
                parent_component,
            )
        except Exception:
            return None

    def _remark_visibility(
        self,
        components: List[Component],
        parent_component: Optional[str],
        names: List[str],
        visibility: Visibility,
        file_path: Path,
    ) -> None:
        """Retroactively set visibility on already-emitted methods by name.

        Handles Ruby's ``private :foo, :bar`` form which marks methods that were
        defined earlier in the class body.
        """
        target_ids = {self.make_id(file_path, n, parent_component) for n in names} if parent_component else set()
        if not target_ids:
            return
        for comp in components:
            if comp.id in target_ids:
                comp.attributes.visibility = visibility

    def _record_declaration(self, scope_component: Component, node: Any) -> None:
        """Append a DSL/mixin declaration to the enclosing scope's decorators."""
        try:
            decorators = scope_component.attributes.decorators
            if len(decorators) >= _MAX_DECLARATIONS:
                return
            text = self._truncate(
                " ".join(self._get_node_text_simple(node).split()),
                _MAX_DECLARATION_CHARS,
            )
            if text and text != "unknown":
                decorators.append(text)
        except Exception:
            return

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _bare_identifier_call_name(self, node: Any, parent: Any) -> Optional[str]:
        """Return a call name for a bare identifier, or ``None`` to ignore it.

        A bare identifier is treated as a paren-less call only when all of the
        following hold:

        1. it occupies a statement or condition slot (see
           :data:`_STATEMENT_PARENTS`), or is the right-hand side of an
           assignment — ``total = compute_total`` is a call, ``total`` on the
           left is not;
        2. it is not a language construct (see :data:`_NON_CALL_IDENTIFIERS`);
        3. it is not a parameter of an enclosing method or block;
        4. it was not assigned earlier in the enclosing scope.

        Rules 3 and 4 are what stop a local variable read from being mistaken
        for a call to a same-named method — the realistic failure mode, since
        ``attr_reader :size`` and a local named ``size`` collide readily.
        """
        if node.type != "identifier" or parent is None:
            return None

        if parent.type in ("assignment", "operator_assignment"):
            left = parent.child_by_field_name("left")
            if left is not None and left.id == node.id:
                return None
        elif parent.type not in _STATEMENT_PARENTS:
            return None

        name = self._simple_name_of(node)
        if name is None or name in _NON_CALL_IDENTIFIERS:
            return None
        if self._is_local_read(node, name):
            return None
        return name

    def _is_local_read(self, node: Any, name: str) -> bool:
        """Report whether ``name`` refers to a local variable rather than a method.

        Walks the ancestor chain for a matching parameter, then scans the
        enclosing scope for an earlier assignment. The scan is bounded by the
        enclosing method body, which keeps it cheap in practice.
        """
        try:
            scope = None
            ancestor = node.parent
            while ancestor is not None:
                if ancestor.type in _METHOD_NODES or ancestor.type in _BLOCK_NODES:
                    if self._has_parameter_named(ancestor, name):
                        return True
                if ancestor.type in _SCOPE_BOUNDARIES:
                    scope = ancestor
                    break
                ancestor = ancestor.parent

            if scope is None:
                return False
            return self._assigned_at_or_before(scope, name, node.start_point[0])
        except Exception:
            return False

    def _has_parameter_named(self, scope_node: Any, name: str) -> bool:
        """Report whether a method or block declares a parameter called ``name``."""
        params_node = scope_node.child_by_field_name("parameters")
        if params_node is None:
            return False
        for child in params_node.children:
            if not child.is_named:
                continue
            if child.type == "identifier":
                if self._get_node_text_simple(child) == name:
                    return True
            elif child.type in _WRAPPED_PARAM_TYPES:
                name_node = child.child_by_field_name("name")
                if name_node is not None and self._get_node_text_simple(name_node) == name:
                    return True
        return False

    def _assigned_at_or_before(self, scope_node: Any, name: str, row: int) -> bool:
        """Report whether ``name`` is an assignment target at or above ``row``."""
        stack = [scope_node]
        while stack:
            current = stack.pop()
            if current.type in ("assignment", "operator_assignment"):
                left = current.child_by_field_name("left")
                if (
                    left is not None
                    and left.type == "identifier"
                    and left.start_point[0] <= row
                    and self._get_node_text_simple(left) == name
                ):
                    return True
            for child in current.children:
                if child.is_named and child.start_point[0] <= row:
                    stack.append(child)
        return False

    def _simple_name_of(self, node: Any) -> Optional[str]:
        """Text of an identifier-like node, or ``None`` when unavailable."""
        if node is None:
            return None
        text = self._get_node_text_simple(node)
        if not text or text == "unknown":
            return None
        return text

    def _receiver_key(self, receiver_node: Any, depth: int = 0) -> Optional[str]:
        """Build the lookup key for a call receiver.

        Instance and class variables keep their sigils (``@gateway``) so the key
        matches what :mod:`codekb.flow.ruby_flow_support` records for them.

        A chained receiver (``invoice.total`` in ``invoice.total.positive?``) is
        expanded into its own dotted path, matching how the Python parser renders
        ``a.b.c()``. The resulting key will not resolve, which is the point: it
        produces no edge rather than a bogus edge to any same-named method.
        """
        if receiver_node.type in ("identifier", "constant", "instance_variable", "class_variable"):
            return self._simple_name_of(receiver_node)
        if receiver_node.type == "scope_resolution":
            name = self._simple_name_of(receiver_node)
            return name.replace("::", ".") if name else None
        if receiver_node.type == "self":
            return "self"
        if receiver_node.type == "call" and depth < 8:
            inner_method = receiver_node.child_by_field_name("method")
            inner_name = self._simple_name_of(inner_method) if inner_method is not None else None
            if not inner_name:
                return None
            inner_receiver = receiver_node.child_by_field_name("receiver")
            if inner_receiver is None:
                return inner_name
            inner_key = self._receiver_key(inner_receiver, depth + 1)
            return f"{inner_key}.{inner_name}" if inner_key else None
        return None

    def _superclass_name(self, superclass_node: Any) -> Optional[str]:
        """Extract the parent class name from a ``superclass`` node (``< Foo``)."""
        for child in superclass_node.children:
            if child.is_named and child.type in ("constant", "scope_resolution"):
                return self._simple_name_of(child)
        return None

    def _symbol_names(self, args_node: Any) -> List[str]:
        """Collect symbol argument names, without the leading colon.

        ``attr_accessor :first_name, :last_name`` -> ``["first_name", "last_name"]``.
        """
        names: List[str] = []
        for child in args_node.children:
            if not child.is_named:
                continue
            if child.type in ("simple_symbol", "symbol"):
                text = self._get_node_text_simple(child)
                if text and text != "unknown":
                    names.append(text.lstrip(":"))
            elif child.type == "string":
                # ``attr_accessor "name"`` is legal too.
                content = "".join(
                    self._get_node_text_simple(part)
                    for part in child.children
                    if part.type == "string_content"
                )
                if content:
                    names.append(content)
        return names

    def _decorate_param_name(self, param_node: Any, name: Optional[str]) -> str:
        """Render a parameter name with the sigil its form implies.

        Keeping ``*rest`` / ``**opts`` / ``&block`` / ``kw:`` distinguishable in
        the signature matters because Ruby's arity rules depend on it.
        """
        base = name or ""
        node_type = param_node.type
        if node_type == "keyword_parameter":
            return f"{base}:" if base else ":"
        if node_type == "splat_parameter":
            return f"*{base}"
        if node_type == "hash_splat_parameter":
            return f"**{base}"
        if node_type == "block_parameter":
            return f"&{base}"
        if node_type == "forward_parameter":
            return "..."
        return base

    def _parameter_signature(self, parameter: Parameter) -> str:
        """Render one parameter for a method signature."""
        if parameter.default_value is None:
            return parameter.name
        separator = " " if parameter.name.endswith(":") else " = "
        return f"{parameter.name}{separator}{parameter.default_value}"

    def _truncate(self, text: str, limit: int) -> str:
        """Clamp text to ``limit`` characters with an ellipsis."""
        if text and len(text) > limit:
            return text[: limit - 1] + "…"
        return text
