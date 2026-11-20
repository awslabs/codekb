"""
Python-specific parser implementation.
"""

from typing import List, Optional, Any
from pathlib import Path

from .base_parser import BaseLanguageParser
from ..core.models import Component, ComponentType, ComponentAttributes, Parameter, Visibility


class PythonParser(BaseLanguageParser):
    """Python-specific parser implementation."""

    def get_call_node_types(self) -> List[str]:
        """Return AST node types that represent function calls in Python."""
        return ["call"]

    def get_function_node_types(self) -> List[str]:
        """Return AST node types that represent function definitions in Python."""
        return ["function_definition", "lambda"]

    def extract_call_name(self, function_node: Any) -> Optional[str]:
        """Extract function name from Python call node."""
        if function_node.type == "identifier":
            return self._get_node_text_simple(function_node)
        elif function_node.type == "attribute":
            # Python method call: obj.method()
            if function_node.children and len(function_node.children) >= 3:
                object_node = function_node.children[0]
                method_name_node = function_node.children[2]
                object_name = self._get_node_text_simple(object_node)
                method_name = self._get_node_text_simple(method_name_node)
                # Return full method call (e.g., "user_service.create_user")
                return f"{object_name}.{method_name}"
        elif function_node.type == "subscript":
            return None  # Skip for now
        return None

    def extract_components(
        self, root_node: Any, source_code: bytes, file_path: Path
    ) -> List[Component]:
        """Extract components from Python syntax tree."""
        components: List[Component] = []
        source_lines = source_code.decode("utf-8").split("\n")

        def extract_from_node(node: Any, parent_component: Optional[str] = None) -> None:
            if node.type == "function_definition":
                component = self._extract_function_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)
                    # Extract nested functions
                    for child in node.children:
                        if child.type == "block":
                            for grandchild in child.children:
                                extract_from_node(grandchild, component.id)

            elif node.type == "class_definition":
                component = self._extract_class_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)
                    for child in node.children:
                        if child.type == "block":
                            for grandchild in child.children:
                                extract_from_node(grandchild, component.id)

            elif node.type == "assignment":
                component = self._extract_variable_component(
                    node, source_lines, file_path, parent_component
                )
                if component:
                    components.append(component)

            if node.type not in ["class_definition", "function_definition"]:
                for child in node.children:
                    extract_from_node(child, parent_component)

        extract_from_node(root_node)
        return components

    def _function_signature(self, node: Any, source_lines: List[str]) -> str:
        """Build a real `def name(params) -> ret` signature for a function.

        Previously the signature was taken from the node's first child, which
        is just the `def` keyword — so every function shared the signature
        ``'def'``. That left overload stubs (`@overload`), property getters and
        setters, and redefinitions indistinguishable, so the overload
        disambiguator had nothing to hash and fell back to a positional
        (order-dependent) suffix. Building the real signature from the `name`,
        `parameters` and `return_type` fields — which include the parameter
        annotations that tell `@overload` stubs apart — makes the discriminator
        stable and meaningful, and gives a correct signature for display.
        """
        name_node = node.child_by_field_name("name")
        params_node = node.child_by_field_name("parameters")
        return_node = node.child_by_field_name("return_type")
        name = self._get_node_text(name_node, source_lines) if name_node else ""
        params = self._get_node_text(params_node, source_lines) if params_node else "()"
        signature = f"def {name}{params}"
        if return_node is not None:
            signature += f" -> {self._get_node_text(return_node, source_lines)}"
        return " ".join(signature.split())

    def extract_function_parameters(
        self, function_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """Extract parameters from Python function definition node."""
        parameters: List[Parameter] = []

        try:
            for child in function_node.children:
                if child.type == "parameters":
                    for param_child in child.children:
                        if param_child.type == "identifier":
                            param_name = self._get_node_text(param_child, source_lines)
                            parameters.append(Parameter(name=param_name, required=True))
                        elif param_child.type == "default_parameter":
                            param_name = (
                                self._get_node_text(param_child.children[0], source_lines)
                                if param_child.children
                                else "unknown"
                            )
                            default_value = (
                                self._get_node_text(param_child.children[2], source_lines)
                                if len(param_child.children) > 2
                                else None
                            )
                            parameters.append(
                                Parameter(
                                    name=param_name, default_value=default_value, required=False
                                )
                            )
        except Exception:
            pass

        return parameters

    def _extract_function_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract Python function component."""
        try:
            name_node = None
            for child in node.children:
                if child.type == "identifier":
                    name_node = child
                    break

            if not name_node:
                return None

            function_name = self._get_node_text(name_node, source_lines)
            component_id = self.make_id(file_path, function_name, parent_component)

            parameters = self.extract_function_parameters(node, source_lines)
            visibility = Visibility.PRIVATE if function_name.startswith("_") else Visibility.PUBLIC

            attributes = ComponentAttributes(
                signature=self._function_signature(node, source_lines),
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
                "python",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_class_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract Python class component."""
        try:
            name_node = None
            for child in node.children:
                if child.type == "identifier":
                    name_node = child
                    break

            if not name_node:
                return None

            class_name = self._get_node_text(name_node, source_lines)
            component_id = self.make_id(file_path, class_name, parent_component)

            attributes = ComponentAttributes(
                signature=f"class {class_name}",
                visibility=Visibility.PUBLIC,
                line_count=node.end_point[0] - node.start_point[0] + 1,
            )

            return self._create_component(
                component_id,
                ComponentType.CLASS,
                node,
                file_path,
                "python",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_variable_component(
        self, node: Any, source_lines: List[str], file_path: Path, parent_component: Optional[str]
    ) -> Optional[Component]:
        """Extract Python variable/constant component."""
        try:
            left_node = node.children[0] if node.children else None
            if not left_node or left_node.type != "identifier":
                return None

            variable_name = self._get_node_text(left_node, source_lines)

            if variable_name.startswith("_") or variable_name in ["__name__", "__main__"]:
                return None

            component_id = self.make_id(file_path, variable_name, parent_component)

            # Check if this is a function assignment (lambda or function reference)
            is_function = False
            parameters = []

            if len(node.children) >= 3:  # assignment has left, =, right
                right_node = node.children[2]
                if right_node.type == "lambda":
                    is_function = True
                    parameters = self._extract_lambda_parameters(right_node, source_lines)
                elif right_node.type in ["identifier", "attribute"] and self._is_function_reference(
                    right_node, source_lines
                ):
                    is_function = True

            if is_function:
                component_type = (
                    ComponentType.METHOD if parent_component else ComponentType.FUNCTION
                )
            else:
                component_type = (
                    ComponentType.CONSTANT if variable_name.isupper() else ComponentType.VARIABLE
                )

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
                "python",
                attributes,
                parent_component,
            )
        except Exception:
            return None

    def _extract_lambda_parameters(
        self, lambda_node: Any, source_lines: List[str]
    ) -> List[Parameter]:
        """Extract parameters from Python lambda expression."""
        parameters = []
        try:
            for child in lambda_node.children:
                if child.type == "lambda_parameters":
                    for param_child in child.children:
                        if param_child.type == "identifier":
                            param_name = self._get_node_text(param_child, source_lines)
                            parameters.append(Parameter(name=param_name, required=True))
        except Exception:
            pass
        return parameters

    def _is_function_reference(self, node: Any, source_lines: List[str]) -> bool:
        """Check if a node represents a function reference (heuristic)."""
        try:
            text = self._get_node_text(node, source_lines).lower()
            # Simple heuristic: common function-like names
            function_indicators = ["handler", "callback", "func", "method", "process", "execute"]
            return any(indicator in text for indicator in function_indicators)
        except Exception:
            return False
