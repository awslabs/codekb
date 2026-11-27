"""
Kotlin-specific parser implementation.
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


class KotlinParser(BaseLanguageParser):
    """Kotlin-specific parser implementation."""

    def get_call_node_types(self) -> List[str]:
        """Return AST node types that represent function calls in Kotlin."""
        return ["call_expression"]

    def get_function_node_types(self) -> List[str]:
        """Return AST node types that represent function definitions in Kotlin."""
        return ["function_declaration"]

    def extract_call_name(self, function_node: Any) -> Optional[str]:
        """Extract function name from Kotlin call node."""
        if function_node.type == "identifier":
            return self._get_node_text_simple(function_node)
        elif function_node.type == "navigation_expression":
            # Kotlin method call: obj.method()
            if function_node.children and len(function_node.children) >= 3:
                object_node = function_node.children[0]
                method_name_node = function_node.children[2]
                object_name = self._get_node_text_simple(object_node)
                method_name = self._get_node_text_simple(method_name_node)
                return f"{object_name}.{method_name}"
        return None

    def extract_components(
        self, root_node: Any, source_code: bytes, file_path: Path
    ) -> List[Component]:
        """Extract components from Kotlin syntax tree."""
        components: List[Component] = []
        source_lines = source_code.decode("utf-8").split("\n")

        def extract_from_node(node: Any, parent_component: Optional[str] = None) -> None:
            if node.type == "function_declaration":
                component = self._extract_function_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)

            elif node.type in ["class_declaration", "object_declaration", "interface_declaration"]:
                component = self._extract_class_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)
                    for child in node.children:
                        if child.type == "class_body":
                            for grandchild in child.children:
                                extract_from_node(grandchild, component.id)

            elif node.type == "property_declaration":
                component = self._extract_property_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)

            # Do not descend into a function's own body. Classes/objects/
            # interfaces handle their members explicitly above; a
            # function_declaration must stop here so declarations local to a
            # function body or lambda (`fun helper()`, `val x`) are NOT emitted
            # as components — they aren't part of the module's callable surface
            # and, keyed only by file+bare-name, they collide across functions.
            #
            # V2 only: dropping locals changes which components exist, so legacy
            # V1 spaces keep descending (old behavior) for refresh stability.
            no_descend = ["class_declaration", "object_declaration", "interface_declaration"]
            if self._scheme == IdScheme.V2_PATH:
                no_descend.append("function_declaration")
            if node.type not in no_descend:
                for child in node.children:
                    extract_from_node(child, parent_component)

        extract_from_node(root_node)
        return components

    def extract_function_parameters(
        self, function_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """Extract parameters from Kotlin function node."""
        parameters: List[Parameter] = []

        try:
            for child in function_node.children:
                if child.type == "function_value_parameters":
                    for param_child in child.children:
                        if param_child.type == "parameter":
                            param_name = self._extract_parameter_name(param_child, source_lines)
                            has_default = self._has_default_value(param_child)
                            if param_name:
                                parameters.append(
                                    Parameter(name=param_name, required=not has_default)
                                )
        except Exception:
            pass

        return parameters

    def _extract_parameter_name(self, param_node: Any, source_lines: List[str]) -> Optional[str]:
        """Extract parameter name from parameter node."""
        for child in param_node.children:
            if child.type == "identifier":
                return self._get_node_text(child, source_lines)
        return None

    def _has_default_value(self, param_node: Any) -> bool:
        """Check if parameter has a default value."""
        for child in param_node.children:
            if child.type == "=":
                return True
        return False

    def _extract_function_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract Kotlin function component."""
        try:
            function_name = None

            for child in node.children:
                if child.type == "identifier":
                    function_name = self._get_node_text(child, source_lines)
                    break

            if not function_name:
                return None

            # Extension functions are identified by their RECEIVER type plus
            # their name — `String.toSlug` and `Int.toSlug` are different
            # functions. Without the receiver in the id they share one id and
            # overwrite each other (20 → 1 observed). Fold the receiver in as a
            # segment so the ids differ while the trailing segment stays the
            # bare name (get_simple_name / call resolution keep working).
            # V2 only: folding the receiver into the id changes it, so legacy
            # V1 spaces are left untouched for refresh stability.
            receiver = (
                self._extension_receiver(node, source_lines)
                if self._scheme == IdScheme.V2_PATH
                else None
            )
            id_name = f"{receiver}.{function_name}" if receiver else function_name
            id_name = self._with_companion(node, source_lines, id_name)

            component_id = self.make_id(file_path, id_name, parent_component)

            parameters = self.extract_function_parameters(node, source_lines)
            visibility = self._extract_visibility(node, source_lines)

            attributes = ComponentAttributes(
                signature=self._extract_signature(node, source_lines),
                parameters=parameters,
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                is_static=False,  # Kotlin doesn't have static, uses companion objects
                is_abstract=self._has_modifier(node, "abstract"),
                is_final=not self._has_modifier(
                    node, "open"
                ),  # Kotlin classes/methods are final by default
            )

            # Determine if it's a method or function based on parent
            component_type = ComponentType.METHOD if parent_component else ComponentType.FUNCTION

            return self._create_component(
                component_id,
                component_type,
                node,
                file_path,
                "kotlin",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extension_receiver(self, node: Any, source_lines: List[str]) -> Optional[str]:
        """Return the receiver type of an extension function or property, or None.

        In tree-sitter-kotlin an extension declaration lays the receiver type
        out before the name, separated by a `.`:
        `[fun, user_type(String), ., identifier(toSlug), ...]`. The node
        immediately before that top-level `.` is the receiver. A plain function
        or property has no such `.` and returns None. A `.` inside a value
        (`val x = a.b`) sits deeper in the tree, so it is never mistaken for one.
        """
        prev = None
        for child in node.children:
            if child.type == ".":
                if prev is not None:
                    text = self._get_node_text(prev, source_lines).strip()
                    return text or None
                return None
            prev = child
        return None

    def _with_companion(self, node: Any, source_lines: List[str], id_name: str) -> str:
        """Put the companion's name in front of a companion member's id.

            fun create() in the class             -> Order.create
            fun create() in `companion object`    -> Order.Companion.create

        Without this, both get the same id and one replaces the other.
        The parent stays `Order`, so a call like `Order.empty()` still finds the method.
        V2 only: old V1 spaces keep their ids when refreshed.
        """
        if self._scheme != IdScheme.V2_PATH:
            return id_name
        body = node.parent  # the `{ ... }` the declaration sits in
        if body is None or body.type != "class_body":
            return id_name
        companion = body.parent  # who owns that `{ ... }`: a class, an object or a companion
        if companion is None or companion.type != "companion_object":
            return id_name
        # `companion object Factory` -> "Factory"; plain `companion object` -> "Companion",
        # the name Kotlin itself gives it.
        name = next(
            (
                self._get_node_text(child, source_lines)
                for child in companion.children
                if child.type == "identifier"
            ),
            "Companion",
        )
        return f"{name}.{id_name}"

    def _extract_class_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract Kotlin class/object/interface component."""
        try:
            name = None
            for child in node.children:
                if child.type == "identifier":
                    name = self._get_node_text(child, source_lines)
                    break

            if not name:
                return None

            id_name = self._with_companion(node, source_lines, name)
            component_id = self.make_id(file_path, id_name, parent_component)

            component_type = self._determine_class_type(node)
            visibility = self._extract_visibility(node, source_lines)

            attributes = ComponentAttributes(
                signature=self._extract_signature(node, source_lines),
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                is_static=False,
                is_abstract=self._has_modifier(node, "abstract"),
                is_final=not self._has_modifier(node, "open"),
            )

            return self._create_component(
                component_id,
                component_type,
                node,
                file_path,
                "kotlin",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_property_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract Kotlin property component."""
        try:
            property_name = None
            for child in node.children:
                if child.type == "variable_declaration":
                    for grandchild in child.children:
                        if grandchild.type == "identifier":
                            property_name = self._get_node_text(grandchild, source_lines)
                            break
                    if property_name:
                        break

            if not property_name:
                return None

            # Keep the receiver in the id: `val Order.isEligible` -> `Order.isEligible`.
            # Without it, the property gets the same id as `fun isEligible` and replaces it.
            # V2 only: old V1 spaces keep their ids when refreshed.
            receiver = (
                self._extension_receiver(node, source_lines)
                if self._scheme == IdScheme.V2_PATH
                else None
            )
            id_name = f"{receiver}.{property_name}" if receiver else property_name
            id_name = self._with_companion(node, source_lines, id_name)

            component_id = self.make_id(file_path, id_name, parent_component)

            visibility = self._extract_visibility(node, source_lines)
            is_constant = self._has_modifier(node, "const") or self._is_val(node)

            component_type = ComponentType.CONSTANT if is_constant else ComponentType.VARIABLE

            attributes = ComponentAttributes(
                signature=self._extract_signature(node, source_lines),
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
                is_static=False,
                is_final=self._is_val(node),
            )

            return self._create_component(
                component_id,
                component_type,
                node,
                file_path,
                "kotlin",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_visibility(self, node: Any, source_lines: List[str]) -> Visibility:
        """Extract visibility modifier from node."""
        for child in node.children:
            if child.type == "modifiers":
                for mod_child in child.children:
                    if mod_child.type == "visibility_modifier":
                        text = self._get_node_text(mod_child, source_lines).lower()
                        if "public" in text:
                            return Visibility.PUBLIC
                        elif "protected" in text:
                            return Visibility.PROTECTED
                        elif "private" in text:
                            return Visibility.PRIVATE
                        elif "internal" in text:
                            return (
                                Visibility.PACKAGE
                            )  # Kotlin's internal is similar to package-private
        return Visibility.PUBLIC  # Kotlin default is public

    def _has_modifier(self, node: Any, modifier: str) -> bool:
        """Check if node has specific modifier."""
        for child in node.children:
            if child.type == "modifiers":
                for mod_child in child.children:
                    text = self._get_node_text_simple(mod_child).lower()
                    if modifier.lower() in text:
                        return True
        return False

    def _is_val(self, node: Any) -> bool:
        """Check if property is declared with val (immutable)."""
        for child in node.children:
            if child.type == "val":
                return True
        return False

    def _extract_signature(self, node: Any, source_lines: List[str]) -> str:
        """Extract function/class signature."""
        text = self._get_node_text(node, source_lines)
        lines = text.split("\n")
        if len(lines) > 1:
            return lines[0][:200]
        return text[:200]

    def _determine_class_type(self, node: Any) -> ComponentType:
        """Determine component type based on node type."""
        if node.type == "interface_declaration":
            return ComponentType.INTERFACE
        elif node.type == "object_declaration":
            return ComponentType.CLASS  # Kotlin objects are singleton classes
        return ComponentType.CLASS
