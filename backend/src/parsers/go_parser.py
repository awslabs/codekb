"""
Go-specific parser implementation.
"""

from typing import List, Optional, Any
from pathlib import Path

from .base_parser import BaseLanguageParser
from ..core.models import Component, ComponentType, ComponentAttributes, Parameter, Visibility


class GoParser(BaseLanguageParser):
    """Go-specific parser implementation."""

    def get_call_node_types(self) -> List[str]:
        """Return AST node types that represent function calls in Go."""
        return ["call_expression"]

    def get_function_node_types(self) -> List[str]:
        """Return AST node types that represent function definitions in Go."""
        return ["function_declaration", "method_declaration"]

    def extract_call_name(self, function_node: Any) -> Optional[str]:
        """Extract function name from Go call node."""
        if function_node.type == "identifier":
            return self._get_node_text_simple(function_node)
        elif function_node.type == "selector_expression":
            # Go method call: obj.Method()
            if function_node.children and len(function_node.children) >= 3:
                object_node = function_node.children[0]
                method_name_node = function_node.children[2]
                object_name = self._get_node_text_simple(object_node)
                method_name = self._get_node_text_simple(method_name_node)
                # Return full method call (e.g., "user.Activate")
                return f"{object_name}.{method_name}"
        elif function_node.type == "call_expression":
            # Handle nested call expressions
            if function_node.children:
                return self.extract_call_name(function_node.children[0])
        return None

    def extract_components(
        self, root_node: Any, source_code: bytes, file_path: Path
    ) -> List[Component]:
        """Extract components from Go syntax tree."""
        components: List[Component] = []
        source_lines = source_code.decode("utf-8").split("\n")

        def extract_from_node(node: Any, parent_component: Optional[str] = None) -> None:
            # Functions
            if node.type == "function_declaration":
                component = self._extract_function_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)

            # Methods (functions with receivers)
            elif node.type == "method_declaration":
                component = self._extract_method_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)

            # Type declarations (struct, interface, type alias)
            elif node.type == "type_declaration":
                extracted = self._extract_type_components(
                    node, source_lines, file_path, parent_component
                )
                components.extend(extracted)

            # Constants
            elif node.type == "const_declaration":
                extracted = self._extract_const_components(
                    node, source_lines, file_path, parent_component
                )
                components.extend(extracted)

            # Variables
            elif node.type == "var_declaration":
                extracted = self._extract_var_components(
                    node, source_lines, file_path, parent_component
                )
                components.extend(extracted)

            # Recurse into children
            for child in node.children:
                extract_from_node(child, parent_component)

        extract_from_node(root_node)
        return components

    def extract_function_parameters(
        self, function_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """Extract parameters from Go function definition node."""
        parameters: List[Parameter] = []

        try:
            for child in function_node.children:
                if child.type == "parameter_list":
                    for param_child in child.children:
                        if param_child.type == "parameter_declaration":
                            # Extract parameter name(s)
                            param_names = []
                            for subchild in param_child.children:
                                if subchild.type == "identifier":
                                    param_names.append(self._get_node_text(subchild, source_lines))

                            # Go allows multiple params with same type: func foo(a, b int)
                            for param_name in param_names:
                                parameters.append(Parameter(name=param_name, required=True))

                        elif param_child.type == "variadic_parameter_declaration":
                            # Handle variadic parameters: func foo(items ...string)
                            for subchild in param_child.children:
                                if subchild.type == "identifier":
                                    param_name = self._get_node_text(subchild, source_lines)
                                    parameters.append(Parameter(name=param_name, required=False))
        except Exception:
            pass

        return parameters

    def _extract_function_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract Go function component."""
        try:
            function_name = None

            for child in node.children:
                if child.type == "identifier":
                    function_name = self._get_node_text(child, source_lines)
                    break

            if not function_name:
                return None

            component_id = self.make_id(file_path, function_name, parent_component)

            parameters = self.extract_function_parameters(node, source_lines)

            # In Go, lowercase first letter = private, uppercase = public
            visibility = Visibility.PUBLIC if function_name[0].isupper() else Visibility.PRIVATE

            attributes = ComponentAttributes(
                signature=self._get_node_text(node, source_lines)[:200],
                parameters=parameters,
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
            )

            return self._create_component(
                component_id,
                ComponentType.FUNCTION,
                node,
                file_path,
                "go",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_method_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract Go method component (function with receiver)."""
        try:
            method_name = None
            receiver_type = None

            # Extract receiver type
            for child in node.children:
                if child.type == "parameter_list":
                    # This is the receiver
                    receiver_type = self._extract_receiver_type(child, source_lines)
                    break

            # Extract method name
            for child in node.children:
                if child.type == "field_identifier":
                    method_name = self._get_node_text(child, source_lines)
                    break

            if not method_name or not receiver_type:
                return None

            # Component ID includes receiver type: User.Activate
            component_id = self.make_id(file_path, f"{receiver_type}.{method_name}")

            parameters = self.extract_function_parameters(node, source_lines)

            visibility = Visibility.PUBLIC if method_name[0].isupper() else Visibility.PRIVATE

            attributes = ComponentAttributes(
                signature=self._get_node_text(node, source_lines)[:200],
                parameters=parameters,
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
            )

            return self._create_component(
                component_id,
                ComponentType.METHOD,
                node,
                file_path,
                "go",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_receiver_type(self, receiver_node: Any, source_lines: List[str]) -> Optional[str]:
        """Extract the receiver type from a method's parameter list."""
        try:
            for child in receiver_node.children:
                if child.type == "parameter_declaration":
                    # Look for type_identifier or pointer_type
                    for subchild in child.children:
                        if subchild.type == "type_identifier":
                            return self._get_node_text(subchild, source_lines)
                        elif subchild.type == "pointer_type":
                            # Handle *User -> User
                            for type_child in subchild.children:
                                if type_child.type == "type_identifier":
                                    return self._get_node_text(type_child, source_lines)
        except Exception:
            pass
        return None

    def _extract_type_components(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> List[Component]:
        """Extract type declarations (struct, interface, type alias)."""
        components = []

        try:
            for child in node.children:
                if child.type == "type_spec":
                    component = self._extract_type_spec(
                        child, source_lines, file_path, parent_component
                    )
                    if component:
                        components.append(component)
        except Exception:
            pass

        return components

    def _extract_type_spec(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract a single type specification."""
        try:
            type_name = None
            type_kind = None

            # Extract type name
            for child in node.children:
                if child.type == "type_identifier":
                    type_name = self._get_node_text(child, source_lines)
                    break

            if not type_name:
                return None

            # Determine type kind
            for child in node.children:
                if child.type == "struct_type":
                    type_kind = ComponentType.CLASS
                    break
                elif child.type == "interface_type":
                    type_kind = ComponentType.INTERFACE
                    break
                elif child.type in ["type_identifier", "pointer_type", "slice_type", "map_type"]:
                    # Type alias - treat as a constant for now
                    type_kind = ComponentType.CONSTANT
                    break

            if not type_kind:
                # Default to constant if we can't determine
                type_kind = ComponentType.CONSTANT

            component_id = self.make_id(file_path, type_name, parent_component)

            visibility = Visibility.PUBLIC if type_name[0].isupper() else Visibility.PRIVATE

            attributes = ComponentAttributes(
                signature=self._get_node_text(node, source_lines)[:200],
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
            )

            return self._create_component(
                component_id,
                type_kind,
                node,
                file_path,
                "go",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_const_components(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> List[Component]:
        """Extract constant declarations."""
        components = []

        try:
            for child in node.children:
                if child.type == "const_spec":
                    component = self._extract_const_spec(
                        child, source_lines, file_path, parent_component
                    )
                    if component:
                        components.append(component)
        except Exception:
            pass

        return components

    def _extract_const_spec(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract a single constant specification."""
        try:
            const_name = None

            # Extract constant name
            for child in node.children:
                if child.type == "identifier":
                    const_name = self._get_node_text(child, source_lines)
                    break

            if not const_name:
                return None

            component_id = self.make_id(file_path, const_name, parent_component)

            visibility = Visibility.PUBLIC if const_name[0].isupper() else Visibility.PRIVATE

            attributes = ComponentAttributes(
                signature=self._get_node_text(node, source_lines),
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
            )

            return self._create_component(
                component_id,
                ComponentType.CONSTANT,
                node,
                file_path,
                "go",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_var_components(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> List[Component]:
        """Extract variable declarations."""
        components = []

        try:
            for child in node.children:
                if child.type == "var_spec":
                    component = self._extract_var_spec(
                        child, source_lines, file_path, parent_component
                    )
                    if component:
                        components.append(component)
        except Exception:
            pass

        return components

    def _extract_var_spec(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract a single variable specification."""
        try:
            var_name = None

            # Extract variable name
            for child in node.children:
                if child.type == "identifier":
                    var_name = self._get_node_text(child, source_lines)
                    break

            if not var_name:
                return None

            component_id = self.make_id(file_path, var_name, parent_component)

            visibility = Visibility.PUBLIC if var_name[0].isupper() else Visibility.PRIVATE

            attributes = ComponentAttributes(
                signature=self._get_node_text(node, source_lines),
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
            )

            return self._create_component(
                component_id,
                ComponentType.VARIABLE,
                node,
                file_path,
                "go",
                attributes,
                parent_component,
            )
        except Exception:
            return None
