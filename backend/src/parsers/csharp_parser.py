"""
C#-specific parser implementation.
"""

from typing import List, Optional, Any
from pathlib import Path

from .base_parser import BaseLanguageParser
from ..core.models import (
    Component,
    ComponentType,
    ComponentAttributes,
    Parameter,
    Visibility,
    IdScheme,
)


# AST node types that represent type-container declarations in C#.
_TYPE_CONTAINER_NODES = {
    "class_declaration",
    "interface_declaration",
    "struct_declaration",
    "enum_declaration",
    "record_declaration",
    "record_struct_declaration",
}

# AST node types that represent function-like definitions in C#.
_FUNCTION_NODES = {
    "method_declaration",
    "constructor_declaration",
    "destructor_declaration",
    "operator_declaration",
    "conversion_operator_declaration",
    "local_function_statement",
}


class CSharpParser(BaseLanguageParser):
    """C#-specific parser implementation."""

    def get_call_node_types(self) -> List[str]:
        """Return AST node types that represent function calls in C#."""
        return ["invocation_expression", "object_creation_expression"]

    def get_function_node_types(self) -> List[str]:
        """Return AST node types that represent function definitions in C#."""
        return list(_FUNCTION_NODES)

    def extract_call_name(self, function_node: Any) -> Optional[str]:
        """Extract function name from C# call node.

        Handles:
        - identifier            : Foo()
        - generic_name          : Foo<T>() - returns just "Foo"
        - member_access_expression : obj.Method() - returns "obj.Method"
        - qualified_name        : Namespace.Foo() - returns "Namespace.Foo"
        - object_creation_expression : new Foo() - returns the type name
        """
        if function_node is None:
            return None

        node_type = function_node.type

        if node_type == "identifier":
            return self._get_node_text_simple(function_node)

        if node_type == "generic_name":
            # generic_name has an `identifier` child followed by `type_argument_list`.
            for child in function_node.children:
                if child.type == "identifier":
                    return self._get_node_text_simple(child)
            return self._strip_generics(self._get_node_text_simple(function_node))

        if node_type == "member_access_expression":
            # expression "." (identifier|generic_name)
            if function_node.children and len(function_node.children) >= 3:
                object_node = function_node.children[0]
                method_name_node = function_node.children[2]
                object_name = self._strip_generics(self._get_node_text_simple(object_node))
                method_name = self._strip_generics(self._get_node_text_simple(method_name_node))
                return f"{object_name}.{method_name}"
            return None

        if node_type == "qualified_name":
            # qualified_name: left "." right
            return self._strip_generics(self._get_node_text_simple(function_node))

        if node_type == "alias_qualified_name":
            # alias::name -> drop alias, keep name
            return self._strip_generics(self._get_node_text_simple(function_node))

        if node_type == "object_creation_expression":
            # `new Foo(...)` — the type follows the `new` keyword.
            for child in function_node.children:
                if child.type in (
                    "identifier",
                    "generic_name",
                    "qualified_name",
                    "member_access_expression",
                    "predefined_type",
                ):
                    return self.extract_call_name(child)
            return None

        if node_type == "invocation_expression":
            # Nested invocation: Foo()(args) — recurse into the function part.
            if function_node.children:
                return self.extract_call_name(function_node.children[0])
            return None

        return None

    def extract_components(
        self, root_node: Any, source_code: bytes, file_path: Path
    ) -> List[Component]:
        """Extract components from C# syntax tree."""
        components: List[Component] = []
        source_lines = source_code.decode("utf-8").split("\n")

        def recurse_into_body(parent_node: Any, parent_component: str) -> None:
            """Recurse into a class/interface/struct body to extract members."""
            for child in parent_node.children:
                if child.type in (
                    "declaration_list",
                    "class_body",
                    "interface_body",
                    "struct_body",
                    "enum_body",
                    "record_body",
                ):
                    for grandchild in child.children:
                        extract_from_node(grandchild, parent_component)

        def extract_from_node(node: Any, parent_component: Optional[str] = None) -> None:
            # File-scoped namespaces and namespace_declaration: walk through but
            # incorporate the namespace name into the parent chain so members keep
            # unique IDs across namespaces.
            if node.type in ("namespace_declaration", "file_scoped_namespace_declaration"):
                ns_name = self._extract_qualified_name(node, source_lines)
                ns_component_id = ns_name or parent_component
                # Walk into the namespace body
                for child in node.children:
                    if child.type == "declaration_list":
                        for grandchild in child.children:
                            extract_from_node(grandchild, ns_component_id)
                    elif child.type not in (
                        "namespace",
                        "identifier",
                        "qualified_name",
                        "{",
                        "}",
                        ";",
                    ):
                        extract_from_node(child, ns_component_id)
                return

            # Type containers: class / interface / struct / enum / record
            if node.type in _TYPE_CONTAINER_NODES:
                component = self._extract_type_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)
                    recurse_into_body(node, component.id)
                return

            # Methods, constructors, destructors, operators, local functions
            if node.type in _FUNCTION_NODES:
                component = self._extract_method_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)
                return

            # Fields, properties, events, indexers
            if node.type in (
                "field_declaration",
                "event_field_declaration",
                "property_declaration",
                "event_declaration",
                "indexer_declaration",
            ):
                component = self._extract_member_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)
                return

            # Continue walking the rest of the tree
            for child in node.children:
                extract_from_node(child, parent_component)

        extract_from_node(root_node)
        return components

    def extract_function_parameters(
        self, function_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """Extract parameters from C# method/constructor node."""
        parameters: List[Parameter] = []

        try:
            for child in function_node.children:
                if child.type == "parameter_list":
                    for param_child in child.children:
                        if param_child.type == "parameter":
                            param_name = self._extract_parameter_name(param_child, source_lines)
                            has_default = self._param_has_default_value(param_child)
                            if param_name:
                                parameters.append(
                                    Parameter(name=param_name, required=not has_default)
                                )
        except Exception:
            pass

        return parameters

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_parameter_name(self, param_node: Any, source_lines: List[str]) -> Optional[str]:
        """Extract parameter name from a `parameter` node."""
        # In tree-sitter-c-sharp, a `parameter` is `[attribute]* [modifier]*
        # type name:identifier [equals_value_clause]`. Use the `name` field:
        # scanning for the first identifier child instead picks the TYPE when it
        # is a simple name — `DateTime when` parses the type as an `identifier`
        # too, so `when` is reported as `DateTime`. (Built-in types like `int`
        # are `predefined_type` nodes, which is why they happen to work.)
        name_node = param_node.child_by_field_name("name")
        if name_node is not None:
            return self._get_node_text(name_node, source_lines)
        for child in param_node.children:
            if child.type == "identifier":
                return self._get_node_text(child, source_lines)
        return None

    def _param_has_default_value(self, param_node: Any) -> bool:
        """Check if a `parameter` node has a default-value clause."""
        for child in param_node.children:
            if child.type == "equals_value_clause":
                return True
        return False

    def _extract_method_component(
        self,
        node: Any,
        source_lines: List[str],
        file_path: Path,
        parent_component: Optional[str],
    ) -> Optional[Component]:
        """Extract C# method/constructor/operator component."""
        try:
            method_name = self._extract_method_name(node, source_lines)
            if not method_name:
                return None

            component_id = self.make_id(file_path, method_name, parent_component)

            parameters = self.extract_function_parameters(node, source_lines)
            visibility = self._extract_visibility(node, source_lines)

            attributes = ComponentAttributes(
                signature=self._extract_signature(node, source_lines),
                parameters=parameters,
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                is_static=self._has_modifier(node, "static"),
                is_abstract=self._has_modifier(node, "abstract"),
                # In C#, `sealed` is the closest analogue to Java's `final` for methods/classes.
                is_final=self._has_modifier(node, "sealed"),
            )

            if node.type == "constructor_declaration":
                component_type = ComponentType.CONSTRUCTOR
            else:
                # destructor / operator / conversion_operator / local_function are
                # all method-like; surface them as METHOD when nested under a type.
                component_type = (
                    ComponentType.METHOD if parent_component else ComponentType.FUNCTION
                )

            return self._create_component(
                component_id,
                component_type,
                node,
                file_path,
                "csharp",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_method_name(self, node: Any, source_lines: List[str]) -> Optional[str]:
        """Extract the name of a method/constructor/operator declaration."""
        # The `name` field of a method_declaration is an identifier.
        # For constructors/destructors, the name is also an identifier child.
        # For operator_declaration, the operator symbol follows the `operator` keyword.
        if node.type == "operator_declaration":
            saw_operator = False
            for child in node.children:
                if child.type == "operator":
                    saw_operator = True
                    continue
                if saw_operator and child.type not in ("operator",):
                    return f"operator{self._get_node_text(child, source_lines)}"
            return None

        if node.type == "conversion_operator_declaration":
            # e.g. `public static implicit operator int(Foo f)` -> name: "operator int"
            for child in node.children:
                if child.type in (
                    "predefined_type",
                    "identifier",
                    "qualified_name",
                    "generic_name",
                ):
                    return f"operator_{self._get_node_text(child, source_lines)}"
            return None

        # Use the declaration's `name` field directly. Scanning for the first
        # identifier child instead picks the RETURN TYPE when it is a simple
        # type — `public Task Foo()` parses as "Task" — so every method
        # returning that type collapses onto one id. The name field is
        # unambiguous; fall back to the first identifier only if the grammar
        # exposes no name field (e.g. destructors).
        #
        # V2 only: this changes the component id (`Task` → `Foo`), so legacy V1
        # spaces keep the old first-identifier behavior for refresh stability
        # and adopt the fix on a deliberate full V2 reindex.
        if self._scheme == IdScheme.V2_PATH:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                return self._get_node_text(name_node, source_lines)
        for child in node.children:
            if child.type == "identifier":
                return self._get_node_text(child, source_lines)
        return None

    def _extract_type_component(
        self,
        node: Any,
        source_lines: List[str],
        file_path: Path,
        parent_component: Optional[str],
    ) -> Optional[Component]:
        """Extract C# class/interface/struct/enum/record component."""
        try:
            name = None
            for child in node.children:
                if child.type == "identifier":
                    name = self._get_node_text(child, source_lines)
                    break

            if not name:
                return None

            component_id = self.make_id(file_path, name, parent_component)

            component_type = self._determine_class_type(node)
            visibility = self._extract_visibility(node, source_lines)

            attributes = ComponentAttributes(
                signature=self._extract_signature(node, source_lines),
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                is_static=self._has_modifier(node, "static"),
                is_abstract=self._has_modifier(node, "abstract"),
                is_final=self._has_modifier(node, "sealed"),
            )

            return self._create_component(
                component_id,
                component_type,
                node,
                file_path,
                "csharp",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_member_component(
        self,
        node: Any,
        source_lines: List[str],
        file_path: Path,
        parent_component: Optional[str],
    ) -> Optional[Component]:
        """Extract C# field / property / event / indexer component."""
        try:
            member_name = self._extract_member_name(node, source_lines)
            if not member_name:
                return None

            component_id = self.make_id(file_path, member_name, parent_component)

            visibility = self._extract_visibility(node, source_lines)

            # `const` always means constant. `static readonly` is the
            # idiomatic C# way to declare a runtime constant.
            is_const = self._has_modifier(node, "const")
            is_static_readonly = self._has_modifier(node, "static") and self._has_modifier(
                node, "readonly"
            )
            is_constant = is_const or is_static_readonly

            component_type = ComponentType.CONSTANT if is_constant else ComponentType.VARIABLE

            attributes = ComponentAttributes(
                signature=self._extract_signature(node, source_lines),
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                is_static=self._has_modifier(node, "static"),
                is_final=self._has_modifier(node, "readonly") or is_const,
            )

            return self._create_component(
                component_id,
                component_type,
                node,
                file_path,
                "csharp",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_member_name(self, node: Any, source_lines: List[str]) -> Optional[str]:
        """Extract the name of a field/property/event/indexer declaration."""
        # property_declaration / event_declaration / indexer_declaration:
        #   modifiers type identifier ...
        if node.type in ("property_declaration", "event_declaration", "indexer_declaration"):
            # Use the declaration's `name` field directly. Scanning for the
            # first identifier child instead picks the TYPE when it is a simple
            # name — `DateTime CreatedAt` parses as "DateTime" — so every member
            # of that type collapses onto one id. Mirrors `_extract_method_name`.
            #
            # V2 only: this changes the component id, so legacy V1 spaces keep
            # the first-identifier behavior for refresh stability and adopt the
            # fix on a deliberate full V2 reindex.
            if self._scheme == IdScheme.V2_PATH:
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    return self._get_node_text(name_node, source_lines)
            for child in node.children:
                if child.type == "identifier":
                    return self._get_node_text(child, source_lines)
            # Indexer uses `this[...]`, fall back to a synthetic name.
            if node.type == "indexer_declaration":
                return "this"
            return None

        # field_declaration / event_field_declaration:
        #   modifiers variable_declaration(type variable_declarator(identifier ...), ...)
        for child in node.children:
            if child.type == "variable_declaration":
                for grandchild in child.children:
                    if grandchild.type == "variable_declarator":
                        for vchild in grandchild.children:
                            if vchild.type == "identifier":
                                return self._get_node_text(vchild, source_lines)
        return None

    def _extract_qualified_name(self, node: Any, source_lines: List[str]) -> Optional[str]:
        """Extract namespace name from namespace_declaration / file_scoped_namespace_declaration."""
        for child in node.children:
            if child.type in ("identifier", "qualified_name"):
                return self._get_node_text(child, source_lines)
        return None

    def _extract_visibility(self, node: Any, source_lines: List[str]) -> Visibility:
        """Extract visibility modifier from node.

        C# defaults: types default to `internal` (mapped to PACKAGE), members
        default to `private`.
        """
        # Modifiers in tree-sitter-c-sharp appear as direct keyword children
        # (`public`, `private`, `protected`, `internal`).
        has_public = False
        has_private = False
        has_protected = False
        has_internal = False

        for child in node.children:
            if child.type == "modifier":
                text = self._get_node_text(child, source_lines).strip().lower()
                if text == "public":
                    has_public = True
                elif text == "private":
                    has_private = True
                elif text == "protected":
                    has_protected = True
                elif text == "internal":
                    has_internal = True
            elif child.type in ("public", "private", "protected", "internal"):
                if child.type == "public":
                    has_public = True
                elif child.type == "private":
                    has_private = True
                elif child.type == "protected":
                    has_protected = True
                elif child.type == "internal":
                    has_internal = True

        if has_public:
            return Visibility.PUBLIC
        if has_protected:
            return Visibility.PROTECTED
        if has_private:
            return Visibility.PRIVATE
        if has_internal:
            return Visibility.PACKAGE  # C# `internal` ~ assembly-private

        # Default: types -> internal (PACKAGE); members -> private.
        if node.type in _TYPE_CONTAINER_NODES:
            return Visibility.PACKAGE
        if node.type in _FUNCTION_NODES or node.type in (
            "field_declaration",
            "event_field_declaration",
            "property_declaration",
            "event_declaration",
            "indexer_declaration",
        ):
            return Visibility.PRIVATE
        return Visibility.PACKAGE

    def _has_modifier(self, node: Any, modifier: str) -> bool:
        """Check if node has a specific modifier keyword."""
        target = modifier.lower()
        for child in node.children:
            if child.type == "modifier":
                text = self._get_node_text_simple(child).strip().lower()
                if text == target:
                    return True
            elif child.type.lower() == target:
                return True
        return False

    def _extract_signature(self, node: Any, source_lines: List[str]) -> str:
        """Extract method/class signature (first line, capped at 200 chars)."""
        text = self._get_node_text(node, source_lines)
        lines = text.split("\n")
        if len(lines) > 1:
            return lines[0][:200]
        return text[:200]

    def _determine_class_type(self, node: Any) -> ComponentType:
        """Determine component type based on node type."""
        if node.type == "interface_declaration":
            return ComponentType.INTERFACE
        if node.type == "enum_declaration":
            return ComponentType.ENUM
        # struct / record / record_struct / class all surface as CLASS.
        return ComponentType.CLASS

    def _strip_generics(self, name: str) -> str:
        """Strip generic type arguments from a name (e.g. `Foo<T>` -> `Foo`)."""
        if not name:
            return name
        idx = name.find("<")
        if idx == -1:
            return name
        return name[:idx]
