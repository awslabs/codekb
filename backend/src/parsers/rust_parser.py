"""
Rust-specific parser implementation.
"""

from typing import List, Optional, Any
from pathlib import Path

from .base_parser import BaseLanguageParser
from ..core.models import Component, ComponentType, ComponentAttributes, Parameter, Visibility


# AST node types that represent type-container declarations in Rust.
_TYPE_CONTAINER_NODES = {
    "struct_item",
    "enum_item",
    "trait_item",
    "union_item",
    "type_item",
}

# AST node types that represent function-like definitions in Rust.
_FUNCTION_NODES = {
    "function_item",
    "function_signature_item",
}

# AST node types that represent member-like declarations.
_MEMBER_NODES = {
    "const_item",
    "static_item",
}


class RustParser(BaseLanguageParser):
    """Rust-specific parser implementation."""

    def get_call_node_types(self) -> List[str]:
        """Return AST node types that represent function calls in Rust."""
        return ["call_expression"]

    def get_function_node_types(self) -> List[str]:
        """Return AST node types that represent function definitions in Rust."""
        return list(_FUNCTION_NODES)

    def extract_call_name(self, function_node: Any) -> Optional[str]:
        """Extract function name from Rust call node.

        Handles:
        - identifier            : foo()
        - field_expression      : obj.method() -> "obj.method"
        - scoped_identifier     : Module::func -> "Module.func" (normalize :: to .)
        - generic_function      : foo::<T>() -> "foo"
        """
        if function_node is None:
            return None

        node_type = function_node.type

        if node_type == "identifier":
            return self._get_node_text_simple(function_node)

        if node_type == "field_expression":
            # value "." (field_identifier|integer_literal)
            if function_node.children and len(function_node.children) >= 3:
                object_node = function_node.children[0]
                method_name_node = function_node.children[2]
                object_name = self._strip_generics(self._get_node_text_simple(object_node))
                method_name = self._strip_generics(self._get_node_text_simple(method_name_node))
                return f"{object_name}.{method_name}"
            return None

        if node_type == "scoped_identifier":
            # `path::name` -> normalize to `path.name`
            text = self._get_node_text_simple(function_node)
            return self._strip_generics(text.replace("::", "."))

        if node_type == "generic_function":
            # generic_function: function "::" type_arguments
            # First child is the underlying function expression.
            if function_node.children:
                return self.extract_call_name(function_node.children[0])
            return None

        if node_type == "call_expression":
            # Nested call: foo()(args)
            if function_node.children:
                return self.extract_call_name(function_node.children[0])
            return None

        if node_type == "parenthesized_expression":
            for child in function_node.children:
                if child.type not in ("(", ")"):
                    return self.extract_call_name(child)
            return None

        return None

    def extract_components(
        self, root_node: Any, source_code: bytes, file_path: Path
    ) -> List[Component]:
        """Extract components from Rust syntax tree."""
        components: List[Component] = []
        source_lines = source_code.decode("utf-8").split("\n")

        def recurse_into_body(parent_node: Any, parent_component: str) -> None:
            """Recurse into a declaration_list to extract members."""
            for child in parent_node.children:
                if child.type == "declaration_list":
                    for grandchild in child.children:
                        extract_from_node(grandchild, parent_component, in_type=True)

        def extract_from_node(
            node: Any, parent_component: Optional[str] = None, in_type: bool = False
        ) -> None:
            """``in_type`` is True inside an impl or trait body, where a fn is a method."""
            # Module: thread the module name through the parent chain.
            if node.type == "mod_item":
                mod_name = self._extract_named_identifier(node, source_lines)
                if not mod_name:
                    return
                mod_component_id = self.make_id(file_path, mod_name, parent_component)
                # mod_item bodies declared inline have a declaration_list child;
                # external modules (mod foo;) have no body and are no-ops here.
                for child in node.children:
                    if child.type == "declaration_list":
                        for grandchild in child.children:
                            extract_from_node(grandchild, mod_component_id)
                return

            # impl block: methods inside belong to the implementing type.
            if node.type == "impl_item":
                impl_target = self._extract_impl_target(node, source_lines)
                if impl_target:
                    impl_parent = self.make_id(file_path, impl_target, parent_component)
                else:
                    impl_parent = parent_component
                for child in node.children:
                    if child.type == "declaration_list":
                        for grandchild in child.children:
                            extract_from_node(grandchild, impl_parent, in_type=bool(impl_target))
                return

            # Type containers: struct / enum / trait / union / type alias
            if node.type in _TYPE_CONTAINER_NODES:
                component = self._extract_type_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)
                    # Trait bodies hold function_signature_item declarations.
                    if node.type == "trait_item":
                        recurse_into_body(node, component.id)
                return

            # Free / impl / trait functions
            if node.type in _FUNCTION_NODES:
                component = self._extract_function_component(
                    node, source_lines, file_path, parent_component, in_type
                )
                if component:
                    components.append(component)
                return

            # Constants / statics
            if node.type in _MEMBER_NODES:
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
        """Extract parameters from a Rust function-like node.

        Skips `self_parameter`. Rust does not support default parameter values,
        so every parameter is required.
        """
        parameters: List[Parameter] = []

        try:
            for child in function_node.children:
                if child.type == "parameters":
                    for param_child in child.children:
                        if param_child.type == "parameter":
                            param_name = self._extract_parameter_name(param_child, source_lines)
                            if param_name:
                                parameters.append(Parameter(name=param_name, required=True))
        except Exception:
            pass

        return parameters

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_parameter_name(self, param_node: Any, source_lines: List[str]) -> Optional[str]:
        """Extract parameter name from a `parameter` node.

        A `parameter` typically has: `pattern : type`. The pattern is usually
        an `identifier`, but can also be a destructuring pattern (rare in
        function signatures); we conservatively return only simple identifiers.
        """
        for child in param_node.children:
            if child.type == "identifier":
                return self._get_node_text(child, source_lines)
            if child.type == "mutable_specifier":
                continue
        return None

    def _extract_function_component(
        self,
        node: Any,
        source_lines: List[str],
        file_path: Path,
        parent_component: Optional[str],
        in_type: bool = False,
    ) -> Optional[Component]:
        """Extract a Rust function/method component. ``in_type``: inside an impl or trait."""
        try:
            function_name = self._extract_named_identifier(node, source_lines)
            if not function_name:
                return None

            component_id = self.make_id(file_path, function_name, parent_component)

            parameters = self.extract_function_parameters(node, source_lines)
            visibility = self._extract_visibility(node, source_lines)

            # `static` in the OO sense = associated function (no self).
            is_static = in_type and not self._has_self_parameter(node)

            # Trait method signatures have no body -> abstract.
            is_abstract = node.type == "function_signature_item"

            attributes = ComponentAttributes(
                signature=self._extract_signature(node, source_lines),
                parameters=parameters,
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                is_static=is_static,
                is_abstract=is_abstract,
                is_final=False,  # Rust has no `final`-equivalent for fns
            )

            # Functions inside an impl/trait block are methods; a function in a
            # module (`mod tests { fn helper() }`) is a free function.
            component_type = ComponentType.METHOD if in_type else ComponentType.FUNCTION

            return self._create_component(
                component_id,
                component_type,
                node,
                file_path,
                "rust",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_type_component(
        self,
        node: Any,
        source_lines: List[str],
        file_path: Path,
        parent_component: Optional[str],
    ) -> Optional[Component]:
        """Extract a Rust struct/enum/trait/union/type component."""
        try:
            name = self._extract_named_identifier(node, source_lines)
            if not name:
                return None

            component_id = self.make_id(file_path, name, parent_component)

            component_type = self._determine_class_type(node)
            visibility = self._extract_visibility(node, source_lines)

            attributes = ComponentAttributes(
                signature=self._extract_signature(node, source_lines),
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                is_static=False,
                is_abstract=node.type == "trait_item",
                is_final=False,
            )

            return self._create_component(
                component_id,
                component_type,
                node,
                file_path,
                "rust",
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
        """Extract a Rust const_item or static_item component."""
        try:
            name = self._extract_named_identifier(node, source_lines)
            if not name:
                return None

            component_id = self.make_id(file_path, name, parent_component)

            visibility = self._extract_visibility(node, source_lines)

            # const_item -> always a constant. static_item is also a constant
            # in our component model (it's a global with a fixed location).
            attributes = ComponentAttributes(
                signature=self._extract_signature(node, source_lines),
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                is_static=node.type == "static_item",
                is_final=True,
            )

            return self._create_component(
                component_id,
                ComponentType.CONSTANT,
                node,
                file_path,
                "rust",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_named_identifier(self, node: Any, source_lines: List[str]) -> Optional[str]:
        """Return the text of the first `identifier` / `type_identifier` child."""
        for child in node.children:
            if child.type in ("identifier", "type_identifier"):
                return self._get_node_text(child, source_lines)
        return None

    def _extract_impl_target(self, impl_node: Any, source_lines: List[str]) -> Optional[str]:
        """Extract the implementing type name from an `impl_item`.

        For both `impl Foo {...}` and `impl Trait for Foo {...}`, we want `Foo`.
        Tree-sitter exposes `type` as a field on impl_item; for trait impls, it
        is the type that follows `for`.
        """
        # Children layout differs slightly between `impl Foo` and
        # `impl Trait for Foo`. The implementing type is always the last
        # type-bearing child before `where_clause` / `declaration_list`.
        target_node = None
        for child in impl_node.children:
            if child.type in (
                "type_identifier",
                "scoped_type_identifier",
                "generic_type",
                "reference_type",
            ):
                target_node = child  # latest one wins -> the impl target
            elif child.type in ("where_clause", "declaration_list"):
                break

        if target_node is None:
            return None

        text = self._get_node_text(target_node, source_lines)
        # Normalize: strip generics, leading `&`/`&mut`, and `module::` prefixes.
        text = text.strip()
        if text.startswith("&mut "):
            text = text[len("&mut ") :]
        elif text.startswith("&"):
            text = text[1:]
        text = self._strip_generics(text)
        if "::" in text:
            text = text.rsplit("::", 1)[1]
        return text or None

    def _extract_visibility(self, node: Any, source_lines: List[str]) -> Visibility:
        """Extract Rust visibility.

        - `pub`           -> PUBLIC
        - `pub(crate)`    -> PACKAGE (crate-scope; closest analogue)
        - `pub(super)` /
          `pub(in path)`  -> PROTECTED-ish; we use PACKAGE for both
        - default         -> PRIVATE
        """
        for child in node.children:
            if child.type == "visibility_modifier":
                text = self._get_node_text(child, source_lines).strip()
                if text == "pub":
                    return Visibility.PUBLIC
                # `pub(crate)`, `pub(super)`, `pub(in path)` all narrow visibility.
                if text.startswith("pub"):
                    return Visibility.PACKAGE
        return Visibility.PRIVATE

    def _has_self_parameter(self, node: Any) -> bool:
        """Return True if a function/method has a `self` receiver."""
        for child in node.children:
            if child.type == "parameters":
                for param_child in child.children:
                    if param_child.type == "self_parameter":
                        return True
        return False

    def _extract_signature(self, node: Any, source_lines: List[str]) -> str:
        """Extract function/type signature (first line, capped at 200 chars)."""
        text = self._get_node_text(node, source_lines)
        lines = text.split("\n")
        if len(lines) > 1:
            return lines[0][:200]
        return text[:200]

    def _determine_class_type(self, node: Any) -> ComponentType:
        """Determine component type based on Rust node type."""
        if node.type == "trait_item":
            return ComponentType.INTERFACE
        if node.type == "enum_item":
            return ComponentType.ENUM
        # struct_item / union_item / type_item all surface as CLASS.
        return ComponentType.CLASS

    def _strip_generics(self, name: str) -> str:
        """Strip generic type arguments from a name (`Foo<T>` -> `Foo`)."""
        if not name:
            return name
        idx = name.find("<")
        if idx == -1:
            return name
        return name[:idx]
