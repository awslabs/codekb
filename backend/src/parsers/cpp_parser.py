"""
C / C++ parser implementation (shared).

Handles the constructs that make C++ harder than every other language the
toolkit supports:

* Section-based access specifiers (`public:` / `private:` / `protected:`)
  apply to every member that follows until the next specifier, rather than
  being a per-declaration modifier.
* Out-of-line member definitions (`void app::Foo::bar() {}` living in a .cpp)
  are attributed to their owning type by parsing the qualified name, not by
  lexical nesting.
* The function name is buried inside a `function_declarator`, itself possibly
  wrapped in `pointer_declarator` / `reference_declarator` (e.g. an operator
  that returns a reference), so the declarator chain must be unwrapped.
* Bodyless prototypes (in headers / forward declarations) are deduplicated
  against their real definitions so a function is not double-counted, and the
  definition (which carries the body / call graph) wins.

The same class parses C too: C source simply never exercises the C++-only
branches (namespaces, classes, templates, access specifiers).
"""

import hashlib
import re
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path

from .base_parser import BaseLanguageParser
from ..core.models import (
    Component,
    ComponentType,
    ComponentAttributes,
    Parameter,
    Visibility,
    IdScheme,
    DISAMBIG_SEP,
)


# Type-container declarations.
_TYPE_CONTAINER_NODES = {
    "class_specifier",
    "struct_specifier",
    "union_specifier",
    "enum_specifier",
}

# Declarator wrappers that sit between a declaration and the real name/function
# declarator (`*p`, `&r`, `(*fp)`, `arr[]`).
_DECLARATOR_WRAPPERS = {
    "pointer_declarator",
    "reference_declarator",
    "parenthesized_declarator",
    "array_declarator",
}

# Nodes that can appear as the "name" of a function declarator.
_NAME_NODES = {
    "identifier",
    "field_identifier",
    "type_identifier",
    "qualified_identifier",
    "operator_name",
    "destructor_name",
}

# A name in capital letters is a macro (as `_CLASS_MACRO_RE` in tree_sitter_wrapper).
_MACRO_NAME = re.compile(r"[A-Z_][A-Z0-9_]+")


class CppParser(BaseLanguageParser):
    """C / C++-specific parser implementation."""

    def __init__(self) -> None:
        # Names of every namespace declared in the current file, used to decide
        # whether the leading segment of an out-of-line qualifier (`app::Foo`)
        # is a namespace (don't stem-prefix) or a file-scope type (stem-prefix),
        # so out-of-line definition IDs match their in-class declaration IDs.
        self._namespaces: set[str] = set()

    # ------------------------------------------------------------------
    # BaseLanguageParser contract
    # ------------------------------------------------------------------

    def get_call_node_types(self) -> List[str]:
        """AST node types that represent calls in C/C++."""
        return ["call_expression", "new_expression"]

    def get_function_node_types(self) -> List[str]:
        """AST node types that represent function definitions (with a body)."""
        return ["function_definition"]

    def extract_call_name(self, function_node: Any) -> Optional[str]:
        """Extract a callee name from the function part of a call.

        Handles:
        - identifier                 : foo()            -> "foo"
        - field_expression           : obj.m() / p->m() -> "obj.m"
        - qualified_identifier       : app::foo()       -> "app.foo"
        - template_function          : foo<T>()         -> "foo"
        - type_identifier            : new Foo()        -> "Foo"
        - parenthesized_expression   : (expr)()         -> recurse
        - call_expression            : foo()()          -> recurse
        """
        if function_node is None:
            return None

        node_type = function_node.type

        if node_type in ("identifier", "field_identifier", "type_identifier"):
            return self._get_node_text_simple(function_node)

        if node_type == "field_expression":
            # argument (. | -> | .*) field
            arg = function_node.child_by_field_name("argument")
            field = function_node.child_by_field_name("field")
            if arg is not None and field is not None:
                obj = self._strip_generics(self._get_node_text_simple(arg))
                member = self._strip_generics(self._get_node_text_simple(field))
                return f"{obj}.{member}"
            return None

        if node_type == "qualified_identifier":
            # `a::b::c` -> `a.b.c`
            return self._strip_generics(
                self._get_node_text_simple(function_node).replace("::", ".")
            )

        if node_type in ("template_function", "template_method"):
            name = function_node.child_by_field_name("name")
            if name is not None:
                return self.extract_call_name(name)
            return None

        if node_type == "parenthesized_expression":
            for child in function_node.children:
                if child.type not in ("(", ")"):
                    return self.extract_call_name(child)
            return None

        if node_type == "call_expression":
            fn = function_node.child_by_field_name("function")
            return self.extract_call_name(
                fn or (function_node.children[0] if function_node.children else None)
            )

        return None

    def extract_components(
        self, root_node: Any, source_code: bytes, file_path: Path
    ) -> List[Component]:
        """Extract components from a C/C++ syntax tree."""
        source_lines = source_code.decode("utf-8", "replace").split("\n")
        self._file_path = file_path
        # Label components by the language actually routed to this parser. The
        # CppParser is shared by both "c" and "cpp"; the extension determines
        # which grammar the wrapper used (see default.yaml: .c -> c, all other
        # C-family extensions incl. .h -> cpp), so mirror that here. Deriving the
        # label (rather than hardcoding "cpp") keeps per-language metrics honest
        # for pure-C repos and keeps the flow analyzer re-parsing with the same
        # grammar the catalog was built from.
        self._lang = "c" if file_path.suffix.lower() == ".c" else "cpp"
        self._namespaces = set()
        self._collect_namespaces(root_node)
        # Per-file map of a V2 disambiguated id -> the exact type signature it
        # stands for. Lets _build_function_component detect a short-hash
        # collision between two DIFFERENT signatures (and widen the hash) while
        # still giving a prototype and its definition — same signature — the
        # same id so they merge.
        self._cpp_disc_sigs: Dict[str, str] = {}

        components: List[Component] = []
        by_id: Dict[str, int] = {}

        def add(component: Optional[Component], is_definition: bool) -> None:
            """Append a component, deduplicating prototypes against definitions.

            When a definition arrives for an id already held by a bodyless
            declaration, the definition replaces it but inherits the
            declaration's visibility (the in-class access section is the
            authoritative source of visibility; the out-of-line definition is
            not).
            """
            if component is None:
                return
            existing_idx = by_id.get(component.id)
            if existing_idx is None:
                by_id[component.id] = len(components)
                components.append(component)
                setattr(component, "_cpp_is_def", is_definition)
                return
            existing = components[existing_idx]
            existing_is_def = getattr(existing, "_cpp_is_def", True)
            if is_definition and not existing_is_def:
                # Definition wins; inherit modifiers the out-of-line definition
                # cannot restate (visibility comes from the in-class access
                # section; static/abstract/final are declared in the header).
                if existing.attributes and component.attributes:
                    component.attributes.visibility = existing.attributes.visibility
                    component.attributes.is_static = (
                        component.attributes.is_static or existing.attributes.is_static
                    )
                    component.attributes.is_abstract = (
                        component.attributes.is_abstract or existing.attributes.is_abstract
                    )
                    component.attributes.is_final = (
                        component.attributes.is_final or existing.attributes.is_final
                    )
                setattr(component, "_cpp_is_def", True)
                components[existing_idx] = component
            elif (
                is_definition
                and self._scheme == IdScheme.V2_PATH
                and component.type
                in (ComponentType.FUNCTION, ComponentType.METHOD, ComponentType.CONSTRUCTOR)
            ):
                # Two definitions with one id in one file (`#if`/`#else` branches,
                # overloads that only a template constraint tells apart) both stay;
                # the later one gets a number, as `_disambiguate_overloads` does.
                number = 2
                while f"{component.id}{number}" in by_id:
                    number += 1
                component.id = f"{component.id}{number}"
                by_id[component.id] = len(components)
                components.append(component)
                setattr(component, "_cpp_is_def", True)
            # else: keep the existing component (first definition / existing def).

        def make_id(parent: Optional[str], name: str) -> str:
            return self.make_id(file_path, name, parent)

        def parent_from_qualifier(
            segments: List[str], parent: Optional[str] = None
        ) -> Optional[str]:
            """Turn a qualifier like ['app','Foo'] into a parent component id.

            With the enclosing namespace ``parent``, the qualifier resolves from
            there, as in C++: `logger::flush` in `namespace spdlog` and
            `spdlog::logger::flush` both name the class `spdlog.logger`.
            """
            if not segments:
                return None
            if parent:
                enclosing = parent.split(".")
                if segments[0] in enclosing:
                    start = len(enclosing) - enclosing[::-1].index(segments[0]) - 1
                    return ".".join(enclosing[:start] + segments)
                return f"{parent}.{'.'.join(segments)}"
            if segments[0] in self._namespaces:
                return ".".join(segments)
            return self.make_id(file_path, ".".join(segments))

        def walk(node: Any, parent: Optional[str], visibility: Visibility) -> None:
            node_type = node.type

            if node_type == "namespace_definition":
                name_node = node.child_by_field_name("name")
                ns_name = (
                    self._get_node_text(name_node, source_lines).replace("::", ".")
                    if name_node is not None
                    else None
                )
                ns_id = ns_name if not parent else (f"{parent}.{ns_name}" if ns_name else parent)
                body = node.child_by_field_name("body")
                if body is not None:
                    for child in body.children:
                        walk(child, ns_id, Visibility.PUBLIC)
                return

            if node_type == "template_declaration":
                # Unwrap: recurse into the templated class/function/declaration.
                for child in node.children:
                    if child.type in _TYPE_CONTAINER_NODES or child.type in (
                        "function_definition",
                        "declaration",
                        "field_declaration",
                    ):
                        walk(child, parent, visibility)
                return

            if node_type in _TYPE_CONTAINER_NODES:
                comp = self._make_type_component(node, source_lines, parent, make_id)
                if comp is not None:
                    add(comp, True)
                    self._walk_type_body(node, comp.id, source_lines, file_path, make_id, add)
                return

            if node_type == "function_definition":
                self._handle_function_like(
                    node,
                    parent,
                    visibility,
                    source_lines,
                    file_path,
                    make_id,
                    parent_from_qualifier,
                    add,
                    is_definition=True,
                )
                return

            if node_type == "type_definition":
                # `typedef struct { ... } Name;` — emit the (possibly anonymous)
                # aggregate under the typedef name. Plain aliases are skipped.
                self._handle_typedef(node, parent, visibility, source_lines, make_id, add)
                return

            if node_type == "declaration":
                # Either a free-function prototype or a (possibly function-pointer)
                # variable. A function_declarator whose inner declarator is a
                # parenthesized_declarator is a function *pointer*, not a function.
                func_decl = self._find_function_declarator(node)
                if func_decl is not None and not self._is_function_pointer(func_decl):
                    self._handle_function_like(
                        node,
                        parent,
                        visibility,
                        source_lines,
                        file_path,
                        make_id,
                        parent_from_qualifier,
                        add,
                        is_definition=False,
                    )
                else:
                    for comp in self._make_variable_components(
                        node, source_lines, parent, visibility, make_id
                    ):
                        add(comp, True)
                return

            # Recurse into anything else (linkage specs, extern "C" blocks, etc.)
            for child in node.children:
                walk(child, parent, visibility)

        walk(root_node, None, Visibility.PUBLIC)

        for comp in components:
            if hasattr(comp, "_cpp_is_def"):
                delattr(comp, "_cpp_is_def")
        return components

    def extract_function_parameters(
        self, function_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """Extract parameters from a C/C++ function-like node."""
        parameters: List[Parameter] = []
        func_decl = self._find_function_declarator(function_node)
        if func_decl is None:
            return parameters

        params_node = func_decl.child_by_field_name("parameters")
        if params_node is None:
            return parameters

        for child in params_node.children:
            if child.type == "parameter_declaration":
                name = self._param_name(child, source_lines)
                if name:
                    parameters.append(Parameter(name=name, required=True))
            elif child.type == "optional_parameter_declaration":
                name = self._param_name(child, source_lines)
                if name:
                    parameters.append(Parameter(name=name, required=False))
            elif child.type == "variadic_parameter_declaration":
                name = self._param_name(child, source_lines)
                if name:
                    parameters.append(Parameter(name=name, required=False))
        return parameters

    # ------------------------------------------------------------------
    # Type containers
    # ------------------------------------------------------------------

    def _make_type_component(
        self, node: Any, source_lines: List[str], parent: Optional[str], make_id
    ) -> Optional[Component]:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None  # anonymous struct/union/enum -> skip
        name = self._get_node_text(name_node, source_lines)
        if not name:
            return None

        component_id = make_id(parent, name)
        component_type = (
            ComponentType.ENUM if node.type == "enum_specifier" else ComponentType.CLASS
        )
        attributes = ComponentAttributes(
            signature=self._signature(node, source_lines),
            visibility=Visibility.PUBLIC,
            line_count=node.end_point[0] - node.start_point[0] + 1,
        )
        return self._create_component(
            component_id, component_type, node, self._file_path, self._lang, attributes, parent
        )

    def _walk_type_body(
        self,
        type_node: Any,
        type_id: str,
        source_lines: List[str],
        file_path: Path,
        make_id,
        add,
    ) -> None:
        """Walk a class/struct/union body honoring section-based access."""
        body = type_node.child_by_field_name("body")
        if body is None:
            return

        # class -> private by default; struct / union -> public.
        current_vis = (
            Visibility.PRIVATE if type_node.type == "class_specifier" else Visibility.PUBLIC
        )

        for child in body.children:
            ctype = child.type
            if ctype == "access_specifier":
                text = self._get_node_text(child, source_lines).strip()
                current_vis = {
                    "public": Visibility.PUBLIC,
                    "protected": Visibility.PROTECTED,
                    "private": Visibility.PRIVATE,
                }.get(text, current_vis)
                continue

            if ctype in _TYPE_CONTAINER_NODES:
                comp = self._make_type_component(child, source_lines, type_id, make_id)
                if comp is not None:
                    comp.attributes.visibility = current_vis
                    add(comp, True)
                    self._walk_type_body(child, comp.id, source_lines, file_path, make_id, add)
                continue

            if ctype == "template_declaration":
                for gc in child.children:
                    if gc.type in ("function_definition", "field_declaration", "declaration"):
                        self._emit_member(gc, type_id, current_vis, source_lines, make_id, add)
                    elif gc.type in _TYPE_CONTAINER_NODES:
                        comp = self._make_type_component(gc, source_lines, type_id, make_id)
                        if comp is not None:
                            comp.attributes.visibility = current_vis
                            add(comp, True)
                            self._walk_type_body(gc, comp.id, source_lines, file_path, make_id, add)
                continue

            if ctype in ("field_declaration", "declaration", "function_definition"):
                self._emit_member(child, type_id, current_vis, source_lines, make_id, add)
                continue

            if ctype == "type_definition":
                self._handle_typedef(child, type_id, current_vis, source_lines, make_id, add)

    def _emit_member(
        self,
        node: Any,
        type_id: str,
        visibility: Visibility,
        source_lines: List[str],
        make_id,
        add,
    ) -> None:
        """Emit a single class member (method or field)."""
        func_decl = self._find_function_declarator(node)
        if func_decl is not None and not self._is_function_pointer(func_decl):
            comp = self._make_method_component(
                node, func_decl, type_id, visibility, source_lines, make_id
            )
            if comp is not None:
                add(comp, node.type == "function_definition")
            return
        # Not a function (or a function-pointer member) -> member variable(s).
        for comp in self._make_variable_components(
            node, source_lines, type_id, visibility, make_id, member=True
        ):
            add(comp, True)

    # ------------------------------------------------------------------
    # Functions / methods
    # ------------------------------------------------------------------

    def _handle_function_like(
        self,
        node: Any,
        parent: Optional[str],
        visibility: Visibility,
        source_lines: List[str],
        file_path: Path,
        make_id,
        parent_from_qualifier,
        add,
        is_definition: bool,
    ) -> None:
        """Handle a file/namespace-scope function definition or prototype.

        Distinguishes free functions from out-of-line member definitions by
        inspecting the (possibly qualified) declarator name.
        """
        func_decl = self._find_function_declarator(node)
        if func_decl is None:
            return
        if self._scheme == IdScheme.V2_PATH:
            qualifier, simple_name = self._qualified_name(
                func_decl, source_lines, self._return_type(node, source_lines)
            )
        else:  # V1 keeps the text of the name, so its ids do not change.
            segments = (self._declarator_name(func_decl, source_lines) or "").split("::")
            qualifier, simple_name = [s for s in segments[:-1] if s], segments[-1]
        if not simple_name:
            return

        if qualifier:
            # Qualified name: either an out-of-line member definition
            # (`app::Foo::bar`) or a namespace-scoped free function
            # (`app::freeHelper`). It's a member only when the segment directly
            # before the name is a type rather than a namespace.
            owner_is_type = qualifier[-1] not in self._namespaces
            scope_parent = parent_from_qualifier(qualifier)

            if owner_is_type:
                if simple_name == qualifier[-1].split("<", 1)[0]:  # `Box<int>::Box`
                    comp_type = ComponentType.CONSTRUCTOR
                else:
                    comp_type = ComponentType.METHOD
                component_id = (
                    f"{scope_parent}.{simple_name}"
                    if scope_parent
                    else make_id(parent, simple_name)
                )
                # The definition keeps its file-local id and hangs under the class.
                comp = self._build_function_component(
                    node,
                    func_decl,
                    component_id,
                    comp_type,
                    parent_from_qualifier(qualifier, parent) or parent,
                    Visibility.PUBLIC,
                    source_lines,
                )
                add(comp, is_definition)
                return

            # All-namespace qualifier -> free function living in that namespace.
            component_id = (
                f"{scope_parent}.{simple_name}" if scope_parent else make_id(parent, simple_name)
            )
            comp = self._build_function_component(
                node,
                func_decl,
                component_id,
                ComponentType.FUNCTION,
                scope_parent or parent,
                visibility,
                source_lines,
            )
            add(comp, is_definition)
            return

        # Free function (or prototype).
        component_id = make_id(parent, simple_name)
        comp = self._build_function_component(
            node,
            func_decl,
            component_id,
            ComponentType.FUNCTION,
            parent,
            visibility,
            source_lines,
        )
        add(comp, is_definition)

    def _make_method_component(
        self,
        node: Any,
        func_decl: Any,
        type_id: str,
        visibility: Visibility,
        source_lines: List[str],
        make_id,
    ) -> Optional[Component]:
        raw_name = self._declarator_name(func_decl, source_lines)
        if not raw_name:
            return None
        # In-class declarator is normally simple, but be defensive about `::`.
        simple_name = raw_name.split("::")[-1]
        class_simple = type_id.split(".")[-1]

        if simple_name == class_simple:
            comp_type = ComponentType.CONSTRUCTOR
        else:
            comp_type = ComponentType.METHOD

        component_id = f"{type_id}.{simple_name}"
        return self._build_function_component(
            node, func_decl, component_id, comp_type, type_id, visibility, source_lines
        )

    def _build_function_component(
        self,
        node: Any,
        func_decl: Any,
        component_id: str,
        comp_type: ComponentType,
        parent: Optional[str],
        visibility: Visibility,
        source_lines: List[str],
    ) -> Component:
        parameters = self.extract_function_parameters(node, source_lines)
        attributes = ComponentAttributes(
            signature=self._signature(node, source_lines),
            parameters=parameters,
            return_type=self._return_type(node, source_lines),
            visibility=visibility,
            line_count=node.end_point[0] - node.start_point[0] + 1,
            is_static=self._has_child_type(node, "storage_class_specifier", "static", source_lines),
            is_abstract=self._is_pure_virtual(node),
            is_final=self._has_virtual_specifier(func_decl, "final", source_lines),
        )

        # Encode the parameter-TYPE signature into the id (V2 only). C++ alone
        # separates a declaration (header prototype) from its definition
        # (source), so the same function appears twice and must merge — which is
        # why identity is name+parent, not name+signature. But that same key
        # conflates genuine OVERLOADS (`f(int)` vs `f(int,int)`), dropping all
        # but one. The type signature is the discriminator the language itself
        # uses: it is IDENTICAL for a prototype and its definition (parameter
        # names may differ or be omitted — only types are compared) yet DIFFERS
        # across overloads. So decl+def of one overload still share an id and
        # merge, while overloads get distinct ids. The hash follows DISAMBIG_SEP,
        # so `Component.get_simple_name` recovers the bare name from the id.
        #
        # Gated to V2: legacy V1 spaces keep their original ids so delta-refresh
        # regenerates ids that still match the persisted ones (the per-space
        # scheme contract). They adopt this on a deliberate full V2 reindex.
        if self._scheme == IdScheme.V2_PATH:
            sig = self._param_type_signature(func_decl)
            disambiguated_id = self._disambiguated_cpp_id(component_id, sig)
            return self._create_component(
                disambiguated_id,
                comp_type,
                node,
                self._file_path,
                self._lang,
                attributes,
                parent,
            )

        return self._create_component(
            component_id, comp_type, node, self._file_path, self._lang, attributes, parent
        )

    def _disambiguated_cpp_id(self, component_id: str, sig: str) -> str:
        """Return `component_id` suffixed with a hash of its type signature.

        The suffix starts at 4 hex chars but is widened on demand: if two
        DIFFERENT signatures under the same `component_id` would collide on the
        short hash, the later one grows the hash until it is unique. A prototype
        and its definition carry the SAME signature, so they resolve to the SAME
        suffix and still merge — only genuinely distinct signatures are forced
        apart. Without this, a 16-bit truncation collision would make two
        overloads share an id and silently merge, the very loss this fixes.
        """
        full = hashlib.sha1(sig.encode("utf-8")).hexdigest()
        width = 4
        while True:
            candidate = f"{component_id}{DISAMBIG_SEP}{full[:width]}"
            seen = self._cpp_disc_sigs.get(candidate)
            if seen is None or seen == sig:
                self._cpp_disc_sigs[candidate] = sig
                return candidate
            if width >= len(full):  # exhausted (practically impossible)
                self._cpp_disc_sigs[candidate] = sig
                return candidate
            width += 1

    def _param_type_signature(self, func_decl: Any) -> str:
        """Name-independent parameter-type signature for overload identity.

        Each parameter is reduced to its type text with BOTH the parameter name
        and any default value removed, so a prototype and its definition yield
        the same signature while distinct overloads do not. In C++ a default
        argument is written on the declaration (`void f(int x = 5);`) but not on
        the definition (`void f(int x) {}`); stripping the `= ...` initializer
        keeps the two signatures identical so they still merge. Whitespace is
        normalized; parameters are joined by ``,``.
        """
        params_node = func_decl.child_by_field_name("parameters")
        if params_node is None:
            return ""
        parts: List[str] = []
        for child in params_node.children:
            if child.type not in (
                "parameter_declaration",
                "optional_parameter_declaration",
                "variadic_parameter_declaration",
            ):
                continue
            text = child.text or b""
            base = child.start_byte
            # Byte ranges (relative to the parameter) to drop: the name, and the
            # default value from its `=` to the end. Applied high-offset-first so
            # earlier offsets stay valid.
            drops: List[tuple] = []
            decl = child.child_by_field_name("declarator")
            name_node = self._innermost_identifier_node(decl) if decl is not None else None
            if name_node is not None:
                drops.append((name_node.start_byte - base, name_node.end_byte - base))
            eq = next((c for c in child.children if c.type == "="), None)
            if eq is not None:
                drops.append((eq.start_byte - base, len(text)))
            for start, end in sorted(drops, reverse=True):
                if 0 <= start <= end <= len(text):
                    text = text[:start] + text[end:]
            parts.append(" ".join(text.decode("utf-8", "replace").split()))
        return ",".join(parts)

    def _innermost_identifier_node(self, node: Any) -> Optional[Any]:
        """Node-returning twin of `_innermost_identifier`: the leaf param name."""
        cur = node
        seen = 0
        while cur is not None and seen < 12:
            seen += 1
            if cur.type in _NAME_NODES:
                return cur
            if cur.type in ("template_function", "template_method"):
                inner = cur.child_by_field_name("name")
                if inner is not None:
                    return inner
            nxt = cur.child_by_field_name("declarator")
            if nxt is None:
                nxt = self._wrapper_inner(cur) if cur.type in _DECLARATOR_WRAPPERS else None
            if nxt is None:
                for ch in cur.children:
                    if ch.type in _NAME_NODES:
                        return ch
                return None
            cur = nxt
        return None

    # ------------------------------------------------------------------
    # Variables / fields / constants
    # ------------------------------------------------------------------

    def _make_variable_components(
        self,
        node: Any,
        source_lines: List[str],
        parent: Optional[str],
        visibility: Visibility,
        make_id,
        member: bool = False,
    ) -> List[Component]:
        is_const = self._has_child_type(
            node, "type_qualifier", "const", source_lines
        ) or self._has_child_type(node, "type_qualifier", "constexpr", source_lines)
        is_static = self._has_child_type(node, "storage_class_specifier", "static", source_lines)

        results: List[Component] = []
        for decl in node.children:
            name = None
            if decl.type in ("identifier", "field_identifier"):
                name = self._get_node_text(decl, source_lines)
            elif decl.type == "init_declarator":
                inner = decl.child_by_field_name("declarator")
                name = self._innermost_identifier(inner, source_lines)
            elif decl.type in _DECLARATOR_WRAPPERS or decl.type == "function_declarator":
                # `function_declarator` here is a function *pointer* variable
                # (`int (*cb)(int)`), not a function.
                name = self._innermost_identifier(decl, source_lines)
            if not name:
                continue

            component_id = make_id(parent, name)
            comp_type = ComponentType.CONSTANT if is_const else ComponentType.VARIABLE
            attributes = ComponentAttributes(
                signature=self._signature(node, source_lines),
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                is_static=is_static,
                is_final=is_const,
            )
            results.append(
                self._create_component(
                    component_id, comp_type, node, self._file_path, self._lang, attributes, parent
                )
            )
        return results

    # ------------------------------------------------------------------
    # Declarator / name helpers
    # ------------------------------------------------------------------

    def _wrapper_inner(self, node: Any) -> Optional[Any]:
        """Return the inner declarator of a wrapper node.

        Some wrappers expose it as the `declarator` field (pointer_declarator),
        others only as a positional child (reference_declarator: `& <decl>`).
        """
        inner = node.child_by_field_name("declarator")
        if inner is not None:
            return inner
        for ch in node.children:
            if (
                ch.type in _DECLARATOR_WRAPPERS
                or ch.type == "function_declarator"
                or ch.type in _NAME_NODES
                or ch.type in ("template_function", "template_method")
            ):
                return ch
        return None

    def _find_function_declarator(self, node: Any) -> Optional[Any]:
        """Follow the `declarator` chain to the function_declarator."""
        cur = node.child_by_field_name("declarator")
        seen = 0
        while cur is not None and seen < 10:
            seen += 1
            if cur.type == "function_declarator":
                return cur
            if cur.type in _DECLARATOR_WRAPPERS:
                cur = self._wrapper_inner(cur)
            else:
                return None
        return None

    def _qualified_name(
        self, func_decl: Any, source_lines: List[str], return_type: Optional[str]
    ) -> Tuple[List[str], Optional[str]]:
        """Return (scopes, name) of a function_declarator: `Box<T>::get` gives (['Box'], 'get').

        tree-sitter cannot expand a macro. With a macro before the name, it reads the
        return type as scopes (`MACRO std::string Foo::bar`): this method drops the
        scopes before a `::` that the source does not have, and takes the scope the
        source wrote from an ERROR node. With a macro after the name (`thread::get_id
        BOOST_PREVENT_MACRO_SUBSTITUTION ()`), the word before the missing `::` is
        the name, unless the return type is a macro too (`DECL ns::type NAME()`).
        """
        segments: List[str] = []
        node = func_decl.child_by_field_name("declarator")
        qualified = node is not None and node.type == "qualified_identifier"
        while node is not None and node.type == "qualified_identifier":
            separator = next((child for child in node.children if child.type == "::"), None)
            error = self._misplaced_name(node, source_lines)
            scope = node.child_by_field_name("scope")
            if separator is None or separator.is_missing:
                after = node.child_by_field_name("name")
                if (
                    scope is not None
                    and scope.type == "namespace_identifier"
                    and after is not None
                    and _MACRO_NAME.fullmatch(self._get_node_text(after, source_lines))
                    and not _MACRO_NAME.fullmatch(return_type or "")
                ):
                    return segments, self._get_node_text(scope, source_lines)
                segments = []
            elif error is not None:
                segments = [error]
            elif scope is not None:
                segments.append(self._scope_name(scope, source_lines))
            node = node.child_by_field_name("name")
        if node is None:
            return segments, None
        if node.type in ("template_function", "template_method"):
            # A qualified name keeps its arguments: `get<array>` and `get<object>`.
            if qualified:
                return segments, self._get_node_text(node, source_lines)
            node = node.child_by_field_name("name")
        return segments, self._innermost_identifier(node, source_lines)

    def _scope_name(self, scope: Any, source_lines: List[str]) -> str:
        """Name of one scope. `Box<T>::get` in `template <class T>` is a member of `Box`.

        A scope keeps template arguments that are not the enclosing template's
        parameters, so `Box<int>::get` and `Box<T*>::get` stay members of the
        specializations. Under `template <>`, the definition specializes a member
        of the primary template (`Box<double>::get`), which stays its owner. A
        macro in place of the template name (`MACRO Box<T>::get`) puts the name
        the source wrote in an ERROR node.
        """
        error = self._misplaced_name(scope, source_lines)
        if error is not None:
            return error
        name = scope.child_by_field_name("name")
        arguments = scope.child_by_field_name("arguments")
        if scope.type == "template_type" and name is not None and arguments is not None:
            given = [
                self._get_node_text(argument, source_lines).removesuffix("...").strip()
                for argument in arguments.named_children
                if argument.type != "comment"
            ]
            parameters = self._enclosing_template_parameters(scope, source_lines)
            if given in parameters or [] in parameters:
                return self._get_node_text(name, source_lines)
        return self._get_node_text(scope, source_lines)

    def _enclosing_template_parameters(self, node: Any, source_lines: List[str]) -> List[List]:
        """Parameter names of each `template_declaration` that encloses ``node``."""
        found = []
        while (node := node.parent) is not None:
            if node.type == "template_declaration":
                parameters = node.child_by_field_name("parameters")
                found.append(
                    [
                        self._innermost_identifier(
                            param.child_by_field_name("name")
                            or param.child_by_field_name("declarator")
                            or param.named_children[-1],
                            source_lines,
                        )
                        for param in (parameters.named_children if parameters else ())
                        if param.type != "comment" and param.named_children
                    ]
                )
        return found

    def _misplaced_name(self, node: Any, source_lines: List[str]) -> Optional[str]:
        """The last name in an ERROR child of ``node`` that holds only names, or None.

        A macro in the place of a name puts the names tree-sitter could not place
        in an ERROR node; with two macros, the return type comes first (`MACRO
        MACRO error_code document::consume()`), and a reference or pointer return
        type puts its `&` or `*` first (`MACRO T &logger::sinks()`). Other ERROR
        content is not read.
        """
        for child in node.children:
            if child.type == "ERROR":
                names = [c for c in child.children if c.type not in ("&", "&&", "*")]
                if names and all(name.type in _NAME_NODES for name in names):
                    return self._get_node_text(names[-1], source_lines)
        return None

    def _declarator_name(self, func_decl: Any, source_lines: List[str]) -> Optional[str]:
        """Return the (possibly qualified) name held by a function_declarator."""
        name_node = func_decl.child_by_field_name("declarator")
        if name_node is None:
            return None
        if name_node.type in _NAME_NODES:
            return self._get_node_text(name_node, source_lines)
        if name_node.type in ("template_function", "template_method"):
            inner = name_node.child_by_field_name("name")
            if inner is not None:
                return self._get_node_text(inner, source_lines)
        # Wrapped (e.g. pointer/reference around the name).
        ident = self._innermost_identifier(name_node, source_lines)
        return ident

    def _innermost_identifier(self, node: Any, source_lines: List[str]) -> Optional[str]:
        """Dig through declarator wrappers to the leaf identifier name."""
        cur = node
        seen = 0
        while cur is not None and seen < 12:
            seen += 1
            if cur.type in _NAME_NODES:
                return self._get_node_text(cur, source_lines)
            if cur.type in ("template_function", "template_method"):
                inner = cur.child_by_field_name("name")
                if inner is not None:
                    return self._get_node_text(inner, source_lines)
            nxt = cur.child_by_field_name("declarator")
            if nxt is None:
                nxt = self._wrapper_inner(cur) if cur.type in _DECLARATOR_WRAPPERS else None
            if nxt is None:
                for ch in cur.children:
                    if ch.type in _NAME_NODES:
                        return self._get_node_text(ch, source_lines)
                return None
            cur = nxt
        return None

    def _param_name(self, param_node: Any, source_lines: List[str]) -> Optional[str]:
        decl = param_node.child_by_field_name("declarator")
        if decl is None:
            return None
        return self._innermost_identifier(decl, source_lines)

    # ------------------------------------------------------------------
    # Attribute helpers
    # ------------------------------------------------------------------

    def _collect_namespaces(self, node: Any) -> None:
        if node.type == "namespace_definition":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                for seg in self._get_node_text_simple(name_node).split("::"):
                    seg = seg.strip()
                    if seg:
                        self._namespaces.add(seg)
        for child in node.children:
            self._collect_namespaces(child)

    def _has_child_type(
        self, node: Any, wrapper_type: str, keyword: str, source_lines: List[str]
    ) -> bool:
        """True if node has a `wrapper_type` child whose text is `keyword`."""
        for child in node.children:
            if child.type == wrapper_type:
                if self._get_node_text(child, source_lines).strip() == keyword:
                    return True
                # storage_class_specifier / type_qualifier wrap a keyword child.
                for gc in child.children:
                    if gc.type == keyword:
                        return True
        return False

    def _is_pure_virtual(self, node: Any) -> bool:
        """Detect `= 0` pure-virtual declarations."""
        for child in node.children:
            if child.type == "number_literal":
                txt = self._get_node_text_simple(child).strip()
                if txt == "0":
                    return True
        return False

    def _has_virtual_specifier(self, func_decl: Any, keyword: str, source_lines: List[str]) -> bool:
        for child in func_decl.children:
            if child.type == "virtual_specifier":
                if self._get_node_text(child, source_lines).strip() == keyword:
                    return True
        return False

    def _return_type(self, node: Any, source_lines: List[str]) -> Optional[str]:
        type_node = node.child_by_field_name("type")
        if type_node is not None:
            return self._get_node_text(type_node, source_lines)[:100]
        return None

    def _signature(self, node: Any, source_lines: List[str]) -> str:
        text = self._get_node_text(node, source_lines)
        first = text.split("\n", 1)[0]
        return first[:200]

    def _is_function_pointer(self, func_decl: Any) -> bool:
        """True if a function_declarator actually declares a function pointer.

        `int (*cb)(int)` parses as a function_declarator whose inner declarator
        is a parenthesized_declarator (wrapping a pointer_declarator); a real
        function's inner declarator is a plain / qualified / operator name.
        """
        inner = func_decl.child_by_field_name("declarator")
        return inner is not None and inner.type == "parenthesized_declarator"

    def _handle_typedef(
        self,
        node: Any,
        parent: Optional[str],
        visibility: Visibility,
        source_lines: List[str],
        make_id,
        add,
    ) -> None:
        """Emit a struct/union/enum introduced via `typedef`.

        Handles the anonymous-aggregate idiom common in C
        (`typedef struct { ... } Point;`) by naming the type after the typedef
        alias. Plain scalar aliases (`typedef int Id;`) are not components.
        """
        type_child = node.child_by_field_name("type")
        if type_child is None or type_child.type not in _TYPE_CONTAINER_NODES:
            return

        # Prefer the aggregate's own tag name; fall back to the typedef alias.
        name = None
        name_node = type_child.child_by_field_name("name")
        if name_node is not None:
            name = self._get_node_text(name_node, source_lines)
        if not name:
            decl = node.child_by_field_name("declarator")
            if decl is not None:
                if decl.type in _NAME_NODES:
                    name = self._get_node_text(decl, source_lines)
                else:
                    name = self._innermost_identifier(decl, source_lines)
        if not name:
            return

        component_id = make_id(parent, name)
        comp_type = (
            ComponentType.ENUM if type_child.type == "enum_specifier" else ComponentType.CLASS
        )
        attributes = ComponentAttributes(
            signature=self._signature(node, source_lines),
            visibility=visibility,
            line_count=node.end_point[0] - node.start_point[0] + 1,
        )
        comp = self._create_component(
            component_id, comp_type, node, self._file_path, self._lang, attributes, parent
        )
        add(comp, True)
        self._walk_type_body(type_child, component_id, source_lines, self._file_path, make_id, add)

    def _strip_generics(self, name: str) -> str:
        if not name:
            return name
        idx = name.find("<")
        return name[:idx] if idx != -1 else name
