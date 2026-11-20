"""
TypeScript-specific parser implementation.
"""

from typing import List, Optional, Any
from pathlib import Path

from .base_parser import BaseLanguageParser
from ..core.models import Component, ComponentType, ComponentAttributes, Parameter, Visibility


class TypeScriptParser(BaseLanguageParser):
    """TypeScript-specific parser implementation."""

    def get_call_node_types(self) -> List[str]:
        """Return AST node types that represent function calls in TypeScript."""
        return ["call_expression"]

    def get_function_node_types(self) -> List[str]:
        """Return AST node types that represent function definitions in TypeScript."""
        return [
            "function_declaration",
            "arrow_function",
            "function_expression",
            "method_definition",
        ]

    def extract_call_name(self, function_node: Any) -> Optional[str]:
        """Extract function name from TypeScript call node."""
        if function_node.type == "identifier":
            return self._get_node_text_simple(function_node)
        elif function_node.type == "member_expression":
            # TypeScript method call: obj.method()
            if function_node.children and len(function_node.children) >= 3:
                object_node = function_node.children[0]
                method_name_node = function_node.children[2]
                object_name = self._get_node_text_simple(object_node)
                method_name = self._get_node_text_simple(method_name_node)
                # Return full method call (e.g., "userService.createUser")
                return f"{object_name}.{method_name}"
        elif function_node.type == "subscript":
            return None  # Skip for now
        return None

    def extract_components(
        self, root_node: Any, source_code: bytes, file_path: Path
    ) -> List[Component]:
        """Extract components from TypeScript syntax tree."""
        components: List[Component] = []
        source_lines = source_code.decode("utf-8").split("\n")

        def extract_from_node(node: Any, parent_component: Optional[str] = None) -> None:
            if node.type in ["function_declaration", "method_definition", "arrow_function"]:
                component = self._extract_function_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)

            elif node.type in ["class_declaration", "interface_declaration"]:
                component = self._extract_class_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)
                    for child in node.children:
                        if child.type == "class_body":
                            for grandchild in child.children:
                                extract_from_node(grandchild, component.id)

            elif node.type in ["variable_declaration", "lexical_declaration"]:
                component = self._extract_variable_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)

            if node.type not in ["class_declaration", "interface_declaration"]:
                for child in node.children:
                    extract_from_node(child, parent_component)

        extract_from_node(root_node)
        return components

    def extract_function_parameters(
        self, function_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """Extract parameters from TypeScript function definition node."""
        parameters: List[Parameter] = []

        try:
            for child in function_node.children:
                if child.type == "formal_parameters":
                    for param_child in child.children:
                        if param_child.type in [
                            "identifier",
                            "required_parameter",
                            "optional_parameter",
                        ]:
                            param_name = self._get_node_text(param_child, source_lines)
                            param_name = param_name.split(":")[0].strip()
                            required = param_child.type != "optional_parameter"
                            parameters.append(Parameter(name=param_name, required=required))
        except Exception:
            pass

        return parameters

    def _extract_function_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract TypeScript function component."""
        try:
            function_name = "anonymous"

            # Handle different node types for function extraction
            if node.type == "method_definition":
                # For class methods, look for property_identifier or identifier
                for child in node.children:
                    if child.type in ["property_identifier", "identifier"]:
                        function_name = self._get_node_text(child, source_lines)
                        break
            else:
                # For regular functions, look for identifier
                for child in node.children:
                    if child.type == "identifier":
                        function_name = self._get_node_text(child, source_lines)
                        break

            component_id = self.make_id(file_path, function_name, parent_component)

            parameters = self.extract_function_parameters(node, source_lines)
            visibility = Visibility.PRIVATE if function_name.startswith("_") else Visibility.PUBLIC

            attributes = ComponentAttributes(
                signature=self._get_node_text(node, source_lines)[:100],
                parameters=parameters,
                visibility=visibility,
                line_count=node.end_point[0] - node.start_point[0] + 1,
            )

            component_type = ComponentType.METHOD if parent_component else ComponentType.FUNCTION

            return self._create_component(
                component_id,
                component_type,
                node,
                file_path,
                "typescript",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_class_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract TypeScript class/interface component."""
        try:
            name = "unknown"
            for child in node.children:
                if child.type in ["type_identifier", "identifier"]:
                    name = self._get_node_text(child, source_lines)
                    break

            component_id = self.make_id(file_path, name, parent_component)

            component_type = (
                ComponentType.INTERFACE
                if node.type == "interface_declaration"
                else ComponentType.CLASS
            )

            attributes = ComponentAttributes(
                signature=f"{node.type.replace('_', ' ')} {name}",
                visibility=Visibility.PUBLIC,
                line_count=node.end_point[0] - node.start_point[0] + 1,
            )

            return self._create_component(
                component_id,
                component_type,
                node,
                file_path,
                "typescript",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_variable_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract TypeScript variable/constant component."""
        try:
            for child in node.children:
                if child.type == "variable_declarator":
                    variable_name = None
                    is_function = False

                    for grandchild in child.children:
                        if grandchild.type == "identifier":
                            variable_name = self._get_node_text(grandchild, source_lines)
                        elif grandchild.type in ["arrow_function", "function_expression"]:
                            is_function = True
                        elif grandchild.type == "call_expression":
                            # Check if call expression contains a function (e.g., catchErrors(async () => {}))
                            is_function = self._contains_function_in_call(grandchild, source_lines)

                    if not variable_name or variable_name.startswith("_"):
                        return None

                    component_id = self.make_id(file_path, variable_name, parent_component)

                    if is_function:
                        component_type = (
                            ComponentType.METHOD if parent_component else ComponentType.FUNCTION
                        )
                        parameters = self._extract_function_parameters_from_declarator(
                            child, source_lines
                        )
                    else:
                        is_const = any(c.type == "const" for c in node.children)
                        component_type = (
                            ComponentType.CONSTANT if is_const else ComponentType.VARIABLE
                        )
                        parameters = []

                    attributes = ComponentAttributes(
                        signature=self._get_node_text(node, source_lines),
                        parameters=parameters,
                        visibility=Visibility.PUBLIC,
                        line_count=node.end_point[0] - node.start_point[0] + 1,
                    )

                    return self._create_component(
                        component_id,
                        component_type,
                        node,
                        file_path,
                        "typescript",
                        attributes,
                        parent_component,
                    )
            return None
        except Exception:
            return None

    def _extract_function_parameters_from_declarator(
        self, declarator_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """Extract parameters from TypeScript function in variable declarator."""
        try:
            for child in declarator_node.children:
                if child.type in ["arrow_function", "function_expression"]:
                    return self.extract_function_parameters(child, source_lines)
                elif child.type == "call_expression":
                    # Handle any call expression that contains functions
                    return self._extract_parameters_from_call(child, source_lines)
        except Exception:
            pass
        return []

    def _contains_function_in_call(self, call_node: Any, source_lines: List[str]) -> bool:
        """Check if a call expression contains a function as an argument."""
        try:
            # Check arguments for any function expressions
            for child in call_node.children:
                if child.type == "arguments":
                    for arg in child.children:
                        if arg.type in ["arrow_function", "function_expression"]:
                            return True
                        # Recursively check nested call expressions
                        elif arg.type == "call_expression":
                            if self._contains_function_in_call(arg, source_lines):
                                return True
            return False
        except Exception:
            return False

    def _extract_parameters_from_call(
        self, call_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """Extract parameters from function inside call expression."""
        try:
            for child in call_node.children:
                if child.type == "arguments":
                    for arg in child.children:
                        if arg.type in ["arrow_function", "function_expression"]:
                            return self.extract_function_parameters(arg, source_lines)
                        # Recursively check nested calls
                        elif arg.type == "call_expression":
                            params = self._extract_parameters_from_call(arg, source_lines)
                            if params:
                                return params
        except Exception:
            pass
        return []
